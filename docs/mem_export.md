# Offline MEM dataset export

Run `python -m lerobot.data_platform.mem_export` on the host that can read the original
LeRobot dataset and its selected Temporal Caption artifact. This is an offline CLI; it does
not register a Job, add a web route, modify Viewer caches, or mark model review as human approval.
Its companion consumer is `high-level-policy`'s `python -m mem_tools`.

Create an explicit selection manifest (paths below are examples):

```json
{
  "schema": "mem_export_selection/v1",
  "episodes": [{
    "root": "/data/robot/dataset-v1",
    "dataset_key": "COPY_EXACT_DATASET_KEY_FROM_ARTIFACT",
    "episode_index": 25,
    "cameras": ["observation.images.head", "observation.images.left_wrist", "observation.images.right_wrist"],
    "task": "Put the two markers into the holder",
    "annotation": "/data/caption-runs/selected-run/fusion_reviewed.json",
    "dataset_version_id": "COPY_LIFECYCLE_VERSION_ID",
    "episode_uid": "COPY_STABLE_EPISODE_UID",
    "lineage_group": "ORIGINAL_RECORDING_AND_EPISODE_ID"
  }]
}
```

Use real camera names and the exact reviewed task goal. Do not derive the goal from an eventual
outcome caption. `dataset_version_id` and `episode_uid` must be supplied together or both omitted
for legacy datasets; never invent lifecycle identities. `lineage_group` groups the original
episode and its trims, copies, different annotation revisions and views across datasets. Omit
it if unknown: export will use a media content hash, but the high-level tool will reject that
unconfirmed grouping for validation/test gold. A content hash alone cannot find every trim or
resampling of the same source; human lineage confirmation is necessary.

Choose exactly one `refined`, `caption` or `fusion_reviewed` artifact per episode. Camera order is
part of the model input contract. Repeat manifest entries for additional full episodes, never
split an episode into independent train/test segments.

```bash
python -m lerobot.data_platform.mem_export \
  --manifest /data/mem/selection.json --output /data/mem/export-v1 --dry-run
python -m lerobot.data_platform.mem_export \
  --manifest /data/mem/selection.json --output /data/mem/export-v1
```

Dry run validates and hashes sources and decodes the first video frame of each selected camera
to estimate uncompressed RGB space. It creates no output. The estimate is not a guaranteed PNG
size and assumes stable dimensions; disk space is also checked during writing. Export materializes
one episode's image rows at a time and revalidates each source against preflight before decoding;
very large image-backed episodes still need sufficient RAM. PNG output may be much larger than
compressed source videos.

To pin a repeat run, copy each `source_snapshots` entry from dry-run output into its selection
entry as `source_files_sha256` and `annotation_sha256`. Hashing covers metadata, selected Parquet,
referenced videos/external images, and the selected annotation. Changing a shared shard can
invalidate the snapshot even when unrelated episodes changed, which is intentionally conservative.

The exporter supports v2.1 per-episode files and v3.0 shared Parquet/video shards. It filters v3
rows by episode and uses the selected episode's video start/end times with row timestamps.
Video frames use nearest decoded PTS within 0.51 frame periods; missing/misaligned timestamps
fail rather than falling back to another episode. Embedded bytes and source-contained image
paths are supported. Image-backed frames use the source table timestamp.

`mem_dataset_export/v1` contains:

- `manifest.json`: schema, selection hash, file checksums and half-open interval convention.
- `episodes.jsonl`: stable identities where available, selected task, ordered cameras, source
  hashes, raw-media fingerprint, lineage grouping and artifact references.
- `media/episode-*/frames.jsonl` and `camera-*/*.png`: raw synchronous RGB camera views with
  timestamps, dimensions and video decode timing error. No caption/frame labels are burned in.
- `annotations/*.original.json`: the selected artifact byte-for-byte; normalized `*.json` retains
  its result and model review, with `human_review: null` and explicit pending source confirmation.

Artifact dataset key, episode index, FPS, frame count and contiguous half-open segments must
match the source. These checks cannot establish that a legacy caption was generated from the
current source revision, so human source/goal confirmation remains mandatory downstream.
Segmentation/caption is a candidate annotation source, not a memory supervision target.

Outputs must be new directories outside every source dataset. Work is staged beside the final
destination and published by rename after checksums and source-change checks; failures clean
staging. Source datasets, lifecycle identities, annotation artifacts and platform state are not
modified. All exported pixels remain original views, not short-term temporal visual-memory inputs.

Validate the adapter with:

```bash
.venv/bin/python -m pytest tests/datasets/test_mem_export.py -q
.venv/bin/python -m ruff check lerobot/data_platform/mem_export.py tests/datasets/test_mem_export.py
```

Fixtures include real tiny video files and shared v3 shards with visibly distinct adjacent
episodes. Tests cover source preservation, camera order, annotation revision checks, invalid
source paths and cleanup. This verifies the offline export contract, not real model quality.
