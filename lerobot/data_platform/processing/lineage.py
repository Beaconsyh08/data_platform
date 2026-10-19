"""Shared lineage registration for local and Agent-derived dataset versions."""

import json
from pathlib import Path

from lerobot.data_platform.precompute.dataset_io import load_episode_records


def register_preprocess_result(store, result, parents, job_type):
    summary = dict(getattr(result, "summary", {}) or {})
    excluded = {}
    deleted = summary.get("delete_episodes")
    if len(parents) == 1 and deleted:
        excluded[parents[0].version_id] = [int(item) for item in deleted]
    profile = json.loads(
        json.dumps(
            {"job_type": job_type, "summary": summary},
            default=str,
        )
    )
    lineage = []
    parent_by_root = {str(Path(parent.root).resolve()): parent for parent in parents}
    for item in list(getattr(result, "episode_lineage", None) or []):
        source_position = item.get("source_position")
        parent = None
        if source_position is not None and 0 <= int(source_position) < len(parents):
            parent = parents[int(source_position)]
        if parent is None and item.get("source_root"):
            parent = parent_by_root.get(str(Path(item["source_root"]).resolve()))
        if parent is None and len(parents) == 1:
            parent = parents[0]
        if parent is None:
            raise ValueError("preprocess result lineage does not identify its source dataset")
        lineage.append(
            {
                "source_dataset_version_id": parent.version_id,
                "source_episode_index": int(item["source_episode_index"]),
                "output_episode_index": int(item["output_episode_index"]),
            }
        )
    if not lineage and len(parents) == 1:
        deleted_indices = {int(item) for item in (summary.get("delete_episodes") or [])}
        source_indices = [
            index for index in sorted(parents[0].uid_by_index()) if index not in deleted_indices
        ]
        output_indices = sorted(int(item["episode_index"]) for item in load_episode_records(result.out_root))
        if len(source_indices) != len(output_indices):
            raise ValueError("preprocess executor changed episode cardinality without explicit lineage")
        lineage = [
            {
                "source_dataset_version_id": parents[0].version_id,
                "source_episode_index": source_index,
                "output_episode_index": output_index,
            }
            for source_index, output_index in zip(source_indices, output_indices, strict=True)
        ]
    operation = str(getattr(result, "op", None) or job_type or "preprocess")
    parent_stages = {parent.stage for parent in parents}
    if len(parent_stages) != 1:
        raise ValueError(f"cannot derive one dataset from mixed lifecycle stages: {parent_stages}")
    parent_stage = next(iter(parent_stages))
    output_stage = "curated" if parent_stage == "curated" else "standard"
    return store.register_derived(
        result.out_root,
        result.repo_id,
        parent_version_ids=[parent.version_id for parent in parents],
        operation=operation,
        profile=profile,
        stage=output_stage,
        excluded_episode_indices=excluded,
        episode_lineage=lineage,
        verify_parents=False,
    )


def register_local_preprocess(store, payload):
    """Retain the legacy local route's immutable output and episode-lineage registration."""
    from lerobot.data_platform.lifecycle import dataset_snapshot
    from lerobot.data_platform.precompute.preprocess.common import PreprocessResult

    value = dict(payload["result"])
    value["src_roots"] = [Path(root) for root in value["src_roots"]]
    value["out_root"] = Path(value["out_root"])
    result = PreprocessResult(**value)
    parents = []
    for root in result.src_roots:
        if dataset_snapshot(root)[0] != payload["fingerprints"][str(root.resolve())]:
            raise ValueError("Local preprocessing input changed before publication")
        parent = store.version_at_root(root)
        if parent is None or parent.fingerprint != payload["fingerprints"][str(root.resolve())]:
            parent = store.ingest(root, f"local/{root.name}", stage="raw", operation="backfill")
        parents.append(parent)
    return register_preprocess_result(store, result, parents, result.op)
