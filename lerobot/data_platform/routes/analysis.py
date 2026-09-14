"""Analysis of Agent metadata, independent of remote Viewer preparation."""

from __future__ import annotations

from pathlib import Path

from flask import jsonify, render_template

from lerobot.data_platform.precompute.analysis import AnalysisMetadata, build_dataset_analysis
from lerobot.data_platform.precompute.viewer_manifest import load_viewer_manifest
from lerobot.data_platform.task_catalog import TaskConfigSnapshot


def build_remote_analysis(location: dict, ctx) -> dict:
    """Read the latest central metadata report; never open the remote source path or queue a job."""
    report = location.get("metadata") or {}
    meta = AnalysisMetadata.from_report(report)
    store = ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store
    snapshot = store.tasks.snapshot(location["dataset_key"]).to_dict()
    static_dir = None
    if report.get("viewer_ready") and ctx.static_dir_for_key:
        static_dir = ctx.static_dir_for_key(ctx.repo_key(location["dataset_key"]))
    manifest = load_viewer_manifest(static_dir) if static_dir is not None else None
    # A report from an older Agent may lack per-episode lengths. A matching manifest can supply them.
    if manifest:
        for row in manifest.get("episodes", []):
            record = meta.episodes.get(int(row["episode_index"]))
            if record is not None and "length" not in record and record.get("tasks") == row.get("tasks"):
                meta.episodes[int(row["episode_index"])] = {**row, **record}
    cached_snapshot = TaskConfigSnapshot.from_dict((manifest or {}).get("task_config")).to_dict()
    analysis = build_dataset_analysis(
        Path(location["root"]), meta, static_dir, task_config=snapshot, cached_task_config=cached_snapshot
    )
    analysis["task_config_stale"] = False
    for row in analysis["episodes"]:
        row["viewer_url"] = (
            f"/{location['dataset_key']}/episode_{row['episode_id']}" if static_dir is not None else None
        )
    analysis["remote_source"] = {
        "last_synced_at": location.get("last_seen_at"),
        "state": location.get("state"),
        "metadata_complete": len(meta.episodes) == meta.total_episodes
        and analysis["metadata_episode_count"] == len(meta.episodes),
    }
    if static_dir is not None and ctx.analysis_with_live_tags:
        analysis = ctx.analysis_with_live_tags(ctx.repo_key(location["dataset_key"]), analysis, static_dir)
    return analysis


def register_remote_analysis_routes(app, ctx) -> None:
    """Expose read-only analysis to every authenticated role, including viewer accounts."""
    store = ctx.control_plane_store

    @app.get("/remote/<location_id>/analysis")
    def remote_analysis_page(location_id):
        try:
            location = store.get_location(location_id)
        except KeyError:
            return jsonify(error="Remote dataset location not found"), 404
        key = location["dataset_key"]
        return render_template(
            "visualize_dataset_analysis.html",
            dataset_key=key,
            analysis_api_base=f"/api/control/locations/{location_id}/analysis",
            analysis_remote=True,
            task_configuration_enabled=True,
            home_url=f"/?remote_location_id={location_id}",
        )

    @app.get("/api/control/locations/<location_id>/analysis/summary")
    @app.get("/api/control/locations/<location_id>/analysis/episodes")
    @app.get("/api/control/locations/<location_id>/analysis/refresh")
    def remote_analysis_data(location_id):
        try:
            location = store.get_location(location_id)
            analysis = build_remote_analysis(location, ctx)
        except KeyError:
            return jsonify(error="Remote dataset location not found"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(error=f"Invalid Agent metadata: {exc}"), 400
        return jsonify(
            summary={key: value for key, value in analysis.items() if key != "episodes"},
            episodes=analysis["episodes"],
            total=len(analysis["episodes"]),
        )
