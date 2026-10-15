# Unified Curation

Implementation baseline: `V0.0.6.1_FusionReview_20260918`. This document describes the
new source implementation; the baseline installed release is immutable and does not acquire
these changes until a new Server/Agent release is installed.

## Workflow

In the central console, select a local or Agent dataset and open **Data Curation**. Both
use the existing console header, dataset picker, and Explore, Quality, Annotation, and Dataset Build
navigation. Each operation opens in its original subtab without leaving the console. Switching
tabs preserves unsaved review fields. **Unsaved changes** identifies an edited episode review.
Changing the episode, draft or dataset, reloading a review, and replacing review content asks before
discarding edits; Cancel preserves the original selection and fields. Leaving the document also
uses the browser's unsaved-change warning. Save or reload the current review before publishing.
Failed saves retain the fields. A reason requires a review decision; the form keeps the text
and asks for a decision instead of silently dropping a reason on an undecided episode.
Read-only accounts and closed drafts have disabled review inputs.
Changing datasets opens that dataset’s own workspace.
Legacy `/curation` links redirect into the console with the selected dataset and page.
The standalone console without a control plane keeps its existing local workflows.

1. **Register dataset version** (or **Update dataset version**) scans the source on its owning node.
   Immutable episode identities,
   metadata, task configuration, and content digests are registered centrally. No raw dataset
   transfer is needed. Existing versions and drafts remain immutable/revisioned respectively.
2. Run stage detection, quality detection, labeling, tagging,
   embedding, comparison-summary preparation, or construction preview on that node.
   Availability depends on its dataset format, signal profile, packages, and model configuration.
   Labeling/tagging support deterministic trials as well as full runs. Workers, model overrides,
   thresholds and trial controls are under **Advanced settings**; their defaults are unchanged.
   Create a review workspace when you need to save episode decisions or annotations.
3. Open a generated result. The selector offers results relevant to the current view. Select
   **Use for next run** to reuse compatible results in this view; changing tabs clears these input
   selections while retaining unsaved review fields. Input files must belong to the same dataset
   version and location. Acceptance is enabled only for the currently opened annotation result.
   Accept generated labels, tags, or stage transitions into a draft, then edit task text, boxes, tags, stages, and
   keep/exclude/repair decisions. Regeneration does not overwrite draft annotations.
4. Existing reviewed sidecars can be imported explicitly. Conflicting existing draft fields
   are rejected. Temporal Caption retains all three baseline schemes and Fusion Review.
   Caption jobs are now available for local central-console datasets too. Caption evidence
   must carry a matching source fingerprint; older imported runs remain viewable but need
   regeneration before attaching them to a versioned workspace.
5. Dataset Profiles and deterministic Recipes operate on synchronized metadata even when the
   source is remote. A target episode count selects a deterministic subset. Metadata filters and grouped counts/weights support balanced sampling. The existing
   cohort, requirement, and workspace-rebase APIs remain available.
6. **Validate & publish draft** queues fresh source validation. Publication checks the
   workspace revision and submitting operator's current role. Concurrent edits or source
   changes stop publication. Only a published manifest can build a curated dataset.
7. **Build curated dataset** writes a new sibling dataset, validates it, registers lineage and
   its replica, and publishes a Viewer cache. Source Parquet, videos, and metadata are preserved.
   Construction uses existing per-format operators, including the node-local v3 adapter.
   Construction uses reviewed boxes/tags; inclusion and repair decisions are applied through
   published-manifest materialization. Review generated datasets in their own workspaces.

Operators may compute, review, publish, and build. Viewer accounts can browse saved results,
analysis, and comparisons, but cannot edit or submit jobs. Compare summaries can come from
separate nodes; builds execute against their registered source node. This does not implement
cross-node raw-file replication.

Embedding and Compare omit episode-review forms. Data Report shows saved quality evidence;
Quality Review contains detection and review controls. Episode fields appear after a workspace is
selected. Selecting an issue's episode in Data Report opens Quality Review for that episode.
Recent tasks are collapsed by default; the shared **Jobs** drawer remains available.
Temporal Caption hides version-registration controls because caption jobs do not require a
registered lifecycle version. Execution paths and access details are available under
**Execution location & access** in the console header.

## Execution and storage

- Local executor and remote Agent share `curation.*` commands on durable job protocol 2.
  Nodes advertise `curation_protocol: 1`, operation names, and model readiness.
- Commands carry a `CurationTarget` (dataset key, optional location, immutable version),
  server-resolved inputs, and whitelisted computation parameters. Agent roots and credentials
  cannot be supplied as arbitrary command options.
- The central Lifecycle ledger owns snapshots, workspaces, manifests, and published runs.
  The Agent uses a disposable builder ledger for immutable inputs, not a second authoritative
  review database. Remote root strings are never treated as Server A filesystem grants.
- Results live beside the Lifecycle ledger under `curation/runs/<job-id>`. Uploads are scoped
  to the current lease/attempt, use path validation, and are published only after file-list,
  length, checksum, operation, and target validation.
- Job submission accepts `Idempotency-Key`. Cancellation, retry, queue limits, crash recovery,
  and sibling-output staging use the existing execution supervisor. A completed compute with
  failed publication remains recoverable through that supervisor; do not rerun the transform
  over an existing output or manually mark an incomplete Viewer cache ready.
- Draft writes retain optimistic revisions and share the publication/rebase lock. Source
  fingerprints are checked before and after computation. Publication requires fresh validation.
- Embedding checkpoints must be under Agent allowed roots or the colon-separated
  `DATA_PLATFORM_MODEL_ROOTS`. Configure model credentials in the execution service environment;
  they are not stored in Curation commands. Dependencies and credentials differ by node.

## Quality checks

Quality detection uses the existing `curation.quality` operation on the dataset's owning node.
The Quality form offers independent checks; unsupported checks show their reason. Supported
joint data defaults to **Control signal consistency** and **Task & stage annotations**.
Recognized native end-effector data defaults to **End-effector motion**, **Numeric validity**,
and **Task & stage annotations** when task/stage fields are present. New checks require the
executor to advertise `quality_protocol: 1`
and the selected names in `quality_checks`, at both submission and claim. Calls without `checks`
retain legacy behavior and remain compatible with older Agents.
For compatible DVT signals, submission records the selected processing version in the job:
DVT2 when no version is selected, or the explicit override. Source identity metadata is unchanged.
Both `h10w` and `h10_w` robot names are recognized. Previously saved, unconfirmed `unknown`
profiles from failed recognition are re-evaluated for these robots when compatible action/state
signals exist, including in older Curation snapshots; no source metadata rewrite is required.
Explicit or confirmed profiles remain authoritative. GenRobot MCAP datasets containing only
end-effector poses, gripper widths and video have no joint-angle action/state signals: Control
signal consistency and Joint motion are not applicable. End-effector motion, Numeric validity
and Image & video quality inspect their poses, numeric signals and media.

- **Control signal consistency** (`control`) checks early gripper transitions (first 0.3 seconds),
  closed-state/idle-command inconsistencies, state transitions without a nearby command change,
  and short joint resets to zero. The supported joint/gripper layout is required; processing
  profiles remain in Advanced settings. Flags are candidates for human review.
- **Task & stage annotations** (`annotations`) requires `task_index` or `subtask_state`, with no
  DVT profile or joint-angle requirement. It flags multiple task texts, English comma/period
  characters in task text (a format heuristic, not semantic validation), and missing/constant
  stage values only when `subtask_state` is declared. No stage field means that stage checks
  are skipped; single-stage tasks may legitimately have constant values. Existing tagging
  mismatch evidence is imported without running new vision inference. These flags require review.
  The UI no longer offers the combined `legacy` option, but old API calls retain its behavior,
  including the missing-stage rule. Do not combine `legacy` with either split check.
- **Joint motion** (`s1`) detects jumps and excessive velocity in the first 16 DVT action/state dimensions,
  excluding grippers 7 and 15. Compatible DVT action/state data uses DVT2 processing by default,
  including datasets with an inferred or recorded DVT1 source profile. No profile confirmation
  is needed. Select DVT1 in Advanced settings only to use its legacy processing rules. S1 still
  requires joint signals compatible with radians. It uses actual
  timestamp differences and reports invalid timestamps separately; it does not smooth signals.
  UMI pose/quaternion signals are not subject to these joint thresholds.
- **End-effector motion** (`eef`) accepts named 7D XYZ + quaternion pose fields from UMI and
  GenRobot without a DVT profile. It checks invalid poses, quaternion norms and timestamp gaps,
  then marks position/orientation jumps as **candidates**, never automatic exclusions. Quaternion
  order follows feature names; `q` and `-q` represent the same orientation. Each pose field uses
  its own baseline: a step must exceed both the selected episodes' P99 and 8 times its local
  31-step median. Advanced JSON accepts `eef.quantile`, `eef.local_ratio`, `eef.window` (odd,
  3–301), and `eef.quaternion_tolerance` (default 0.001). Results record thresholds, the selected
  baseline scope, peak frame and speed using actual time deltas. Position measurements retain
  source units; undeclared units do not imply meters. Changing the episode selection recalibrates
  the baseline, so partial runs can produce different candidates. Invalid-time windows do not
  produce speed judgments. Normal gripper opening/closing is not a pose jump; use S3 for invalid
  values and declared-unit bounds. Raw tracking confidence and cross-stream synchronization
  cannot be assessed without their source fields. The executor makes two numeric-only streaming
  passes, using temporary disk arrays for exact quantiles and preserving episode boundaries
  across v3 shards. EEF + S3 shares the second pass; no whole-dataset image load is required.
- **Numeric validity** (`s3`) checks numeric scalar/vector features for non-finite values, missing values, and
  shape mismatch. Optional bounds or allowed codes require a field, dimension, and unit
  matching feature metadata. No physical limits are inferred from an unknown unit. Statistical
  reference distributions are not implemented in this first version.
- **Image & video quality** (`c3`) checks source images/videos for missing or undecodable media and black frames.
  Blur and freeze detections are review candidates, not task-failure judgments. Pixel statistics
  use grayscale images at 160 by 120 pixels, with thresholds recorded in the result.
  Video decoding uses one native thread per stream and a separate pool of 1–16 video workers.
  Pending stream tasks are bounded. v3 shared videos are inspected only within the episode's
  `from_timestamp`/`to_timestamp` range; Viewer preview caches are not the detection source.

Enable C3 explicitly. Its default `sampled` mode examines frames at one-second intervals plus
the last frame. Videos are decoded sequentially to detect decoder failures, but visual metrics
are computed only for sampled frames. `full` computes metrics for every frame. Coverage reports
the checked intervals, counts, fraction, and `complete`, `sampled`, `incomplete`, or `error`
status for every camera. Sampled results do not certify unchecked frames. Media paths must
resolve inside the registered dataset; an external video symlink is reported as a media error.

Advanced thresholds use a versioned JSON object, for example:

```json
{
  "schema_version": 1,
  "s1": {"max_step": 0.5, "max_speed": 20.0},
  "s3": {"bounds": []},
  "c3": {
    "sample_every_seconds": 1.0,
    "black_mean": 4.0,
    "black_pixel": 16.0,
    "black_fraction": 0.98,
    "blur_variance": 20.0,
    "freeze_delta": 0.5,
    "freeze_seconds": 2.0
  }
}
```

S1 step and speed are in radians and radians/second. An S3 bound can be
`{"field":"sensor","dimension":0,"unit":"m","min":0,"max":1}` when that feature declares
`unit: "m"`. For coded values, declare `unit: "unitless"` in metadata and use `allowed_values`.
Unknown options, non-finite thresholds, invalid dimensions, and mismatched units are rejected
before a job is accepted, and validated again on the execution node.

The Quality tab follows **Run quality checks → Inspect results → Review episodes**.
Each check has a plain-language description and a visible availability reason. Episode selection
stays in the main form; workers, recomputation and custom JSON thresholds are under **Advanced
settings**. Joint-profile confirmation appears only when needed, and video coverage settings
appear when C3 is selected. **Task progress** is collapsible and uses the existing Jobs flow.
The result picker shows Quality runs on this tab. Review uses the shared workspace; switching
tabs preserves unsaved fields. Task text, stage, tag and box annotation controls remain on
other Curation tabs. Detection and review do not edit the source; apply reviewed changes through
Dataset Build.

**Quality → Data Report** opens the newest saved Quality result for the selected dataset version.
The result picker also opens earlier runs. The report shows dataset episode/frame counts and duration,
the number of episodes checked, flagged episodes, detected issues, the checks used, and video coverage.
Sampled video, incomplete streams, and missing video evidence are identified separately; zero flags
does not certify unchecked data. Saved evidence can include input results from earlier runs.
The dataset overview has a **Data report** shortcut. **Open Episode Viewer** is available above the
report when a Viewer cache is registered; operators without a cache get **Prepare Viewer cache**.
Read-only users can open reports and prepared Viewer results. Clicking an issue's episode expands
the existing review form, preserving draft edits when switching between report and review tabs.
This report tab is available in the central full console; standalone and visualize-only modes retain
their existing workflows. It uses the existing version-scoped Curation results and access checks.

Open a generated result to filter evidence by detector, episode, and field/camera. Detected issues and media coverage are displayed separately. Click an issue’s episode to open
its review form; **Open in Viewer** opens the selected candidate interval in a new tab at its
starting time and pauses playback, preserving the Quality page and draft. Normal Viewer
entry retains autoplay; episode navigation clears the candidate position. Choose another interval in the row to
inspect another candidate. If no Viewer cache is registered, prepare it first. The review form
also links to the selected episode. Save Keep, Exclude, Needs review or a repair proposal in a
review workspace after inspection; opening Viewer alone does not save a decision. Read-only
users can inspect results and Viewer, but cannot run checks or save decisions. Expand
**Measurements** for metric and time details. Evidence includes severity,
half-open frame/time intervals, and measured values/thresholds. Existing
legacy `frames` fields keep their original semantics. Results are stored in the existing
Curation bundle as `annotation_issues.json`, `quality_flagged_episodes.json`, and
`quality_coverage.json`; publication still checks the source fingerprint and bundle checksums.

To recompute selected checks while retaining earlier evidence, open the previous combined
Quality result in Quality Review and select **Use for next run**. A new immutable run replaces automatic
issues only for the selected episodes and detectors; other detectors/episodes and manual
evidence remain. Incomplete/failed media checks retain earlier camera evidence and add the
new failure. `overwrite` does not broaden a configurable run beyond this scope. An old issue
with a known legacy reason is matched to control or annotation checks for scoped replacement,
including records without `detector`. Unknown legacy reasons are preserved by split reruns.
Historical evidence is displayed under the corresponding readable check name. Several input
runs containing conflicting files are still rejected; choose one combined result. Computation never changes source datasets or
draft inclusion/repair decisions. Viewer cache synchronization and interval highlighting
remain separate workflows and are not implied by Curation result publication.

## Validation and rollout

Targeted tests cover v2.1 and shared-shard v3.0 snapshots, source-change rejection,
Operator/Viewer roles, stale attempts, revision conflicts, input-result binding, review edits,
local durable execution, construction/materialization lineage, source preservation, and Viewer
registration. Model-backed tests use controlled responses; they do not certify a deployed GPU,
checkpoint, or paid model endpoint.

Browser acceptance uses a temporary Flask/control-plane fixture, checking all four tabs,
draft save/reload, preservation of unsaved text and optimistic revision on refresh, concurrent
edits, profile/recipe creation, desktop and
narrow layouts, and read-only Viewer controls. This is distinct from live dev acceptance.

Build a new immutable release using the normal release workflow in
[data_platform_environments.md](data_platform_environments.md). Install Server A and its local
executor before Agents; confirm matching release/environment heartbeats and protocol 1
capabilities before enabling Curation execution. Keep dev/prod databases and source datasets
separate. Never modify `V0.0.6.1_FusionReview_20260918` in place or claim a source test is a
successful deployment. Validate a real dev sample and model configuration before production
promotion.
