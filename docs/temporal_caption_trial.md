# Single-episode temporal caption experiment

`Data Curation → Annotation → Temporal Caption` is a parallel annotation review surface with remote single-episode jobs.
It does not replace Stage & Subtask or Object Labeling, and does not apply labels to source data.
Local and Agent datasets expose this review tab under the same Data Curation navigation.
Local and remote review use the same template, result selector, synchronized camera video, segment
navigation, active caption highlighting, and optional segment loop.

## Independent inference workflow

1. Select exactly one episode by index. The trial exporter supports image-backed LeRobot v3 datasets
   with 1–4 cameras and regular timestamps. It uses the shared v3 reader to filter a shared Parquet
   shard by `episode_index`, verifies local frame indices/timestamps, and rejects output inside the source.
2. Export the episode's images into labeled, synchronized camera panels and an H.264 MP4. Export
   available pose, gripper, and action observations into an episode bundle outside the source dataset.
   The exporter does not manufacture an action column from measured poses.
3. Send approximately 2 Hz visual observations and 4 Hz rounded pose/gripper observations to
   `qwen3.8-max`. The independently written prompt requires evidence-based segments, object identity
   tracking across cameras, Chinese/English captions, uncertainty, and contiguous half-open frame ranges.
   It explicitly distinguishes measured poses from commanded actions and leaves coordinate/gripper
   conventions unverified. Redundant quaternion signals are omitted from model input.
4. Densify observations within one second of proposed boundaries to approximately 8 Hz, then request
   another partition. Only candidate boundaries are supplied from the first pass, avoiding direct reuse
   of earlier caption wording. Both outputs are retained independently; the second is not auto-accepted.
5. Validate complete frame coverage, no overlaps/gaps, integer endpoints, captions, and confidence labels.
   Save model name, input sample indices, prompt/hash, export digest, usage, and both JSON results. Invalid
   results fail instead of being silently repaired. CLI output retains a completed initial pass if a later request fails; remote jobs publish only complete runs. Requests are capped at 200 images each; this is a trial, not a batch scheduler.

Model confidence is subjective. The trial showed disagreement about whether two views depict the same
marker being handed between manipulators. Temporal refinement did not reliably improve object identity.
The review UI therefore treats both variants as drafts, can display `review_notes` supplied by a reviewer,
and never writes them into training metadata. Human verification is still required.

## Run once

Run preparation on the Agent host, with its existing environment. The output must not already exist:

```bash
python -m lerobot.data_platform.temporal_caption prepare \
  --root /path/to/image-backed-v3-dataset --episode 0 --output /path/to/episode-bundle
```

Transfer only this bundle to the inference host. Configure `DASHSCOPE_API_KEY` in the process environment
(or the existing server credential mechanism); never put it in source, artifacts, prompts, or command-line
options. The shared Qwen transport uses the configured DashScope endpoint. No API keys go to the browser.

Set `DATASET_KEY` to the **registered** dataset key, including its node namespace/path suffix for an Agent.
Use a dedicated experiment root (or the console's lifecycle `temporal_captions` directory):

```bash
export CAPTION_ROOT=/path/to/caption-artifacts
export DATASET_KEY=namespace/registered-dataset
CAPTION_KEY=$(python -c 'import os; from lerobot.data_platform.temporal_caption import artifact_key; print(artifact_key(os.environ["DATASET_KEY"]))')
python -m lerobot.data_platform.temporal_caption infer \
  --bundle /path/to/episode-bundle --dataset-key "$DATASET_KEY" \
  --output "$CAPTION_ROOT/$CAPTION_KEY/trial_001"
python -m lerobot.data_platform.temporal_caption preview \
  --artifact-root "$CAPTION_ROOT" --dataset-key "$DATASET_KEY" --port 8766
```

The isolated preview binds only to `127.0.0.1`; it has no write/API execution surface. Open
`http://127.0.0.1:8766/`. It uses the same routes and UI as the console. Keep private datasets and artifacts
out of Git. The bundle can contain sensitive observations even though it contains no API key.

## Console integration and scope

Configure `DATA_PLATFORM_TEMPORAL_CAPTION_ROOT` on Server A to an artifact root readable by the service,
or place artifacts under `<lifecycle-root>/temporal_captions/<sha256(dataset_key)>/<run>/`.
A run contains `coarse.json`, optional `refined.json`, `video.mp4`, and optional prompt/episode evidence
files. Model result JSON includes the registered dataset key; mismatched keys are rejected.

- Local page: `/<namespace>/<dataset>/temporal-caption`.
- Remote page: `/remote/<location_id>/temporal-caption`.
- Remote API: `/api/control/locations/<location_id>/temporal-caption`.

The remote route resolves the registered location, then reads only the central result artifact. It never
opens a client-provided remote path or queues Viewer generation. The normal console login guard applies;
viewer accounts may read results and seek videos using HTTP Range requests. Full console mode is required.
Operators and administrators can submit single-episode annotation jobs; administrators can import existing
review ZIPs. Results remain review evidence and do not write back to source datasets.

Deploy through the existing immutable dev/prod release workflow. A local preview is not a production
release. Update Server A and the Agent together to expose supported annotation methods.

## Three reusable annotation methods, one review UI

The shared page follows the caption demo layout: synchronized camera video, colored semantic timeline,
gripper event markers, measured gripper/speed curves, bilingual captions, scene and task-quality panels.
The **Annotation method** selector always offers Multi-view Semantics, Video + Events, and Fusion + Review,
including when the dataset has no saved annotations. It is synchronized with the new-task method selector
and remembers the choice in this browser; an explicit `?scheme=` link takes precedence.

Choose a method, then a **Saved episode / result** to review an earlier run. Switching methods keeps the
same episode when available and prefers `refined`, `caption`, or `fusion_reviewed`; intermediate results
remain selectable. If no result exists, the old video and annotations are cleared. To reuse the method,
enter a new episode index and select **Annotate one episode**. Each job retains its method ID and saves
separate artifacts; it does not overwrite earlier trials. Completed jobs refresh the saved-result list.
Executor readiness is reported per method, including unsupported Agent versions and read-only roles.

- **Multi-view Semantics** (`multiview_semantics`): the independent image/pose workflow described above,
  with `coarse` and `refined` results. Scene labels, phase labels and instruction success are not fabricated
  when this scheme did not produce them.
- **Video + Events** (`video_events`): the demo-inspired temporal video sequence, a compact trajectory
  digest at up to 24 sampled timestamps, and gripper transition candidates. Produces `caption`, including
  scene, active arm, phase, bilingual episode caption and a model assessment of instruction compliance.
  The integrated runner treats raw gripper midrange crossings as rise/fall candidates; it does not assume
  that increasing values always mean closing or that a threshold crossing proves contact. Pose units
  remain source units. This makes the adaptation explicit rather than silently adopting rig conventions.

Both run against an explicitly exported **single episode** using the same CLI:

```bash
python -m lerobot.data_platform.temporal_caption infer \
  --scheme video_events --bundle /path/to/episode-bundle --dataset-key "$DATASET_KEY" \
  --output "$CAPTION_ROOT/$CAPTION_KEY/video_events_trial_001"
```

Existing completed demo results can also be imported without rerunning a model:

```bash
python -m lerobot.data_platform.temporal_caption import-demo \
  --source /path/to/demo/episode-directory --dataset-key "$DATASET_KEY" \
  --output "$CAPTION_ROOT/$CAPTION_KEY/demo_trial_001"
```

The importer reads `caption.json`, `episode_meta.json`, `traj.csv`, and `combined.mp4`; rejects disagreement
between episode identities/counts/FPS; preserves the original caption; and converts **inclusive** demo
`end_frame` values to the review contract's **exclusive** ends. It does not alter the demo directory.
Per-segment confidence absent from the demo stays `not_reported`; episode-level quality confidence is not
reused as segment confidence. Model quality assessments do not approve labels or delete data.

## Remote annotation jobs and existing result import

On a registered remote dataset, the review page offers one episode index and either named scheme.
Operators and administrators can submit `caption.annotate`; viewers can only review results. The page
shows queued/running/error/cancelled states, execution messages, and available cancel/retry actions.
Submission recovery uses an owner- and input-scoped idempotency key. Retrying a failed/cancelled task
may call the model again and incur additional usage; provider requests already sent cannot be recalled.

Deploy Server A first, then the Agent from the same immutable development release. Configure
`DASHSCOPE_API_KEY` (optionally `DASHSCOPE_BASE_URL`) in the Agent's protected service environment and
restart that development Agent. Its heartbeat advertises `caption_protocol=1` and credential availability
as a boolean. The key never travels through job options, result artifacts, or the browser. The Agent needs
FFmpeg and the dataset dependencies; currently only v3.0 datasets with 1–4 embedded image cameras are
supported. Raw source access is restricted to the Agent's registered allowed roots.

Execution prepares exactly one episode outside the dataset, calls Qwen3.8-max, and retains upload files
in the durable execution spool. Server A accepts only the current node/attempt/lease and allowlisted
artifact names. It checks queued dataset, episode, scheme, full frame coverage, FPS and required variants
before atomically publishing a complete run. Upload/acknowledgement retries do not rerun inference.
Existing runs remain immutable; source metadata and labels are never rewritten.

Administrators can use **Import existing results** on the same page. A portable ZIP contains only
`<run-name>/episode.json`, `<run-name>/video.mp4`, and either `coarse.json` + `refined.json` or `caption.json`.
Episode metadata must include `episode_index`, `frame_count`, and `fps`. The importer validates all runs,
rebinds the preview dataset key to the selected registered location, and records the original key.
Identical imports are idempotent; conflicting existing runs are rejected. Importing existing results does
not call Qwen. A privileged deployment operator can also import using the deployed server Python:

```bash
sudo /opt/data-platform/dev/current/.venv/bin/python \
  -m lerobot.data_platform.temporal_caption_jobs --env dev \
  --location-id "$LOCATION_ID" --archive /path/to/review-results.zip
```

This command loads the selected environment configuration, switches to its service account, resolves
the registered location, writes to the configured caption root (or console lifecycle directory), and
records the import in the existing operation log. Do not copy the trial-key directory verbatim into a
registered dataset's result directory: the internal dataset identity must be rebound as well.

## Fusion + Review

`fusion_review` adds two results, `fusion_draft` and `fusion_reviewed`, to the same UI and remote job.
The independent visual and video/event hypotheses receive no candidate captions from one another.
The pipeline records a global object/event ledger, two hypotheses, dense evidence around their combined
boundaries and measured gripper transitions, and a fused draft. Dense evidence is split into packets;
individual camera panels are supplied alongside synchronized views, at their original source resolution.

A separate blind audit observes an offset, denser sample without seeing the hypotheses or draft. A review
then compares this event ledger, images and draft. Findings trigger denser observations of the indicated
intervals, targeted repair and another review (at most two repair cycles). Unaffected segments are frozen.
The final result retains unresolved findings rather than claiming a verified ground truth. Each segment
includes an inclusive uncertainty interval for its chosen start boundary. Coverage and uncertainty bounds
are validated in code; semantic accuracy still requires human judgment.

The page exposes draft/final choices, review status, remaining findings, before/after repairs and stage
receipts. `no_issues_detected` means only that this model review found no further issue. All model stages
currently use the same Qwen model with independent requests and thinking disabled. This is not independent
human validation or a demonstrated accuracy improvement over either baseline.

The current runner supports short episodes (about 53 seconds at 30 fps for the global image budget).
CLI inference can resume completed stage receipts only when dataset identity, source digest, model,
pipeline version and each stage's prompt/sampling signature match. A remote job retry may still repeat
inference because it starts a fresh isolated execution. No API keys are stored in receipts.
