# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Legacy console submissions and worker result handling, independent of Flask requests.

Routes supply parsed payloads; execution still uses the existing audited job scheduler.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lerobot.data_platform.dataset_metadata import MetaOnlyDataset
from lerobot.data_platform.local_execution import launch_background
from lerobot.data_platform.precompute.annotation import DEFAULT_FALLBACK_STAGE_COUNT
from lerobot.data_platform.precompute.data_profile import (
    require_dataset_operation,
    require_operation,
    resolve_processing_profile,
)
from lerobot.data_platform.precompute.labeling import (
    DEFAULT_BACKEND,
    DEFAULT_BOX_THRESHOLD,
    DEFAULT_DASHSCOPE_BASE_URL,
    DEFAULT_DASHSCOPE_MODEL,
    DEFAULT_MODEL_ID,
    DEFAULT_QWEN_ENDPOINT,
    DEFAULT_QWEN_MODEL,
    DEFAULT_TEXT_THRESHOLD,
    sample_episodes_by_task_type,
)
from lerobot.data_platform.precompute.labeling import (
    get_capabilities as get_labeling_capabilities,
)
from lerobot.data_platform.precompute.viewer_manifest import (
    manifest_episode_ids,
)
from lerobot.data_platform.processing.commands import build_command
from lerobot.data_platform.routes import (
    RouteContext,
)
from lerobot.data_platform.services.cache import DatasetCacheService
from lerobot.data_platform.services.catalog import DatasetCatalog
from lerobot.data_platform.services.dataset_paths import (
    _is_dataset_root,
)
from lerobot.data_platform.services.options import _float_option, _parse_positive_int


@dataclass(frozen=True)
class SubmissionResult:
    payload: dict
    status: int = 200


@dataclass
class ConsoleSubmissionService:
    ctx: RouteContext
    catalog: DatasetCatalog
    cache: DatasetCacheService
    console_mode: str
    tab_enabled: Callable[[str], bool]
    precompute: Callable
    precompute_v3: Callable
    labeling: Callable
    delete_source_episodes: Callable
    thread_factory: Callable
    initial_episodes: list[int] | None

    def start_precompute(self, body: dict):
        dataset_key_value = str(body.get("dataset_key") or body.get("repo_id") or "").strip()
        if not dataset_key_value:
            return SubmissionResult({"error": "dataset_key is required"}, 400)
        dataset_key = self.ctx.repo_key(dataset_key_value)
        index_entry = self.catalog.state.datasets_index.get(dataset_key)
        if index_entry is None:
            return SubmissionResult({"error": f"dataset is not registered: {dataset_key_value}"}, 404)
        root_path = Path(index_entry["root"]).expanduser()
        output_dir = Path(index_entry["output_dir"]).expanduser()
        repo_id = index_entry["repo_id"]
        info = self.cache.read_light_dataset_info(root_path, output_dir) or {}
        options = body.get("options") or {}
        if not self.tab_enabled("cache"):
            return SubmissionResult(
                {"error": f"cache preparation is disabled in {self.console_mode} console mode"}, 403
            )
        if self.console_mode == "visualize":
            options = dict(options)
            for field in [
                "fix_episode_indices",
                "write_parquet",
                "write_subtask",
                "overwrite_parquet",
                "overwrite_subtask_text",
                "annotate",
            ]:
                options[field] = False
            options["visualize_only"] = True
            options["precomputed_only"] = True
        try:
            if any(
                (
                    options.get(key)
                    for key in (
                        "force_recompute_stage",
                        "annotate",
                        "write_parquet",
                        "write_subtask",
                        "overwrite_parquet",
                        "overwrite_subtask_text",
                    )
                )
            ):
                require_dataset_operation(
                    root_path, "auto_stage", data_version_override=options.get("data_version")
                )
            if options.get("embed_policy"):
                require_operation(info, "embedding")
            if info.get("robot_type", "").lower() == "umi" and options.get("data_version"):
                raise ValueError("UMI data cannot use a DVT processing profile")
            fallback_stage_count = int(options.get("fallback_stage_count") or DEFAULT_FALLBACK_STAGE_COUNT)
            if fallback_stage_count < 2:
                raise ValueError("fallback_stage_count must be at least 2")
        except ValueError as exc:
            return SubmissionResult({"error": str(exc)}, 400)
        selected_episodes = self.ctx.parse_int_list(options.get("episodes"))
        downsample_opt = options.get("downsample")
        downsample_opt = int(downsample_opt) if downsample_opt not in (None, "") else None
        data_version = resolve_processing_profile(
            root_path, data_version_override=options.get("data_version")
        ).legacy_data_version
        total_episodes = (
            len(selected_episodes) if selected_episodes is not None else int(info.get("total_episodes") or 0)
        )
        self.catalog.state.server_state["max_frames"] = None
        self.catalog.state.server_state["downsample"] = downsample_opt
        self.catalog.state.server_state["prepare_videos"] = self.ctx.bool_option(
            options, "prepare_videos", True
        )
        self.catalog.state.server_state["precomputed_only"] = self.ctx.bool_option(
            options, "precomputed_only", True
        )
        self.catalog.state.server_state["data_version"] = data_version
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": job_id,
            "dataset_key": self.ctx.repo_id_from_key(dataset_key),
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": total_episodes,
            "message": "Queued",
            "error": None,
            "viewer_url": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "logs": [],
        }
        with self.ctx.jobs_lock:
            self.ctx.jobs_registry[job_id] = job

        def _update_job(payload: dict) -> None:
            self.ctx.update_job(job, payload)

        def _run_job() -> None:
            try:
                _update_job({"status": "running", "message": "Starting precompute"})
                _update_job(
                    {
                        "status": "running",
                        "message": f"Using robot/Stage profile: {data_version}; force stage recompute: {self.ctx.bool_option(options, 'force_recompute_stage', False)}; overwrite CSV: {self.ctx.bool_option(options, 'overwrite_csv', False)}",
                    }
                )
                dataset_format_version = str(info.get("dataset_format_version") or "")
                source_is_v3 = dataset_format_version.lower().removeprefix("v").startswith("3.")
                if source_is_v3:
                    _update_job(
                        {"status": "running", "message": "Using the read-only v3.0 viewer cache adapter"}
                    )
                    result = self.precompute_v3(
                        root=root_path,
                        repo_id=repo_id,
                        episodes=selected_episodes,
                        output_dir=output_dir,
                        prepare_videos=self.ctx.bool_option(options, "prepare_videos", True),
                        prepare_csv=self.ctx.bool_option(options, "prepare_csv", True),
                        workers=_parse_positive_int(options.get("prepare_workers"), 8),
                        downsample=downsample_opt,
                        overwrite_videos=self.ctx.bool_option(options, "overwrite", False),
                        overwrite_csv=self.ctx.bool_option(options, "overwrite_csv", False),
                        data_version=data_version,
                        progress_callback=_update_job,
                        fallback_stage_count=fallback_stage_count,
                        force_recompute_stage=self.ctx.bool_option(options, "force_recompute_stage", False),
                    )
                else:
                    result = self.precompute(
                        root=root_path,
                        repo_id=repo_id,
                        episodes=selected_episodes,
                        image_keys=None,
                        output_dir=output_dir,
                        prepare_videos=self.ctx.bool_option(options, "prepare_videos", True),
                        prepare_csv=self.ctx.bool_option(options, "prepare_csv", True),
                        prepare_workers=_parse_positive_int(options.get("prepare_workers"), 8),
                        max_frames=None,
                        downsample=downsample_opt,
                        overwrite=self.ctx.bool_option(options, "overwrite", False),
                        overwrite_csv=self.ctx.bool_option(options, "overwrite_csv", False),
                        fix_episode_indices_enabled=self.ctx.bool_option(
                            options, "fix_episode_indices", False
                        ),
                        annotate=self.ctx.bool_option(options, "annotate", False),
                        write_parquet=self.ctx.bool_option(options, "write_parquet", False),
                        force_recompute_stage=self.ctx.bool_option(options, "force_recompute_stage", False),
                        fallback_stage_count=_parse_positive_int(
                            options.get("fallback_stage_count"), DEFAULT_FALLBACK_STAGE_COUNT
                        ),
                        task_config=self.catalog.lifecycle_store().tasks.snapshot(repo_id).to_dict(),
                        write_subtask=self.ctx.bool_option(options, "write_subtask", False),
                        overwrite_parquet=self.ctx.bool_option(options, "overwrite_parquet", False),
                        overwrite_subtask_text=self.ctx.bool_option(options, "overwrite_subtask_text", False),
                        visualize_only=self.ctx.bool_option(options, "visualize_only", False),
                        data_version=data_version,
                        progress_callback=_update_job,
                        show_progress=False,
                    )
                self.catalog.register_precomputed(result)
                first_episode = result.episodes[0] if result.episodes else 0
                viewer_url = f"/{result.repo_id}/episode_{first_episode}"
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "done"
                    job["progress"] = 100
                    job["current"] = job.get("total") or len(result.episodes)
                    job["total"] = job.get("total") or len(result.episodes)
                    job["message"] = "Precompute complete"
                    job["viewer_url"] = viewer_url
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, "Precompute complete")
                self.ctx.audit_job(job, "success")
            except Exception as exc:
                logging.exception("Precompute job failed")
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["message"] = "Precompute failed"
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, f"Error: {exc}")
                self.ctx.audit_job(job, "failed", exc)

        launch_background(
            target=_run_job,
            name=f"precompute-{job_id}",
            daemon=True,
            thread_factory=self.thread_factory,
            command=build_command(
                "viewer.prepare",
                self.precompute,
                root=root_path,
                repo_id=repo_id,
                episodes=selected_episodes,
                image_keys=None,
                output_dir=output_dir,
                prepare_videos=self.ctx.bool_option(options, "prepare_videos", True),
                prepare_csv=self.ctx.bool_option(options, "prepare_csv", True),
                prepare_workers=_parse_positive_int(options.get("prepare_workers"), 8),
                max_frames=None,
                downsample=downsample_opt,
                overwrite=self.ctx.bool_option(options, "overwrite", False),
                overwrite_csv=self.ctx.bool_option(options, "overwrite_csv", False),
                fix_episode_indices_enabled=self.ctx.bool_option(options, "fix_episode_indices", False),
                annotate=self.ctx.bool_option(options, "annotate", False),
                write_parquet=self.ctx.bool_option(options, "write_parquet", False),
                force_recompute_stage=self.ctx.bool_option(options, "force_recompute_stage", False),
                fallback_stage_count=_parse_positive_int(
                    options.get("fallback_stage_count"), DEFAULT_FALLBACK_STAGE_COUNT
                ),
                task_config=self.catalog.lifecycle_store().tasks.snapshot(repo_id).to_dict(),
                write_subtask=self.ctx.bool_option(options, "write_subtask", False),
                overwrite_parquet=self.ctx.bool_option(options, "overwrite_parquet", False),
                overwrite_subtask_text=self.ctx.bool_option(options, "overwrite_subtask_text", False),
                visualize_only=self.ctx.bool_option(options, "visualize_only", False),
                data_version=data_version,
                show_progress=False,
            ),
        )
        return SubmissionResult({"job": self.ctx.serialize_job(job)}, 200)

    def start_labeling(self, body: dict):
        options = body.get("options") or {}
        backend = str(options.get("backend") or DEFAULT_BACKEND).strip() or DEFAULT_BACKEND
        capabilities = get_labeling_capabilities()
        backend_capabilities = capabilities.get("backends", {}).get(backend)
        if backend_capabilities is None:
            return SubmissionResult({"error": f"Unknown detector backend: {backend}"}, 400)
        if not backend_capabilities.get("available"):
            if backend == "qwen_remote":
                install_hint = "Object labeling with Remote Qwen requires `gradio-client`."
                pip_hint = "Install via `pip install gradio-client`."
            else:
                install_hint = "Object labeling requires `transformers` and `torch`."
                pip_hint = "Install via `pip install transformers torch torchvision`."
            return SubmissionResult(
                {"error": f"{install_hint} {pip_hint}", "details": backend_capabilities.get("error")}, 400
            )
        dataset_key_value = str(body.get("dataset_key") or body.get("repo_id") or "").strip()
        if not dataset_key_value:
            return SubmissionResult({"error": "dataset_key is required"}, 400)
        dataset_key = self.ctx.repo_key(dataset_key_value)
        try:
            dataset_obj, ds_static = self.ctx.ensure_dataset_loaded(dataset_key)
        except KeyError:
            return SubmissionResult({"error": f"dataset is not registered: {dataset_key_value}"}, 404)
        except Exception as exc:
            logging.exception("Failed to load dataset for object labeling %s", dataset_key_value)
            return SubmissionResult({"error": str(exc)}, 400)
        selected_episodes = self.ctx.parse_int_list(options.get("episodes"))
        model_id = str(options.get("model_id") or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
        default_endpoint = (
            DEFAULT_DASHSCOPE_BASE_URL if backend == "qwen_dashscope" else DEFAULT_QWEN_ENDPOINT
        )
        default_qwen_model = DEFAULT_DASHSCOPE_MODEL if backend == "qwen_dashscope" else DEFAULT_QWEN_MODEL
        endpoint = str(options.get("endpoint") or default_endpoint).strip() or default_endpoint
        qwen_model = str(options.get("qwen_model") or default_qwen_model).strip() or default_qwen_model
        qwen_token = str(options.get("qwen_token") or "").strip() or None
        if (
            backend_capabilities.get("requires_token")
            and (not qwen_token)
            and (not backend_capabilities.get("token_configured"))
        ):
            token_env = (backend_capabilities.get("token_env_vars") or ["DASHSCOPE_API_KEY"])[0]
            return SubmissionResult(
                {"error": f"{backend} requires a token. Set {token_env} or paste it in the token field."}, 400
            )
        min_pixels = int(options.get("min_pixels") or 1024)
        max_pixels = int(options.get("max_pixels") or 9800)
        workers = max(1, int(options.get("workers") or 8))
        devices = str(options.get("devices") or "").strip()
        box_threshold = _float_option(options, "box_threshold", DEFAULT_BOX_THRESHOLD)
        text_threshold = _float_option(options, "text_threshold", DEFAULT_TEXT_THRESHOLD)
        save_vis = self.ctx.bool_option(options, "save_vis", False)
        trial = self.ctx.bool_option(options, "trial", False)
        labeling_run_mode = str(options.get("run_mode") or "missing").strip().lower()
        if labeling_run_mode not in {"missing", "full"}:
            return SubmissionResult({"error": "labeling run_mode must be 'missing' or 'full'"}, 400)
        trial_per_type = max(1, int(options.get("trial_per_type") or 20))
        trial_seed_value = options.get("trial_seed")
        trial_seed = int(trial_seed_value) if trial_seed_value not in (None, "") else int(time.time())
        self.catalog.state.server_state["annotate"] = self.ctx.bool_option(options, "annotate", True)
        output_variant = None
        sample_result = None
        if trial:
            sample_result = sample_episodes_by_task_type(
                dataset_obj.root,
                dataset_obj.meta,
                per_type=trial_per_type,
                episodes=selected_episodes,
                seed=trial_seed,
            )
            selected_episodes = sample_result.episodes
            output_variant = f"{backend}_trial"
            if not selected_episodes:
                return SubmissionResult({"error": "No supported episodes found for trial labeling."}, 400)
        total = (
            len(selected_episodes)
            if selected_episodes is not None
            else len(self.ctx.dataset_episode_ids(dataset_obj, dataset_key))
        )
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        repo_id = self.ctx.repo_id_from_key(dataset_key)
        job = {
            "id": job_id,
            "job_type": "labeling",
            "dataset_key": repo_id,
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": total,
            "message": "Queued",
            "error": None,
            "viewer_url": None,
            "review_url": f"/{repo_id}/labeling" + (f"?variant={output_variant}" if output_variant else ""),
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "logs": [],
        }
        with self.ctx.jobs_lock:
            self.ctx.jobs_registry[job_id] = job

        def _update_job(payload: dict) -> None:
            self.ctx.update_job(job, payload)

        def _run_job() -> None:
            try:
                start_message = (
                    f"Starting object labeling ({backend}, mode={labeling_run_mode}, workers={workers})"
                )
                if trial and sample_result is not None:
                    start_message = f"Starting trial object labeling ({backend}, mode={labeling_run_mode}, workers={workers}, per_type={trial_per_type}, seed={sample_result.seed}, sampled={len(selected_episodes)}, counts={sample_result.counts})"
                _update_job({"status": "running", "message": start_message})
                result = self.labeling(
                    root=dataset_obj.root,
                    meta=dataset_obj.meta,
                    episodes=selected_episodes,
                    static_dir=ds_static,
                    backend=backend,
                    model_id=model_id,
                    endpoint=endpoint,
                    qwen_model=qwen_model,
                    qwen_token=qwen_token,
                    min_pixels=min_pixels,
                    max_pixels=max_pixels,
                    box_threshold=box_threshold,
                    text_threshold=text_threshold,
                    save_vis=save_vis,
                    workers=workers,
                    devices=devices,
                    output_variant=output_variant,
                    run_mode=labeling_run_mode,
                    progress_callback=_update_job,
                    show_progress=False,
                )
                refreshed = MetaOnlyDataset(dataset_obj.repo_id, root=dataset_obj.root)
                self.ctx.register_dataset(refreshed, ds_static.parent, None if trial else result.episodes)
                review_url = f"/{result.repo_id}/labeling" + (
                    f"?variant={output_variant}" if output_variant else ""
                )
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "done"
                    job["progress"] = 100
                    job["current"] = len(result.episodes)
                    job["total"] = len(result.episodes)
                    job["message"] = "Object labeling complete"
                    job["review_url"] = review_url
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, "Object labeling complete")
                self.ctx.audit_job(job, "success")
            except Exception as exc:
                logging.exception("Object labeling job failed")
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["message"] = "Object labeling failed"
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, f"Error: {exc}")
                self.ctx.audit_job(job, "failed", exc)

        launch_background(
            target=_run_job,
            name=f"labeling-{job_id}",
            daemon=True,
            thread_factory=self.thread_factory,
            command=build_command(
                "curation.labeling",
                self.labeling,
                root=dataset_obj.root,
                meta=dataset_obj.meta,
                episodes=selected_episodes,
                static_dir=ds_static,
                backend=backend,
                model_id=model_id,
                endpoint=endpoint,
                qwen_model=qwen_model,
                qwen_token=qwen_token,
                min_pixels=min_pixels,
                max_pixels=max_pixels,
                box_threshold=box_threshold,
                text_threshold=text_threshold,
                save_vis=save_vis,
                workers=workers,
                devices=devices,
                output_variant=output_variant,
                run_mode=labeling_run_mode,
                show_progress=False,
            ),
        )
        return SubmissionResult({"job": self.ctx.serialize_job(job)}, 200)

    def preload_viewer(self, body: dict, target_url: str):
        dataset_key_value = str(body.get("dataset_key") or "").strip()
        episode_id = int(body.get("episode_id") or 0)
        if not dataset_key_value:
            return SubmissionResult({"error": "dataset_key is required"}, 400)
        dataset_key = self.ctx.repo_key(dataset_key_value)
        if (
            dataset_key not in self.catalog.state.datasets_index
            and dataset_key not in self.catalog.state.datasets_registry
        ):
            return SubmissionResult({"error": f"dataset is not registered: {dataset_key_value}"}, 404)
        now = time.time()
        job = {
            "id": uuid.uuid4().hex[:12],
            "job_type": "viewer_preload",
            "dataset_key": self.ctx.repo_id_from_key(dataset_key),
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": 4,
            "message": "Queued",
            "error": None,
            "viewer_url": target_url,
            "review_url": None,
            "output_root": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "logs": [],
        }
        with self.ctx.jobs_lock:
            self.ctx.jobs_registry[job["id"]] = job

        def _update(current: int, message: str) -> None:
            with self.ctx.jobs_lock:
                update_time = time.time()
                job["status"] = "running"
                if job.get("started_at") is None:
                    job["started_at"] = update_time
                job["current"] = current
                job["progress"] = int(current / job["total"] * 100)
                job["message"] = message
                job["updated_at"] = update_time
                elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, update_time)
                job["elapsed_seconds"] = elapsed_seconds
                job["eta_seconds"] = eta_seconds
                self.ctx.append_job_log(job, message)

        def _run_job() -> None:
            try:
                _update(1, "Loading dataset metadata")
                index_entry = self.catalog.state.datasets_index.get(dataset_key)
                manifest_adapter_index = False
                if index_entry is not None:
                    root_path = Path(index_entry["root"]).expanduser()
                    output_dir = Path(index_entry["output_dir"]).expanduser()
                    manifest_adapter_index = (
                        not _is_dataset_root(root_path)
                        and self.cache.root_has_cache_manifest(root_path, output_dir)
                        or self.catalog.dataset_index_uses_v3_adapter(dataset_key)
                    )
                manifest, manifest_static = (
                    self.catalog.manifest_for_key(dataset_key) if manifest_adapter_index else (None, None)
                )
                if manifest_adapter_index:
                    if manifest is None or manifest_static is None:
                        raise RuntimeError("Viewer cache manifest is missing; run Prepare video + CSV first")
                    _update(2, f"Checking episode {episode_id} cache")
                    episode_ids = manifest_episode_ids(manifest)
                    if episode_id not in episode_ids:
                        raise RuntimeError(f"Episode {episode_id} is not present in viewer cache")
                    _update(3, "Using cache-only viewer manifest")
                    _update(4, "Viewer ready")
                    with self.ctx.jobs_lock:
                        finished_at = time.time()
                        job["status"] = "done"
                        job["progress"] = 100
                        job["message"] = "Viewer ready"
                        job["finished_at"] = finished_at
                        job["updated_at"] = finished_at
                        elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                        job["elapsed_seconds"] = elapsed_seconds
                        job["eta_seconds"] = eta_seconds
                        self.ctx.append_job_log(job, "Viewer ready")
                    self.ctx.audit_job(job, "success")
                    return
                dataset_obj, ds_static = self.ctx.ensure_dataset_loaded(dataset_key)
                _update(2, f"Checking episode {episode_id} video/CSV cache")
                cache_for_episode = self.cache.episode_cache_status(dataset_obj, ds_static, episode_id)
                if (
                    self.catalog.state.server_state["precomputed_only"]
                    and cache_for_episode["status"] != "cached"
                ):
                    raise RuntimeError(
                        f"Viewer cache is missing: video {cache_for_episode['videos']['cached']}/{cache_for_episode['videos']['total']}, csv {cache_for_episode['csv']['cached']}/{cache_for_episode['csv']['total']}"
                    )
                _update(3, "Warming episode metadata")
                self.ctx.dataset_episode_ids(dataset_obj, dataset_key)
                _update(4, "Viewer ready")
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "done"
                    job["progress"] = 100
                    job["message"] = "Viewer ready"
                    job["finished_at"] = finished_at
                    job["updated_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    self.ctx.append_job_log(job, "Viewer ready")
                self.ctx.audit_job(job, "success")
            except Exception as exc:
                logging.exception("Viewer preload failed")
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["message"] = "Viewer preload failed"
                    job["finished_at"] = finished_at
                    job["updated_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    self.ctx.append_job_log(job, f"Error: {exc}")
                self.ctx.audit_job(job, "failed", exc)

        launch_background(
            compatibility_operator="viewer.preload",
            target=_run_job,
            name=f"viewer-preload-{job['id']}",
            daemon=True,
            thread_factory=self.thread_factory,
        )
        return SubmissionResult({"job": self.ctx.serialize_job(job)}, 200)

    def delete_episodes(self, body: dict):
        dataset_key_value = str(body.get("dataset_key") or body.get("repo_id") or "").strip()
        if not dataset_key_value:
            return SubmissionResult({"error": "dataset_key is required"}, 400)
        dataset_key = self.ctx.repo_key(dataset_key_value)
        try:
            dataset_obj, ds_static = self.ctx.ensure_dataset_loaded(dataset_key)
        except KeyError:
            return SubmissionResult({"error": f"dataset is not registered: {dataset_key_value}"}, 404)
        except Exception as exc:
            logging.exception("Failed to load dataset for episode deletion %s", dataset_key_value)
            return SubmissionResult({"error": str(exc)}, 400)
        options = body.get("options") or body
        reason = str(options.get("reason") or "").strip()
        if not reason:
            return SubmissionResult({"error": "reason is required for in-place episode deletion"}, 400)
        if len(reason) > 500:
            return SubmissionResult({"error": "reason must be 500 characters or fewer"}, 400)
        episode_ids = self.ctx.parse_int_list(options.get("episodes") or options.get("episode_ids"))
        if not episode_ids:
            return SubmissionResult({"error": "episodes is required, e.g. 0,1,2"}, 400)
        episode_ids = sorted(set(episode_ids), reverse=True)
        existing = set(self.ctx.dataset_episode_ids(dataset_obj, dataset_key))
        missing = sorted(set(episode_ids) - existing)
        if missing:
            return SubmissionResult({"error": f"episodes not found: {missing}"}, 400)
        repo_id = self.ctx.repo_id_from_key(dataset_key)
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": job_id,
            "job_type": "preprocess_delete_episodes",
            "dataset_key": repo_id,
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": len(episode_ids),
            "message": "Queued",
            "error": None,
            "viewer_url": None,
            "review_url": None,
            "output_root": str(dataset_obj.root),
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "logs": [],
        }
        with self.ctx.jobs_lock:
            self.ctx.jobs_registry[job_id] = job

        def _update_delete_job(payload: dict) -> None:
            self.ctx.update_job(job, payload)

        def _run_job() -> None:
            try:
                total = len(episode_ids)
                _update_delete_job(
                    {
                        "status": "running",
                        "current": 0,
                        "total": total,
                        "message": f"Deleting episodes {list(reversed(episode_ids))}",
                    }
                )

                def _log_job(message: str) -> None:
                    with self.ctx.jobs_lock:
                        self.ctx.append_job_log(job, message)

                result = self.delete_source_episodes(
                    dataset_obj, episode_ids, static_folder=ds_static, log=_log_job
                )
                self.cache.clear_episode_dependent_caches()
                if self.initial_episodes is not None:
                    remaining_ids = sorted(getattr(dataset_obj.meta, "episodes", {}))
                    if not remaining_ids:
                        remaining_total = int(getattr(dataset_obj, "total_episodes", 0) or 0)
                        remaining_ids = list(range(remaining_total))
                    self.initial_episodes.clear()
                    self.initial_episodes.extend(remaining_ids)
                self.ctx.append_operation_log(
                    ds_static,
                    "preprocess_delete_episodes",
                    dataset_key=dataset_key,
                    dataset_root=Path(dataset_obj.root),
                    episode_ids=list(reversed(episode_ids)),
                    details={"result": result, "job_id": job_id, "reason": reason},
                )
                _update_delete_job(
                    {
                        "status": "running",
                        "current": total,
                        "total": total,
                        "message": "Deleted selected episodes; repaired indices once",
                    }
                )
                self.cache.invalidate_light_cache_status(dataset_obj.root, ds_static.parent)
                remaining_ids = sorted(getattr(dataset_obj.meta, "episodes", {}).keys())
                if not remaining_ids:
                    remaining_total = int(getattr(dataset_obj, "total_episodes", 0) or 0)
                    remaining_ids = list(range(remaining_total))
                self.catalog.state.episodes_by_key[dataset_key] = remaining_ids
                self.catalog.state.task_caches.pop(dataset_key, None)
                first_episode = (
                    self.catalog.state.episodes_by_key[dataset_key][0]
                    if self.catalog.state.episodes_by_key[dataset_key]
                    else None
                )
                viewer_url = f"/{repo_id}/episode_{first_episode}" if first_episode is not None else None
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "done"
                    job["progress"] = 100
                    job["current"] = total
                    job["total"] = total
                    job["message"] = "Episode deletion complete"
                    job["viewer_url"] = viewer_url
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, f"Deleted episodes: {list(reversed(episode_ids))}")
                    self.ctx.append_job_log(job, f"Remaining episodes: {result['new_total_episodes']}")
                self.ctx.audit_job(job, "success")
            except Exception as exc:
                logging.exception("Episode deletion job failed")
                with self.ctx.jobs_lock:
                    finished_at = time.time()
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["message"] = "Episode deletion failed"
                    job["finished_at"] = finished_at
                    elapsed_seconds, eta_seconds = self.ctx.job_timing_snapshot(job, finished_at)
                    job["elapsed_seconds"] = elapsed_seconds
                    job["eta_seconds"] = eta_seconds
                    job["updated_at"] = finished_at
                    self.ctx.append_job_log(job, f"Error: {exc}")
                self.ctx.audit_job(job, "failed", exc)

        launch_background(
            target=_run_job,
            name=f"preprocess-delete-episodes-{job_id}",
            daemon=True,
            thread_factory=self.thread_factory,
            command=build_command(
                "mutation.delete_episodes",
                self.delete_source_episodes,
                dataset_obj,
                episode_ids,
                static_folder=ds_static,
            ),
        )
        return SubmissionResult({"job": self.ctx.serialize_job(job)}, 200)
