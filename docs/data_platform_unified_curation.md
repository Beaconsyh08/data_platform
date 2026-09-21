# Unified Curation

Implementation baseline: `V0.0.6.1_FusionReview_20260918`. This document describes the
new source implementation; the baseline installed release is immutable and does not acquire
these changes until a new Server/Agent release is installed.

## Workflow

In the central console, select a local or Agent dataset and open **Data Curation**. Both
use the existing console header, dataset picker, and Explore, Quality, Annotation, and Dataset Build
navigation. Each operation opens in its original subtab without leaving the console. Switching
tabs preserves unsaved review fields; changing datasets opens that dataset’s own workspace.
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
