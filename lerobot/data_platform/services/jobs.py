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

"""Bounded job presentation shared by console routes."""

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ConsoleJobService:
    control_plane_store: Any

    def job_timing_snapshot(self, job: dict, now: float | None = None) -> tuple[int, int | None]:
        started_at = job.get("started_at")
        if started_at is None:
            return 0, None

        end_at = job.get("finished_at") or now or time.time()
        elapsed_seconds = int(max(0, end_at - started_at))
        status = job.get("status")
        if status in {"done", "error"}:
            return elapsed_seconds, 0

        current = int(job.get("current") or 0)
        total = int(job.get("total") or 0)
        if status == "running" and current > 0 and total > current:
            eta_seconds = int((elapsed_seconds / current) * (total - current))
            return elapsed_seconds, max(0, eta_seconds)
        return elapsed_seconds, None

    def serialize_job(self, job: dict, *, persistent: dict | None = None, actor: dict | None = None) -> dict:
        controls = {}
        control_id = job.get("control_job_id") or job.get("persistent_job_id")
        if self.control_plane_store is not None and control_id:
            from lerobot.data_platform.local_execution import local_job_payload

            if persistent is None:
                persistent = self.control_plane_store.job_manager.decorate(
                    self.control_plane_store.get_job(control_id), actor
                )
            job = local_job_payload(self.control_plane_store, persistent)
            controls = {
                key: persistent.get(key)
                for key in (
                    "requested_by",
                    "requested_by_username",
                    "revision",
                    "phase",
                    "action_reasons",
                    "queue_ahead",
                )
            }
            controls["control_job_id"] = control_id
            controls["available_actions"] = (
                [action for action in persistent["available_actions"] if action in {"retry", "cancel"}]
                if actor and persistent.get("requested_by") == actor["user_id"]
                else []
            )
        elapsed_seconds, eta_seconds = self.job_timing_snapshot(job)
        return {
            **controls,
            "id": job["id"],
            "job_type": job.get("job_type", "precompute"),
            "dataset_key": job.get("dataset_key"),
            "related_dataset_keys": job.get("related_dataset_keys", []),
            "status": job["status"],
            "progress": job.get("progress", 0),
            "current": job.get("current", 0),
            "total": job.get("total", 0),
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "message": (
                f"Queued: {controls['queue_ahead']} task(s) ahead"
                if job["status"] == "queued" and "queue_ahead" in controls
                else job.get("message", "")
            ),
            "error": job.get("error"),
            "viewer_url": job.get("viewer_url"),
            "review_url": job.get("review_url"),
            "output_root": job.get("output_root"),
            "output_dataset_key": job.get("output_dataset_key"),
            "result_summary": job.get("result_summary"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "updated_at": job.get("updated_at"),
            "logs": job.get("logs", [])[-80:],
        }

    def append_job_log(self, job: dict, message: str) -> None:
        if not message:
            return
        job.setdefault("logs", []).append({"time": time.strftime("%H:%M:%S"), "message": message})
        if len(job["logs"]) > 200:
            del job["logs"][:-200]
