# Single-episode temporal caption experiment

`Data Curation → Annotation → Temporal Caption` is a parallel annotation review surface with remote single-episode jobs.
It does not replace Stage & Subtask or Object Labeling, and does not apply labels to source data.
Local and Agent datasets expose this review tab under the same Data Curation navigation.
Local and remote review use the same template, result selector, synchronized camera video, segment
navigation, active caption highlighting, and optional segment loop.

## Independent inference workflow

1. Select exactly one episode by index. The trial exporter supports image-backed LeRobot v2.1/v3.0 datasets
   with 1–4 cameras and regular timestamps. It uses format-aware readers: per-episode files for v2.1,
   shared shards filtered by `episode_index` for v3.0. It verifies episode identity, frame indices and
   timestamps, and rejects output inside the source.
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
  --root /path/to/image-backed-dataset --episode 0 --output /path/to/episode-bundle
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

## FLORA: offline segmentation, captions and rewards

Select **FLORA** in the existing Annotation task form. Each episode creates one `caption.annotate`
Job/Attempt: export → TempSeg → Captioner → terminal Rewarder → dense Rewarder → validated publication.
Progress and cancellation stay in Jobs; Review opens only a complete, acknowledged run. It produces
`episode.json`, `video.mp4`, and `caption.json`, so caption upload protocol 1 and Job protocol 2 remain
unchanged. Operator version is 1.1.0; the Agent additionally advertises `caption_flora_protocol=1` and
`caption_flora_configured`. Qwen credentials and FLORA runtime readiness are independent. An old Agent,
missing runtime, unsupported view layout, or read-only account cannot submit this method.

The adapter supports image/video-backed v2.1/v3.0 episodes with named left/right wrist cameras
and an optional scene camera, a recorded task instruction, and regular FPS. Video-backed episodes
are decoded from their metadata timestamp range with frame-level alignment checks. Two-wrist inputs
use a lossless left/right montage through the official single-video YAM path; the montage instruction
and layout are recorded in `input_adaptation`. Rewarder receives the original two camera streams.
Episodes up to 600 seconds / 36000 frames are split into equal contiguous chunks within the official
120-second / 7200-frame limit for segmentation/caption only; original frame timing and chunk offsets
are retained. Chunk boundaries may split an action; terminal/dense rewards still cover the complete
episode with original cameras. No frames are padded to make short clips appear valid. Camera videos
retain source resolution and use lossless H.264 encoding rather than
cropping the compressed Review montage. Source Parquet, videos and annotations remain unchanged.
FLORA receives no action/state/pose/gripper evidence: the signal checkbox is disabled and submissions
with `include_signals=true` are rejected. The recorded task is supplied to Rewarder; TempSeg/Captioner
retain their official visual inference prompts, with the montage layout instruction for two-wrist inputs. The native Captioner currently returns English text.

Native segment seconds and the TempSeg output remain in diagnostics. Review derives half-open frame
boundaries using `ceil(time * fps)`, labels uncovered intervals, and preserves overlapping native caption
indices; it does not silently close gaps or select one overlapping caption. FLORA allows up to 500 native segments and 1000 review intervals; other methods retain their
40-interval limit. Excessive partitions fail validation rather than dropping content.

Review shows episode outcome (1–5) separately from action quality (1 failure, 2 suboptimal, 3 normal).
Each raw dense window is aligned using the official nearest sampled-frame-pair rule without tail trim,
and masked outside its actual coverage. The official merged scores and selected source windows remain
unchanged. A disagreement means two covered windows assign different numeric scores at that frame;
it is not a human failure label, and absence of disagreement does not certify correctness. Click a
highlighted interval or a score track to seek; arrow keys on a focused track move one frame. Raw model
reasoning is expandable. Terminal progress is not a calibrated success probability. No reward automatically
accepts, excludes, deletes, or relabels an episode.

### Executor configuration

Use a dedicated free 80 GB GPU for the native pipeline. The tested environments use Python 3.11,
Torch 2.11.0+cu130, Transformers 5.8.1, Accelerate 1.12.0 and PEFT 0.18.1 for Captioner; the separate
Rewarder environment uses vLLM 0.24.0, decord 0.6.0 and qwen-vl-utils 0.0.14. Keep these GPU environments
separate from the platform interpreter. No GPU/model packages are added to the Server or CLI.

Provision official code and verify offline checkpoint manifests before enabling an executor. Tested pins:

| Component | Revision |
| --- | --- |
| Captioner code, `batch_utils` plus native loader helpers | `f736ca36925ae55d2f7915993ed1bc9f87288bb3` |
| `nvidia/Flora-TempSeg` | `114a1076f70eb53f4a58ac045505db3087e79347` |
| `Qwen/Qwen3.5-9B` TempSeg base | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |
| `nvidia/Flora-Captioner` | `fef0bbfe11829ca7f3539d58da7a6a2c2f109dc6` |
| `nvidia/Flora-Rewarder`, including its `batch_utils` | `7de68d22c4a9decec5ddb7f86ddcaee8f82d3367` |

Set `DATA_PLATFORM_FLORA_RUNTIME=/etc/data-platform/flora-runtime.json` in the chosen Agent service
environment (or local executor environment). This administrator-owned JSON contains absolute paths:

```json
{
  "caption_python": "/srv/flora/caption-env/bin/python",
  "reward_python": "/srv/flora/reward-env/bin/python",
  "caption_code": "/srv/flora/caption-code",
  "weights": "/srv/flora/weights",
  "lock_dir": "/var/lib/flora/runtime",
  "gpu": 0,
  "stage_timeout_s": 3600
}
```

`weights` contains `tempseg`, `tempseg_base`, `captioner` and `rewarder`. Keep original manifests,
configuration and tokenizer files. The service account needs read/execute access to code, environments
and weights, and write access to the lock/cache directory and normal job workspace. Never grant world
write permissions. All services using this physical GPU must use the same lock directory/index; external
training jobs do not participate in this lock, so reserve the GPU operationally. Execution requires at
least 73000 MiB free and 75000 MiB total. Each stage runs in a separate process and releases the model
before the next stage. Cancellation, timeout and supervisor death terminate model workers and descendants.
Per-stage elapsed time and sampled device memory peaks are retained; peaks include vLLM's reserved cache
and are not minimum hardware requirements. The timeout also bounds waiting for the configured GPU.

The worker forces offline Hugging Face operation, redirects the pinned TempSeg base to local storage,
and does not download models or establish SSH tunnels. The platform has no client-supplied model path
or arbitrary execution command. Native inference keeps BF16 and the official sensitive FP32 parameters;
Rewarder rejects quantization and non-BF16 engine configuration.

For a standalone acceptance on the data/GPU host:

```bash
python -m lerobot.data_platform.temporal_caption prepare \
  --root /path/to/dataset --episode 0 --output /path/to/flora-bundle --flora
DATA_PLATFORM_FLORA_RUNTIME=/etc/data-platform/flora-runtime.json \
python -m lerobot.data_platform.temporal_caption infer \
  --scheme flora --bundle /path/to/flora-bundle --dataset-key namespace/registered-dataset \
  --output /path/to/new-flora-run
```

After installing the same compatible Server/Agent release, verify the configured Agent heartbeat,
submit an authorized three-camera episode through Annotation, observe all four phases in Jobs and
final acknowledgement, then verify video seeking and disagreement evidence in Review. Exercise Viewer
read-only restrictions and cancellation; keep running old Agent generations until their attempts finish.
A standalone A100 run and local browser acceptance do not establish online deployment. Publish and
accept the companion independently versioned CLI before declaring the platform upgrade complete.

## Four reusable annotation methods, one review UI

The shared page follows the caption demo layout: synchronized camera video, colored semantic timeline,
gripper event markers, measured gripper/speed curves, bilingual captions, scene and task-quality panels.
The task form offers FLORA, Multi-view Semantics, Video + Events, and Fusion + Review,
including when the dataset has no saved annotations. Review lists saved methods and remembers the choice in this browser; an explicit `?scheme=` link takes precedence.

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

The Qwen methods run against an explicitly exported **single episode** using the same CLI:

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
FFmpeg and the dataset dependencies; v2.1 and v3.0 datasets with 1–4 image/video cameras are
supported. Agents advertise `caption_dataset_versions`; v2.1 submissions require upgraded executors. Raw source access is restricted to the Agent's registered allowed roots.

Execution prepares exactly one episode outside the dataset, calls Qwen3.8-max, and retains upload files
in the durable execution spool. Server A accepts only the current node/attempt/lease and allowlisted
artifact names. It checks queued dataset, episode, scheme, full frame coverage, FPS and required variants
before atomically publishing a complete run. Upload/acknowledgement retries do not rerun inference.
Existing runs remain immutable; source metadata and labels are never rewritten.

Administrators can import existing results using the deployment CLI below. A portable ZIP contains only
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

Imported `hybrid_windows` runs use `caption.json` with the same result envelope: dataset identity,
episode index, frame count, FPS, duration, `variant: "caption"`, model, creation time, and nested
`result` containing summary, segments, and limitations. Retain the original pipeline and source
provenance. Segment intervals already use half-open `[start_frame, end_frame)` endpoints; do not
apply the inclusive-end conversion from `import-demo`. The accompanying `episode.json` and video
must describe the same episode. Flat `output/episode_*/caption.json` files need this envelope before
ZIP import; source files stay unchanged. Review shows these results as **hybrid_windows**, while
annotation tasks retain the three executable methods. On first load, Review selects an available
method when its remembered method has no results, unless the URL specifies a method or the user
changes the method while results are loading.

### Results kept on the dataset Agent

An upgraded Agent advertising `caption_read_protocol=1` also exposes existing experiment results
directly from its registered dataset. Keep each completed episode under
`output/<method>/episode_XXXXXX/`, with `caption.json` and a nonempty `video.mp4`. Add `episode.json`
to display measured signals and camera metadata. The method directory name becomes the exact Review
method ID and label; experimental methods are not added to the executable annotation task catalog.

`caption.json` may use the flat experiment format (`summary`, `segments`, `limitations`) or the
published result envelope. It must identify the matching dataset episode, frame count, FPS and model;
segments cover the complete episode using half-open intervals. `meta/info.json` and v2.1/v3.0 episode
metadata are checked. Incomplete or invalid episodes are omitted with a visible warning. Keep different
experiments in separate method directories and finish writing the episode files before refreshing Review.
The current directory contents are inspected whenever Review is opened or refreshed; no ZIP import,
publication command, registry update or inference is needed. Old experiment registries are not used
to discover results.

The Agent reads JSON and video on demand through its authenticated outbound connection. Server A
only relays bounded replies in memory and does not publish or save these experiment results. Video
byte ranges are supported; any camera-order display derivative stays in the Agent's state directory.
The original dataset and captions are unchanged. Normal web login, dataset read scopes, Agent root
containment and Linux file permissions still apply. An offline Agent cannot serve dataset-local results.
Existing published results remain supported; an imported copy identical to a dataset-local caption is
hidden from the result list to avoid duplicate methods. Removing the local files can expose that older
published copy again.

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

Global Fusion observations adapt to episode duration with at most 160 images covering the entire episode.
Dense evidence and blind audit remain packeted over the full duration. Multi-view requests use at most
200 images while retaining endpoints and candidate boundaries; longer episodes have sparser global
observations and may need more evidence packets. The former ~53-second Fusion rejection is removed.
CLI inference can resume completed stage receipts only when dataset identity, source digest, model,
pipeline version and each stage's prompt/sampling signature match. A remote job retry may still repeat
inference because it starts a fresh isolated execution. No API keys are stored in receipts.

## Annotation and review pages

`/<namespace>/<dataset>/temporal-caption` and `/remote/<location_id>/temporal-caption`
are task pages: select one of the four methods, enter an episode, then start annotation.
They retain executor readiness, task progress, cancellation, retry, and authorized ZIP import.
Standalone previews show all four methods but cannot submit work without an executor.

Append `/review` for the separate video, timeline, caption, and model-review page.
The task page links to review; completed jobs link directly to their result. The dataset
Explore tools include Temporal Caption Review alongside Viewer, Analysis, and Label Review,
including for remote datasets before Viewer preparation. Review remains inspection of model
outputs; it does not record human approval or alter the dataset.

The embedded Temporal Caption tab contains the three-method task form, executor readiness,
task progress, saved caption results, and a separate video review link. The standalone task
page uses the same controller and form. Caption tasks do not require a curation snapshot.
Generic review
workspace, generated results, episode decisions, and tasks are hidden on this tab and remain
available on other curation tabs. Caption evidence attachment lives under Dataset Build,
Profile & recipe, beside the review workspace.

Caption results and model credentials are environment-specific. Import saved result packages
into the registered location in the intended environment; releasing code does not migrate
annotation data or credentials. A disabled start button explains the selected method's
executor or role restriction.

Register dataset version (previously Synchronize version) scans source identity and metadata
for versioned review and safe materialization; it does not copy or alter source data.
Console initialization runs once, and delayed startup responses must not reset navigation
after the user has selected a page.

## Batch annotation and review controls

The inline task form accepts episode indices/ranges (for example `0,5,10-20`) or the
entire dataset. Each selected episode is a separate durable job in the shared Jobs
page. Submission retains per-episode idempotency keys and a confirmed offset in the
browser session so interrupted delivery can resume with the same selection. Changing
the selection starts a new batch. Leaving the page stops further submissions; jobs
already accepted continue. No new model requests are made merely by selecting all.
The submit button previews the job count and shows confirmed submissions while sending.
Method, episode selection and signal inputs are disabled during submission so the displayed
settings continue to match the active batch. Submission feedback is announced to screen readers;
execution progress remains in Jobs.

`include_signals` is an optional boolean job option, defaulting to true for older
clients. When false, inference receives no measured action/state, pose, gripper, or
derived motion evidence. The option is saved in the artifact and verified during
publication. An updated Agent advertising `caption_signal_options` is required.
The ordinary task UI no longer offers ZIP import, saved-result lists, or a separate
execution progress list; administrative CLI import remains available for migration.

Review separates the episode selector from the result stage/run selector. Up/down
arrows switch episodes outside other editing controls. Task outcome assessment is
optional and distinct from Fusion annotation review: missing outcome fields mean
not assessed, never failure. Viewer and caption review use left / head / right order;
legacy composite videos are reordered into a derived display cache without rewriting
the original result.

## Cross-modal Fusion V2

New `fusion_review` jobs use `pipeline_version=fusion-review-2`; existing V1 runs remain readable.
No fourth scheme is added. Upgrade both the server and executors to run V2; old executors still run V1.
The per-result pipeline version in Review identifies the implementation that actually generated it.

The new signal branch runs locally before any model hypotheses, at the full source frame rate:

- Process numeric dimensions separately so differently scaled features do not dominate each other.
- Detect jumps using the maximum of median absolute delta plus six robust MAD scales, 5% of the
  observed range, and a numerical floor. Record the actual threshold and magnitude, not a probability.
- Detect motion start/stop from delta crossing 20% of its 90th percentile (with range/numeric floors).
- Retain gripper midrange crossings as candidates, including gradual transitions; they do not imply contact.
- Suppress nearby detections per dimension and event kind within 0.15 seconds. Never bridge missing
  or non-finite samples; report unusable channels explicitly.
- Do not assume action/state dimensions correspond or infer tracking errors, physical units, contact,
  gripper polarity or success. Thresholds are heuristics and still require calibration on labeled data.

Local code merges visual, joint video/event and signal boundary candidates into bounded-span groups
of at most 0.25 seconds. It retains every source frame, feature, dimension and detector threshold.
The matrix labels temporal support, signal-only candidates, visual/joint-only support and disagreement
between boundary hypotheses. These are temporal evidence labels, not semantic verdicts. Missing signal
support is not proof that an action did not occur. All contributing frames drive dense visual sampling.

Fusion receives this matrix and the independent signal report. Visual blind audit receives neither
signals nor captions. Local signal audit identifies changes away from current segment boundaries;
these are supplied to model review, targeted repair and verification without automatically declaring
an omission. The signal audit is recomputed after repair and separately for draft/final artifacts.
All numerical analysis, candidate grouping, source attribution and audit matching use zero API calls.
Visual understanding, semantic fusion and review still use Qwen; the base model stages are unchanged, but extra candidate windows can increase evidence packets and API calls.

Review displays the version, matrix, source details, local signal report and per-segment boundary
references. Clicking a candidate seeks the video. V2 publication recomputes local evidence to detect
inconsistent artifacts. Resume also binds complete episode metadata, including the signal-input toggle.
Disabling signals produces visual-only hypotheses and explicit disabled status, never fabricated support.
Camera panels remain 224 pixels; V2 does not add a trained signal encoder,
calibrated confidence, semantic conflict classification or validated accuracy improvement.

The annotation form links directly to Review; job tracking uses the console Jobs navigation.
The signal-input checkbox has a dedicated horizontal row in both standalone and embedded forms.
