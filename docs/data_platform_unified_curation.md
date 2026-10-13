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

1. **Synchronize version** scans the source on its owning node. Immutable episode identities,
   metadata, task configuration, and content digests are registered centrally. No raw dataset
   transfer is needed. Existing versions and drafts remain immutable/revisioned respectively.
2. Create a review workspace. Run stage detection, quality detection, labeling, tagging,
   embedding, comparison-summary preparation, or construction preview on that node.
   Availability depends on its dataset format, signal profile, packages, and model configuration.
   Labeling/tagging support deterministic trials as well as full runs.
3. Open a generated result. Select **Use as computation input** to reuse it in later jobs;
   input files must belong to the same dataset version and location. Accept generated labels,
   tags, or stage transitions into a draft, then edit task text, boxes, tags, stages, and
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
The Quality form offers independent checks; unsupported checks show their reason. Legacy DVT
checks remain the default. New checks require the executor to advertise `quality_protocol: 1`
and the selected names in `quality_checks`, at both submission and claim. Calls without `checks`
retain legacy behavior and remain compatible with older Agents.
For compatible DVT signals, submission records the selected processing version in the job:
DVT2 when no version is selected, or the explicit override. Source identity metadata is unchanged.
Both `h10w` and `h10_w` robot names are recognized. Previously saved, unconfirmed `unknown`
profiles from failed recognition are re-evaluated for these robots when compatible action/state
signals exist, including in older Curation snapshots; no source metadata rewrite is required.
Explicit or confirmed profiles remain authoritative. GenRobot MCAP datasets containing only
end-effector poses, gripper widths and video have no joint-angle action/state signals: DVT and
S1 are not applicable, while S3 and C3 can inspect their numeric signals and media.

- **S1** detects jumps and excessive velocity in the first 16 DVT action/state dimensions,
  excluding grippers 7 and 15. Compatible DVT action/state data uses DVT2 processing by default,
  including datasets with an inferred or recorded DVT1 source profile. No profile confirmation
  is needed. Select DVT1 in Advanced settings only to use its legacy processing rules. S1 still
  requires joint signals compatible with radians. It uses actual
  timestamp differences and reports invalid timestamps separately; it does not smooth signals.
  UMI pose/quaternion signals are not subject to these joint thresholds.
- **S3** checks numeric scalar/vector features for non-finite values, missing values, and
  shape mismatch. Optional bounds or allowed codes require a field, dimension, and unit
  matching feature metadata. No physical limits are inferred from an unknown unit. Statistical
  reference distributions are not implemented in this first version.
- **C3** checks source images/videos for missing or undecodable media and black frames.
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

Open a generated result to filter evidence by detector, episode, and field/camera. Detected issues and media coverage are displayed separately. Click an issue’s episode to open
its review form; expand **Measurements** for metric and time details. Evidence includes severity,
half-open frame/time intervals, and measured values/thresholds. Existing
legacy `frames` fields keep their original semantics. Results are stored in the existing
Curation bundle as `annotation_issues.json`, `quality_flagged_episodes.json`, and
`quality_coverage.json`; publication still checks the source fingerprint and bundle checksums.

To recompute selected checks while retaining earlier evidence, open the previous combined
Quality result and select **Use as computation input**. A new immutable run replaces automatic
issues only for the selected episodes and detectors; other detectors/episodes and manual
evidence remain. Incomplete/failed media checks retain earlier camera evidence and add the
new failure. `overwrite` does not broaden a configurable run beyond this scope. An old issue
without `detector` is treated as legacy. Several input runs containing conflicting files are
still rejected; choose one combined result. Computation never changes source datasets or
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
