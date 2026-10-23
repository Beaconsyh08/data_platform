# Shared data-processing strategies

This is an opt-in planning experiment, not model training or automatic business execution.
Administrators manage shared workflow templates; individual conversations, notes and Hindsight
banks remain private. A successful Job does not prove better data quality.

## Runtime contract

`ai_tools/strategies.py` owns administration and selection. The independent strategy worker owns
candidate generation and evaluation. Both reuse the operator registry, PipelineManager, ordinary
Job/Attempt admission and final acknowledgement. No Agent protocol or execution privilege is added.

Five additive control tables store evidence, immutable candidate contents, immutable evaluation
sets, leased experiments and the active binding. Pipeline versions gain nullable strategy attribution;
old versions and clients remain readable. Small bounded evaluation records stay in the control database.
No raw reports or model prompts are written to the audit log.

Templates allow only Snapshot, Split, Convert v3, Quality and Prepare Viewer. They carry exact operator
versions, supported formats, required standard fields, preceding input/evidence references and explicit
`$request.<parameter>` placeholders. Literal parameter values, arbitrary instructions, paths, unknown
fields and source mutation operators are rejected. Missing user intent still requires clarification;
all proposals pass the existing preview. Strategy versions never grant permissions or approve review.

The collector projects at most 200 recent terminal pipelines per pass, checking administrator and
original-owner access, registered input identity, immutable plan digest and final attempts. Shared
projections omit names, paths, parameter values, messages and report bodies. Unknown failures remain
unclassified; cancellation, process loss and provider failures are not inferred to be planning mistakes.
Evidence revisions are not overwritten. Changed or inaccessible evidence makes dependent strategies
unavailable; foreground selection persists invalidation and falls back to ordinary planning.

Every request pins its selected version and digest in its durable admission snapshot. At most five
matching templates enter the model request. Existing requests keep their version across activation;
disabling a version prevents publication/confirmation of its unsubmitted cards. Accepted pipelines are
not rewritten. Server-resolved attribution marks edited drafts as `user_modified`, and client-provided
strategy IDs cannot create attribution. Web and CLI display the short version identifier.

## Configure an isolated experiment

The default feature switch is off. The experiment worker defaults to dev only; non-dev manual
acceptance requires a separate explicit deployment switch. After deploying to dev, an operator with service-management access
sets `DATA_PLATFORM_STRATEGIES_ENABLED=1` through the existing environment configuration procedure.
The release installs `data-platform-strategy@dev.service`; no production experiment worker is started.
The foreground feature can be disabled independently of existing tasks and private Hindsight.

Generate three tiny fixtures in a new, dedicated directory accessible to Server A and the test node:

```bash
.venv/bin/python -m lerobot.data_platform.ai_tools.strategy_fixtures \
  --root /absolute/new/synthetic-strategy-fixtures
```

Register all three roots through normal dataset discovery. Use a dedicated operator with explicit grants
to just these fixtures; the service rejects an administrator, unrestricted account, unrelated grants,
changed fixture bytes or symlinked roots. Server A must be able to read the synthetic fixtures and
derived outputs for independent verification. Unmounted remote-only test roots are not supported in
this first implementation. Do not replace unavailable reads with successful validation flags.

Open **Platform management → Strategies** (`/admin/strategies`). Choose the operator, node and numeric v2.1/v3.0 plus v2.1 video
locations, save budgets and unpause. Create the initial evaluation set, inspect its expected behavior
and approve it. This case approval is distinct from activating a strategy. No suite is automatically
human-approved by its installer or worker.

The initial set has 20 development and 20 hidden cases. They include composed pipelines, explicit
parameter preservation, clarification, unavailable media, review and authorization explanations.
Structural oracles evaluate expected operations, data references and parameters; clarification oracles
require no operation plus named clarification terms. These bounded oracles do not certify unrestricted
natural-language understanding. Dedicated permission tests remain an additional release gate.

Each automatic round requires ten new eligible Run records and at least 24 hours since the last
automatic start. Administrators may also start a round explicitly. Each round generates at most two
candidates, evaluates baseline/candidates on development cases, then tests only the selected candidate
against the baseline on hidden cases. Each case is repeated twice. The generator never receives hidden
cases or answers. Used sets cannot be reused; import a new reviewed set for another round. Imported
holdout cases cannot duplicate existing case contents merely by renaming IDs; semantic independence
still requires the administrator's review.

Default ceilings: 1,000 provider calls, 1,000,000 input/output tokens, four active hours and twelve
test pipelines, with one pipeline at a time. Provider calls reserve budget before sending. Missing usage,
unknown call outcomes, lost authorization, incompatible versions and external failures produce
`incomplete`, never a passing result. A durable request marker prevents automatically resending an
unknown provider call. A completed candidate is checkpointed before the next phase; pipeline delivery
identities survive a worker restart. Pausing stops new worker activity; submitted computation retains
normal Jobs controls. Cancelling an experiment does not delete its outputs or business data.

## Promotion and rollback

Eligibility requires 40 paired hidden measurements, no safety failure or stable newly failing case,
and either two additional correct measurements or unchanged correctness with at least 15% fewer model
calls. Mean token usage and P95 latency must each stay within 110% of the baseline. There is no automatic
activation, including when every gate passes. A safety or data-integrity violation immediately revokes
the affected strategy and clears its active binding; ordinary incomplete or worse evaluations keep
the existing binding.

The selected candidate and baseline then run six real synthetic scenarios each through PipelineManager.
Publication must be acknowledged; derived datasets must be registered. Independent validation checks
source hashes, episode selection, untouched observations/actions, indexes, timestamps, task metadata,
per-episode and aggregate statistics, both physical formats, shared v3 shard preservation and decoded
video frames with their episode time references. The fixed conversion scenario uses the video fixture;
planning cases continue to use the numeric-only fixtures. This is synthetic integrity coverage, not
validation of business video semantics. Output retention follows the
existing protected dataset cleanup workflow; this worker never recursively deletes directories.

The administrator reviews the version diff and paired measurements, then activates an exact eligible
version. Reactivating a retained, compatible, non-revoked version performs rollback. Disabled or
source-invalidated versions cannot be silently reactivated. Dev approval does not by itself create an approved strategy in prod. For an explicitly authorized
production acceptance, set `DATA_PLATFORM_STRATEGY_EXPERIMENTS_ENABLED=1` in the production service
environment and run `python -m lerobot.data_platform.ai_tools.strategy_worker` under the normal service
identity/environment. Configure the same restricted synthetic-node contract and start a manual round
through the administrator page. Automatic starts remain forbidden outside dev. Stop that acceptance
worker and remove the override afterwards; the server can continue consuming the manually activated
version. No cross-environment database copying or trust bypass is required.

## APIs and verification

All endpoints use `/api/ai-assistant/strategies`. `GET` on the base path accepts `location_id` and returns
only a public applicable-version summary. `GET /admin` is administrator-only. Management POSTs are
`configure`, `suites`, `review-suite`, `start`, `cancel`, `activate`, `rollback` and `disable`; all require
`expected_revision`. Configuration carries the test operator/node, three registered locations, switches
and budgets. Suite import accepts the documented `Case` fields in `strategy_evaluation.py`. The existing
authenticated CSRF and maintenance guards apply. Old APIs remain compatible; proposal/action receipts
add optional strategy metadata, and Pipeline save accepts an optional owned `assistant_request_id`.

```bash
DATA_PLATFORM_BROWSER_EXECUTABLE=/usr/bin/google-chrome .venv/bin/python -m pytest \
  tests/datasets/test_ai_strategies.py tests/datasets/test_ai_assistant_actions.py \
  tests/datasets/test_ai_assistant_runtime.py tests/datasets/test_ai_experience.py \
  tests/datasets/test_processing.py -q --confcutdir=tests/datasets
.venv/bin/python experiments/assistant_langgraph/run_acceptance.py --runtime
```

Unit tests use explicit model/computation doubles for the two-generation state machine; separate tests
run actual subset/conversion code and browser interactions. They do not establish real-provider
improvement or an installed Agent's acceptance. Production promotion additionally requires the normal
Server/Agent identity, TLS, heartbeats, maintenance, active-job preservation and authenticated UI checks.
Publish and verify the compatible independently versioned CLI before advancing `cli/latest/install.py`.
Until those checks finish, describe the feature as implemented/tested, not deployed or proven to improve.
