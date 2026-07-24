from pathlib import Path

from lerobot.data_platform.precompute.preprocess.common import PreprocessResult, ProgressCallback, emit, load_json, write_json


FLAG_ISSUE_TYPES = {"quality_flag", "tagging_prompt_behavior", "object_labeling"}


def _load_json_any(path: Path):
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def _flagged_count(payload) -> int:
    if not isinstance(payload, dict):
        return 0
    values = payload.get("flagged_episodes")
    return len(values) if isinstance(values, list) else 0


def _cleared_flag_payload(path: Path, previous: dict) -> dict:
    payload = {"flagged_episodes": []}
    if path.name != "flagged_episodes.json" or isinstance(previous.get("flag_reasons"), dict):
        payload["flag_reasons"] = {}
    summary = previous.get("summary") if isinstance(previous.get("summary"), dict) else {}
    if summary:
        next_summary = dict(summary)
        for key, value in list(next_summary.items()):
            if isinstance(value, int) and (key.endswith("_count") or key in {"episodes_scanned", "flagged_episode_count"}):
                next_summary[key] = 0
        payload["summary"] = next_summary
    return payload


def clear_all_flags(
    root: Path | None,
    static_dir: Path,
    progress_callback: ProgressCallback = None,
    repo_id: str | None = None,
) -> PreprocessResult:
    root = Path(root).expanduser() if root is not None else None
    static_dir = Path(static_dir).expanduser()
    if repo_id is None and root is not None:
        info = load_json(root / "meta" / "info.json")
        repo_id = str(info.get("repo_id") or root.name)
    if repo_id is None:
        manifest = _load_json_any(static_dir / "viewer_manifest.json")
        repo_id = str(manifest.get("repo_id") or static_dir.parent.name) if isinstance(manifest, dict) else static_dir.parent.name
    static_dir.mkdir(parents=True, exist_ok=True)

    emit(progress_callback, status="running", current=0, total=2, message="Clearing flag sidecars")
    flag_paths = {static_dir / "flagged_episodes.json"}
    flag_paths.update(path for path in static_dir.glob("*_flagged_episodes.json") if path.is_file())

    cleared_episode_refs = 0
    files_updated = 0
    for path in sorted(flag_paths):
        previous = _load_json_any(path)
        cleared_episode_refs += _flagged_count(previous)
        write_json(path, _cleared_flag_payload(path, previous if isinstance(previous, dict) else {}))
        files_updated += 1

    emit(progress_callback, status="running", current=1, total=2, message="Clearing flag annotation issues")
    issues_path = static_dir / "annotation_issues.json"
    removed_issues = 0
    if issues_path.is_file():
        issues = _load_json_any(issues_path)
        if isinstance(issues, list):
            retained = []
            for issue in issues:
                if isinstance(issue, dict) and str(issue.get("type") or "") in FLAG_ISSUE_TYPES:
                    removed_issues += 1
                    continue
                retained.append(issue)
            write_json(issues_path, retained)

    summary = {
        "flag_files_updated": files_updated,
        "cleared_episode_refs": cleared_episode_refs,
        "annotation_issues_removed": removed_issues,
    }
    emit(progress_callback, status="done", current=2, total=2, message="All flags cleared")
    return PreprocessResult(
        op="clear_flags",
        src_roots=[root] if root is not None else [],
        out_root=root or static_dir.parent,
        repo_id=repo_id,
        summary=summary,
    )
