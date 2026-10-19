"""Bounded background source checks and retryable cache cleanup outside compute slots."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from lerobot.data_platform.execution import ExecutionSupervisor, atomic_json
from lerobot.data_platform.source_cleanup_files import (
    missing_witness,
    prune_node_job_cache,
    prune_viewer_cache,
)


class SourceCleanupWorker:
    def __init__(self, agent):
        from lerobot.data_platform.agent import AgentClient

        self.agent = agent
        self.client = AgentClient(agent.client.server_url, verify=agent.client.verify)
        self.stop = threading.Event()
        self.cursor_path = agent.state_path.parent / "source-check-cursor.json"
        self.thread = threading.Thread(target=self.run, name="source-cleanup", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=45)
        self.client.session.close()

    def request(self, method, path, **kwargs):
        return self.client._request(
            method,
            path,
            token=self.agent.state.node_token,
            timeout=(10, 60),
            headers={"X-Agent-Release": os.environ.get("DATA_PLATFORM_RELEASE", "")},
            **kwargs,
        )

    def once(self):
        cursor = ""
        if self.cursor_path.is_file():
            cursor = json.loads(self.cursor_path.read_text()).get("cursor", "")
        response = self.request("GET", "/api/agents/source-cleanup", params={"cursor": cursor})
        for check in response["checks"]:
            if self.stop.is_set():
                return
            root = Path(check["root"])
            witness = missing_witness(root, self.agent.allowed_roots)
            if witness:
                self.request(
                    "POST",
                    "/api/agents/source-cleanup/presence",
                    json={**check, "status": "missing", "witness": witness},
                )
            elif check.get("observed"):
                try:
                    exists = root.lstat()
                except OSError:
                    continue
                if exists:
                    self.request(
                        "POST", "/api/agents/source-cleanup/presence", json={**check, "status": "present"}
                    )
        atomic_json(self.cursor_path, {"cursor": response["next_cursor"]})
        task = response.get("task")
        if not task or self.stop.is_set() or ExecutionSupervisor(self.agent).pending_here():
            return
        witness = missing_witness(Path(task["root"]), self.agent.allowed_roots)
        if witness is None:
            try:
                Path(task["root"]).lstat()
            except OSError:
                return
            self.request(
                "POST",
                "/api/agents/source-cleanup/presence",
                json={
                    "location_id": task["location_id"],
                    "root": task["root"],
                    "token": task["token"],
                    "status": "present",
                },
            )
            return
        if witness != task["witness"]:
            raise RuntimeError("Source parent filesystem changed; cache cleanup is deferred")
        try:
            if not task["node_done"]:
                from lerobot.data_platform.dataset_results import result_lock

                with result_lock(Path(task["output_dir"]), blocking=False):
                    prune_viewer_cache(
                        task, self.agent.writable_roots, state_root=self.agent.state_path.parent
                    )
                page = ""
                while not self.stop.is_set():
                    batch = self.request(
                        "GET", f"/api/agents/source-cleanup/{task['token']}/jobs", params={"cursor": page}
                    )
                    if missing_witness(Path(task["root"]), self.agent.allowed_roots) != task["witness"]:
                        return
                    prune_node_job_cache(
                        self.agent.state_path.parent,
                        task,
                        batch["job_ids"],
                        writable_roots=self.agent.writable_roots,
                    )
                    page = batch["next_cursor"]
                    if not page:
                        break
            if not self.stop.is_set():
                self.request(
                    "POST", f"/api/agents/source-cleanup/{task['token']}/ack", json={"status": "done"}
                )
        except Exception as exc:
            self.request(
                "POST",
                f"/api/agents/source-cleanup/{task['token']}/ack",
                json={"status": "error", "error": str(exc)},
            )
            raise

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.once()
                except Exception:
                    logging.exception("Source cleanup will retry")
                self.stop.wait(15)
        finally:
            self.client.session.close()
