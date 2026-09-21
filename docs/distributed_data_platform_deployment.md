# Distributed Data Platform deployment without OSS

For new deployments and upgrades, use the [development/production guide](data_platform_environments.md).
The single-environment paths below describe legacy installations; migrate them with `adopt-legacy`.
All new update/restart commands require an explicit environment.

This is the first distributed deployment stage. Server A owns the web service, accounts, RBAC,
the dataset catalog, and remote job state. One administrator-managed agent runs on each data node.
Users only open the HTTPS service on server A.

Large original datasets stay on A/B/C. Remote agents execute existing local `run_precompute` and
`run_preprocess_op` functions beside the data. Until OSS is enabled, a remote viewer job streams its
read-only H.264/CSV viewer cache to a configured directory on server A.

## Current guarantees and limits

- MySQL is authoritative for users, sessions, nodes, remote locations, job leases, results, and
  remote job events.
- Local console mode remains available when `DATA_PLATFORM_DATABASE_URL` is unset.
- Remote preprocess jobs support `convert_action`, `convert_v3`, `drop_field`, `smooth_action`,
  `split`, `merge`, `standardize`, and `value_edit`.
- Remote merge accepts at least two registered locations on the same Agent. `dimension_policy=min`
  aligns named action/state dimensions to the smallest layout; `strict` remains the default.
  Cross-node sources and client-supplied source paths are rejected.
- Remote preprocess accepts an operation default or an absolute `out_root` below the selected
  Agent's writable roots. The source, its ancestors, its descendants, `output_mode`, `in_place`,
  and arbitrary source paths remain rejected.
- Agents only read below `DATA_PLATFORM_AGENT_ALLOWED_ROOTS` and only create output below
  `DATA_PLATFORM_AGENT_WRITABLE_ROOTS`.
- Remote viewer cache upload is a temporary no-OSS transport. Its videos consume network bandwidth
  and disk on server A.
- Run one Gunicorn worker for now. Existing local dataset registries and local background jobs still
  contain process-local state, even though remote jobs are stored in MySQL. Threads inside that one
  worker may still run concurrently.
- Agent authentication currently uses a per-node bearer token over TLS. Put the service behind
  HTTPS; mTLS can be added when the node PKI is available.

## 1. Prepare MySQL

Use MySQL 8 with InnoDB and `utf8mb4`. The service account should only access this database.

```sql
CREATE DATABASE data_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'data_platform'@'SERVER_A_PRIVATE_IP' IDENTIFIED BY 'REPLACE_ME';
GRANT ALL PRIVILEGES ON data_platform.* TO 'data_platform'@'SERVER_A_PRIVATE_IP';
```

The first service start creates the `dp_*` tables. Back up MySQL before deploying a future schema
change. Database passwords containing reserved URL characters must be URL-encoded in the SQLAlchemy
URL.

## 2. Install the server dependencies

On server A:

```bash
uv sync --extra data-platform-server --extra test
```

Create separate random values for the initial administrator and agent enrollment:

```bash
openssl rand -base64 36
openssl rand -base64 36
```

Copy `deploy/data-platform/server.env.example` to `/etc/data-platform/server.env`, fill in the
database URL and tokens, then protect it:

```bash
sudo chown root:data-platform /etc/data-platform/server.env
sudo chmod 640 /etc/data-platform/server.env
```

The bootstrap token is only needed until the first administrator is created. The agent enrollment
token is needed whenever a new node enrolls. `DATA_PLATFORM_ALLOW_REGISTRATION=1` allows later users
to request read-only viewer accounts. Requested accounts stay inactive until an administrator
approves them from the control-plane page. Approved accounts start as `viewer`, cannot modify or
delete data, and may later be promoted to `operator`. Only the `admin` role unlocks legacy in-place
and delete operations; central deployments do not use a second Admin Mode password. With
registration disabled, administrators can create accounts with `POST /api/auth/users`.
The example enables `DATA_PLATFORM_TRUST_PROXY=1` because Gunicorn only accepts traffic from the
local Nginx proxy; do not enable it when an untrusted client can connect directly to Gunicorn.

## 3. Start server A

Adjust `/opt/data-platform` and service users in the supplied unit if the checkout lives elsewhere.

```bash
sudo cp deploy/data-platform/data-platform-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now data-platform-web
sudo systemctl status data-platform-web
```

Install the example Nginx configuration after replacing the hostname and certificate paths. Only
Nginx port 443 should be exposed to users; Gunicorn remains bound to `127.0.0.1:9091`. The unlimited
request body and disabled request buffering are necessary while agents upload large viewer videos.

For the current company-network deployment without stable DNS, install the IP-independent Nginx
listener and loopback-only H100 reverse-forward with one command:

```bash
sudo ./deploy/data-platform/configure-dynamic-ip.sh
```

It keeps Gunicorn on loopback, listens on changing Server A addresses, allows only `127.0.0.1` and
`10.8.0.0/16`, rewrites the tunnel target to `127.0.0.1:443`, validates Nginx and both health
endpoints, and restores its timestamped backup on failure. It also installs the later manual restart
command `sudo data-platform-restart`. Systemd still starts all services automatically after a normal
reboot; without DNS, users obtain the new browser IP with `hostname -I`.

Open `https://<current-10.8-address>/login`, choose the first administrator username/password, and
enter `DATA_PLATFORM_BOOTSTRAP_TOKEN`. Remove the bootstrap token from the environment file after the
administrator is created and restart the service.

### Environment-specific updates

Use the [environment deployment guide](data_platform_environments.md) to initialize dev/prod, migrate the
legacy installation, and deploy an approved immutable release. Run `data-platform-update-all --env dev
--version RELEASE`, accept that candidate, then run `data-platform-update-all --env prod --release RELEASE`.
Within either environment, Server A is upgraded before its Agents. A server-only update intentionally
leaves maintenance enabled until matching Agents have been checked.

## 4. Install one agent on each data server

The agent is installed once by an administrator, not by every platform user. Its OS account needs
read permission on allowed datasets and write permission on the configured parent output roots.
Servers B/C do not need a Git checkout or a preinstalled `uv`. Build the versioned bundle on server
A from the exact code revision that should run on the nodes:

```bash
python scripts/build_data_platform_agent_bundle.py --output-dir dist/agent
```

This creates a platform-specific archive and a matching SHA-256 file, for example:

```text
data-platform-agent-0.1.0-linux-x86_64.tar.gz
data-platform-agent-0.1.0-linux-x86_64.tar.gz.sha256
```

The archive contains the project wheel, a locked dependency list, a matching `uv` binary, the
installer, and the systemd unit. It does not contain database credentials, enrollment tokens, or
dataset data. The initial install downloads locked third-party Python wheels, so it requires access
to the configured Python package index. A fully offline wheelhouse is not included yet.

Copy only these two generated files to each node. On server B/C, verify and extract the bundle:

```bash
sha256sum --check data-platform-agent-0.1.0-linux-x86_64.tar.gz.sha256
tar -xzf data-platform-agent-0.1.0-linux-x86_64.tar.gz
cd data-platform-agent-0.1.0-linux-x86_64
./install.sh --verify-only
```

Then install it. The token is requested without echoing it to the terminal or shell history:

```bash
sudo ./install.sh \
  --server-url https://data-platform.example.com \
  --name server-b \
  --allowed-root /data/datasets \
  --writable-root /data
```

Use one `--allowed-root` or `--writable-root` argument per parent when a node has multiple storage
roots. Use a stable unique node name. The service account must be able to traverse/read the allowed
roots and create sibling outputs below the writable roots. Prefer granting it membership in the
existing dataset access group instead of making data world-writable:

```bash
sudo usermod --append --groups DATASET_GROUP data-platform-agent
sudo systemctl restart data-platform-agent
```

The installer places immutable releases below `/opt/data-platform-agent/releases`, switches the
`current` symlink, creates `/etc/data-platform/agent.env`, and enables the service. After the first
successful enrollment, the permanent node token is stored in `/var/lib/data-platform-agent/agent.json`
with mode `0600`; the installer removes the one-time enrollment token from the environment file and
restarts the service. Do not copy one node's state file to another node.

For an upgrade, build a new bundle version, copy it to the node, and run its installer. Existing
node configuration and state are preserved. The installer refuses to overwrite an existing release
directory with the same version.

After extracting a newer version on the node, the upgrade itself is one command:

```bash
sudo ./install.sh
```

The web console can select local and remote datasets from the same **Working dataset** picker.
Remote Cache exposes the same video/CSV, profile, and cache-overwrite controls as Server A. Remote
preprocessing output fields accept absolute paths inside the node's configured writable roots; the
Agent rejects the source directory, its ancestors, its descendants, and paths outside those roots.
Leaving the field empty uses the operation's safe default. Standardize and Convert v3 may overwrite
an existing dataset output when explicitly selected; Standardize may also remove requested episodes
from the new output only.

Inspect the running service with:

```bash
sudo systemctl status data-platform-agent
sudo journalctl -u data-platform-agent -f
```

For a one-shot development check:

```bash
DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN=... \
python -m lerobot.data_platform.agent \
  --server-url https://data-platform.example.com \
  --name server-b \
  --allowed-root /data/datasets \
  --writable-root /data \
  --once
```

Source mutation is disabled independently on every Agent. Leave
`DATA_PLATFORM_AGENT_ALLOW_SOURCE_MUTATIONS=0` for normal sibling-output operation.

## 5. Operate the distributed console

The homepage `/` is the normal dataset entry point. Choose exactly one server from **Server**, enter
a parent directory belonging to that server, then choose **Scan**. For **Server A (local)** this is a
real filesystem scan. For an Agent, the page refreshes the synchronized catalog and filters the
locations below the entered path; the path must stay inside that Agent's configured allowed roots.

Matching remote locations are displayed directly below the scan controls. If a viewer cache is
ready, choose **Viewer**. Otherwise an Operator or Admin can choose **Prepare viewer**; the
Agent builds the cache beside the data and uploads the read-only viewer artifacts to Server A.
Viewer accounts may open an existing cache but cannot start the preparation job.

Choose **select & preprocess**, or use the global **Working dataset** picker, to make an Agent
location the working dataset. The normal Standardize, Transform, Value Edit, and Materialize forms
then submit work to that Agent instead of Server A. Output fields accept a safe operation default or
an absolute path inside that Agent's writable roots. Supported sibling operations are
`convert_action`, `convert_v3`, `drop_field`, `smooth_action`, `split`, `merge`, `standardize`, and
`value_edit`. Remote jobs and their Agent events appear in both the **Jobs** drawer and **Pipeline
Runs** with the same status, output, result summary, and logs. A completed Standardize job also
builds and uploads the derived dataset's Viewer cache, registers the new location, and exposes
**Use dataset** and **Open viewer** actions without a second preparation job. Merge also builds and
uploads the derived Viewer cache before completing; its dry run only returns the dimension mapping.
Update and restart both Server A and the Agent to enable remote merge. The Merge form is available
after the Agent advertises `preprocess.merge` in its capabilities. Agent bundles include and validate
the named dimension alignment implementation during packaging.

For other completed jobs that produce a dataset without a viewer cache, the output card exposes
**Prepare viewer**. It starts cache generation on the server that owns the output and changes to
**Open viewer** after the cache is synchronized. Agent datasets can also enter **Data Curation >
Explore**. **Dataset Analysis** runs directly from synced Agent metadata without a Prepare job;
**Episode Viewer** uses its optional cache. Analysis shows the last sync time and remains available
while the Agent is offline. Update the Agent and sync datasets to include episode lengths
(`analysis_metadata_version=1`); older reports still show available totals and tasks. Embedding,
annotation, and construction actions remain hidden.

### Optional Admin source mutations

Remote in-place value edits, v3 video timestamp repair, and episode deletion remain locked unless
all of these conditions are true:

1. The Server A account is `admin`.
2. `/etc/data-platform/server.env` contains `DATA_PLATFORM_ENABLE_LEGACY_MUTATIONS=1`.
3. The selected Agent has `DATA_PLATFORM_AGENT_ALLOW_SOURCE_MUTATIONS=1` in
   `/etc/data-platform/agent.env`.
4. The dataset itself is below both that Agent's allowed and writable roots.
5. The Admin accepts the warning and types `MUTATE <dataset_key>` exactly.

In the console, select the Agent dataset and open Runs to enter episode indices and a deletion
reason. Admin accounts can submit deletion directly; operators submit a request for administrator
review. The form shows which server/Agent mutation switch blocks submission. Admin accounts can
also use DEL in the cached Viewer, confirm the episode and reason, and follow the submitted task
in Runs. Deletion does not require turning EDIT on. A submitted task is not a completed deletion.
The Registered list's `unregister` action only removes a local registration; it does not delete files.

Cached Viewer EDIT supports Stage and Trim annotations for admin/operator accounts without
loading the remote source dataset. These edits save to the current Viewer cache; they do not apply
trim or stage changes to the Agent source. Viewer accounts remain read-only. Re-preparing the cache
may replace these annotations, so preserve them before rebuilding it.

Apply configuration changes with:

```bash
# Server A
sudo systemctl restart data-platform-web

# Data node
sudo systemctl restart data-platform-agent
```

Before every actual source write, the Agent creates a persistent backup at
`<source-parent>/.data-platform-backups/<dataset-name>/<timestamp>-<job-id>/`. Metadata is copied;
large data/video files use hard links when the filesystem supports them. Failed jobs restore the
source automatically and retain the backup. Successful mutations retain the backup for manual
recovery and append request/result events to the existing operation log. Server A invalidates its
uploaded Viewer cache after a successful source change, so prepare the Viewer again before review.

Keep this capability off on nodes that only need read and sibling-output access. Do not place raw
source roots inside Agent writable roots unless remote Admin mutation is intentionally required.

The management page `/control-plane` remains available for node health, user approvals, a compact
remote-location inventory, and job history. It shows only per-node dataset counts by default. Choose
**View N datasets** to inspect one node, then search or filter its locations; the list is paginated at
20 rows. Start preprocessing from the Dataset console so local and Agent datasets use the same forms.

1. Confirm every node has a recent heartbeat.
2. Choose one node and confirm its agent-discovered dataset locations.
3. Select **Prepare viewer** to generate the cache on that node and upload it to server A.
4. When the job completes, **Open viewer cache** opens the normal read-only dataset viewer.
5. Choose **Open in Dataset console** and submit preprocessing from the normal forms. Custom output
   paths are limited to the Agent's writable roots; in-place and source-path fields are rejected by
   both server A and the agent.
6. A Standardize output is reported as a new remote location together with its Viewer cache. Open it
   directly from the completed run or select it as the new working dataset.

Useful endpoints:

- `GET /api/auth/me`
- `GET /api/control/nodes`
- `GET /api/control/locations`
- `GET /api/control/jobs`
- `GET /api/control/jobs/{job_id}`
- `POST /api/control/locations/{location_id}/viewer-jobs`
- `POST /api/control/locations/{location_id}/preprocess-jobs`
- `POST /api/control/locations/{location_id}/mutation-jobs` (Admin plus both mutation switches)

## Recovery and safety

- MySQL and `DATA_PLATFORM_REMOTE_CACHE_ROOT` should be backed up separately. Remote cache is
  reproducible; original datasets are not.
- If an agent is offline, its running lease expires and the same node may claim the job again.
  Executors therefore use new output paths and existing-output rejection rather than overwriting.
- Never give an agent `/` as an allowed root. Configure the smallest dataset parents and sibling
  output parents it needs.
- Keep raw source directories outside writable roots where practical. The platform's existing
  protected-source rules remain relevant for operations executed directly on server A.
- Persistent Agent mutation backups are not pruned automatically. Review and remove an individual
  confirmed backup only after the changed dataset and regenerated Viewer cache have been verified.
