"""Authorized plan submission and transactional, idempotent step coordination."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select

from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.management_storage import enqueue_event
from lerobot.data_platform.operation_log import build_operation_event
from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.task_catalog import TaskConfigSnapshot, content_digest

from .models import Pipeline, PipelineRun, PipelineStepRun, PipelineVersion
from .planner import PipelineSpec, availability


def location_identity(location: dict) -> dict:
    metadata = location.get("metadata") or {}
    return {
        "root": location["root"],
        "dataset_key": location["dataset_key"],
        "digest": content_digest(
            {
                key: metadata[key]
                for key in (
                    "codebase_version",
                    "fps",
                    "total_frames",
                    "total_episodes",
                    "total_tasks",
                    "data_path",
                    "video_path",
                    "features",
                    "content_revision",
                    "dataset_revision",
                    "fingerprint",
                )
                if key in metadata
            }
        ),
        "node_id": location["node_id"],
    }


class PipelineManager:
    def __init__(self, store, lifecycle=None):
        self.store = store
        self.lifecycle = lifecycle

    def _actor(self, session, owner: str, *, write: bool = True):
        from lerobot.data_platform.control_plane import ControlPlaneUser

        actor = session.get(ControlPlaneUser, owner)
        if not actor or not actor.active or write and actor.role not in {"admin", "data_manager", "operator"}:
            raise PermissionError("An active operator account is required")
        return actor

    def _location(
        self,
        session,
        location_id: str,
        owner: str,
        node_id: str | None = None,
        *,
        write=True,
        allow_retired=False,
    ):
        from lerobot.data_platform.control_plane import ControlPlaneNode, DatasetLocation

        actor = self._actor(session, owner, write=write)
        location = session.get(DatasetLocation, location_id)
        if location is None:
            raise KeyError("Dataset location not found")
        if location.state != "available" and not allow_retired:
            raise JobConflictError("Source dataset was deleted or is being cleaned up")
        if not self.store.dataset_access_allowed(session, actor, location):
            raise PermissionError("Dataset is outside this account's data permissions")
        if node_id and location.node_id != node_id:
            raise ValueError("All pipeline inputs must belong to the same execution node")
        node = session.get(ControlPlaneNode, location.node_id)
        if node is None:
            raise KeyError("Execution node not found")
        return self.store._location_dict(location), self.store._node_dict(node)

    @staticmethod
    def _audit(session, owner, operation, **details):
        enqueue_event(
            session,
            build_operation_event(
                operation, status="success", source="control-plane", actor={"user_id": owner}, details=details
            ),
        )

    def _snapshot_for_location(self, location):
        """Bind a logical snapshot to the selected replica without rewriting its immutable record."""
        head = (
            self.lifecycle._get_record("curation_locations", location["location_id"])
            if self.lifecycle
            else None
        )
        snapshot = self.lifecycle.curation_snapshot(head["dataset_version_id"]) if head else None
        if snapshot:
            return {**snapshot, "version": {**snapshot["version"], "root": location["root"]}}
        return None

    def _info(self, location):
        """Read synchronized version metadata for legacy local registration stubs."""
        if location["metadata"].get("features"):
            return location["metadata"]
        if self.lifecycle:
            snapshot = self._snapshot_for_location(location)
            if snapshot:
                return snapshot["info"]
        return location["metadata"]

    def preview(self, owner: str, location_id: str, definition: dict) -> dict:
        from lerobot.data_platform.processing.planner import PipelineValidationError

        with self.store.sessions() as session:
            location, node = self._location(session, location_id, owner)
            try:
                spec = PipelineSpec.from_dict(definition)
            except PipelineValidationError as exc:
                return {
                    "definition": definition,
                    "location_id": location_id,
                    "can_execute": False,
                    "blocked": [exc.issue],
                    "steps": [],
                    "input_identity": location_identity(location),
                }
            blocked, resolved = [], []
            source_info = self._info(location)
            synchronized_source = bool(
                self.lifecycle and self.lifecycle._get_record("curation_locations", location_id)
            )
            for position, step in enumerate(spec.steps, 1):
                operator = get_operator(step.operator_id, step.version)
                info = source_info if step.input == "source" else {}
                reason = availability(
                    operator,
                    node,
                    info,
                    step.parameters,
                    validate_dataset=step.input == "source"
                    and bool(info.get("features") or not synchronized_source),
                )
                code, suggestion = getattr(reason, "code", "operator_unavailable"), None
                if (
                    operator.family == "curation"
                    and operator.operator_id == "curation.snapshot"
                    and step.input == "source"
                ):
                    synchronized_source = not reason
                elif (
                    self.lifecycle
                    and step.input == "source"
                    and not synchronized_source
                    and operator.input_type == "dataset"
                    and not operator.operator_id.startswith("import.mcap.")
                ):
                    reason = "Add a Snapshot step first to bind the source dataset version"
                    code = "missing_snapshot"
                    suggestion = {"operator_id": "curation.snapshot", "input": "source", "parameters": {}}
                if operator.review_required and self.store.restricted_dataset_keys(owner) is not None:
                    reason = "Curation review requires unrestricted dataset access"
                    code = "permission_denied"
                if reason:
                    blocked.append(
                        {
                            "step_id": step.step_id,
                            "operator_id": step.operator_id,
                            "position": position,
                            "code": code,
                            "reason": reason,
                            "suggestion": suggestion,
                        }
                    )
                for source_id in step.options.get("source_location_ids", []):
                    self._location(session, source_id, owner, node["node_id"])
                    if self.lifecycle and not self.lifecycle._get_record("curation_locations", source_id):
                        blocked.append(
                            {
                                "step_id": step.step_id,
                                "reason": "Synchronize every Merge input dataset version first",
                                "operator_id": step.operator_id,
                                "position": position,
                                "code": "missing_source_snapshot",
                            }
                        )
                output = {"type": operator.output_type}
                if operator.output_type == "dataset":
                    output["path_template"] = self._output(location, node, "{run_id}", step.step_id)
                    local = node["name"].startswith("local-") and node["capabilities"].get("local_requests")
                    if not local and not any(
                        PurePosixPath(output["path_template"]).is_relative_to(PurePosixPath(path))
                        for path in node["allowed_roots"]
                    ):
                        blocked.append(
                            {
                                "step_id": step.step_id,
                                "reason": "Dataset output must be inside this node's configured read roots",
                                "operator_id": step.operator_id,
                                "position": position,
                                "code": "output_scope",
                            }
                        )
                resolved.append(
                    {
                        "step_id": step.step_id,
                        "operator_id": operator.operator_id,
                        "version": operator.version,
                        "parameters": step.parameters,
                        "input": step.input,
                        "dependencies": list(step.artifacts),
                        "output": output,
                        "review_before": step.review_before,
                    }
                )
        return {
            "definition": spec.to_dict(),
            "digest": spec.digest,
            "location_id": location_id,
            "input_identity": location_identity(location),
            "can_execute": not blocked,
            "blocked": blocked,
            "steps": resolved,
        }

    def save(self, owner: str, definition: dict, *, pipeline_id=None, expected_revision=None) -> dict:
        spec = PipelineSpec.from_dict(definition)
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            self._actor(session, owner)
            if pipeline_id:
                row = session.get(Pipeline, pipeline_id)
                if row is None or row.owner != owner:
                    raise KeyError("Pipeline not found")
                if type(expected_revision) is not int or row.revision != expected_revision:
                    raise JobConflictError("Pipeline revision conflict")
                row.revision += 1
                row.name = spec.name
                row.updated_at = time.time()
            else:
                row = Pipeline(
                    pipeline_id=str(uuid.uuid4()),
                    owner=owner,
                    name=spec.name,
                    revision=1,
                    updated_at=time.time(),
                )
                session.add(row)
            version = PipelineVersion(
                version_id=f"{row.pipeline_id}:{row.revision}",
                pipeline_id=row.pipeline_id,
                revision=row.revision,
                spec=spec.to_dict(),
                digest=spec.digest,
            )
            session.add(version)
            self._audit(session, owner, "pipeline.saved", pipeline_id=row.pipeline_id, revision=row.revision)
            return {
                "pipeline_id": row.pipeline_id,
                "revision": row.revision,
                "definition": spec.to_dict(),
                "digest": spec.digest,
            }

    def list(self, owner: str, *, page=None, search=""):
        with self.store.sessions() as session:
            self._actor(session, owner, write=False)
            query = select(Pipeline).where(Pipeline.owner == owner)
            if search:
                query = query.where(Pipeline.name.contains(search, autoescape=True))
            query = (
                page.apply(query, Pipeline.updated_at, Pipeline.pipeline_id)
                if page
                else query.order_by(Pipeline.updated_at.desc()).limit(200)
            )
            rows = session.scalars(query).all()
            paging = {}
            if page:
                rows, paging = page.response(
                    rows, timestamp=lambda r: r.updated_at, identity=lambda r: r.pipeline_id
                )
            result = [
                {"pipeline_id": row.pipeline_id, "revision": row.revision, "name": row.name} for row in rows
            ]
            return (result, paging) if page else result

    def get(self, owner: str, pipeline_id: str, revision: int | None = None) -> dict:
        with self.store.sessions() as session:
            self._actor(session, owner, write=False)
            row = session.get(Pipeline, pipeline_id)
            if row is None or row.owner != owner:
                raise KeyError("Pipeline not found")
            version = session.get(PipelineVersion, f"{pipeline_id}:{revision or row.revision}")
            if version is None:
                raise KeyError("Pipeline version not found")
            return {
                "pipeline_id": pipeline_id,
                "revision": version.revision,
                "definition": version.spec,
                "digest": version.digest,
            }

    def start(self, owner: str, pipeline_id: str, revision: int, location_id: str, delivery: str) -> dict:
        if not delivery or len(delivery) > 128:
            raise ValueError("An Idempotency-Key of 1–128 characters is required")
        saved = self.get(owner, pipeline_id, revision)
        key = content_digest([owner, pipeline_id, revision, location_id, delivery])
        with self.store.sessions() as session:
            self._location(session, location_id, owner)
            previous = session.scalar(select(PipelineRun).where(PipelineRun.idempotency_key == key))
            previous_id = previous.run_id if previous else None
        if previous_id:
            return self.run(owner, previous_id)
        preview = self.preview(owner, location_id, saved["definition"])
        if not preview["can_execute"]:
            raise ValueError("; ".join(item["reason"] for item in preview["blocked"]))
        key = content_digest([owner, pipeline_id, revision, location_id, delivery])
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            self._location(session, location_id, owner)
            existing = session.scalar(select(PipelineRun).where(PipelineRun.idempotency_key == key))
            if existing:
                run_id = existing.run_id
            else:
                active = session.scalars(
                    select(PipelineRun)
                    .where(
                        PipelineRun.owner == owner,
                        PipelineRun.status.in_(
                            ["queued", "running", "waiting_review", "cancel_requested", "retry_requested"]
                        ),
                    )
                    .limit(self.store.job_manager.queue_limit)
                ).all()
                if len(active) >= self.store.job_manager.queue_limit:
                    raise JobConflictError("User pipeline queue limit reached")
                run_id = str(uuid.uuid4())
                session.add(
                    PipelineRun(
                        run_id=run_id,
                        owner=owner,
                        version_id=f"{pipeline_id}:{revision}",
                        location_id=location_id,
                        node_id=preview["input_identity"]["node_id"],
                        status="queued",
                        revision=0,
                        idempotency_key=key,
                        input_identity=preview["input_identity"],
                        created_at=time.time(),
                        updated_at=time.time(),
                    )
                )
                for position, step in enumerate(saved["definition"]["steps"]):
                    session.add(
                        PipelineStepRun(
                            row_id=f"{run_id}:{step['step_id']}",
                            run_id=run_id,
                            step_id=step["step_id"],
                            position=position,
                            status="queued",
                        )
                    )
                self._audit(session, owner, "pipeline.started", run_id=run_id, digest=saved["digest"])
        return self.run(owner, run_id)

    def runs(self, owner: str, location_id: str | None = None, *, page=None, filters=None):
        with self.store.sessions() as session:
            actor = self._actor(session, owner, write=False)
            allowed = self.store._accessible_location_ids(session, actor)
            query = select(PipelineRun).where(PipelineRun.owner == owner)
            if allowed is not None:
                query = query.where(PipelineRun.location_id.in_(allowed))
            if location_id:
                query = query.where(PipelineRun.location_id == location_id)
            from sqlalchemy.orm import load_only

            from lerobot.data_platform.list_queries import parse_time

            filters = filters or {}
            if filters.get("status"):
                query = query.where(PipelineRun.status == filters["status"])
            for key, condition in (
                ("created_from", PipelineRun.created_at.__ge__),
                ("created_to", PipelineRun.created_at.__lt__),
            ):
                if filters.get(key):
                    from datetime import timezone

                    query = query.where(
                        condition(parse_time(filters[key]).replace(tzinfo=timezone.utc).timestamp())
                    )
            query = query.options(
                load_only(
                    PipelineRun.run_id,
                    PipelineRun.version_id,
                    PipelineRun.status,
                    PipelineRun.location_id,
                    PipelineRun.updated_at,
                    PipelineRun.created_at,
                )
            )
            query = (
                page.apply(query, PipelineRun.created_at, PipelineRun.run_id)
                if page
                else query.order_by(PipelineRun.created_at.desc()).limit(100)
            )
            rows = session.scalars(query).all()
            paging = {}
            if page:
                rows, paging = page.response(
                    rows, timestamp=lambda r: r.created_at, identity=lambda r: r.run_id
                )
            result = [
                {
                    "run_id": row.run_id,
                    "version_id": row.version_id,
                    "status": row.status,
                    "location_id": row.location_id,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]
            return (result, paging) if page else result

    def run(self, owner: str, run_id: str) -> dict:
        with self.store.sessions() as session:
            self._actor(session, owner, write=False)
            row = session.get(PipelineRun, run_id)
            if row is None or row.owner != owner:
                raise KeyError("Pipeline run not found")
            self._location(session, row.location_id, owner, write=False, allow_retired=True)
            steps = session.scalars(
                select(PipelineStepRun)
                .where(PipelineStepRun.run_id == run_id)
                .order_by(PipelineStepRun.position)
            ).all()
            definition = PipelineSpec.from_dict(session.get(PipelineVersion, row.version_id).spec)
            descriptions = {item.step_id: get_operator(item.operator_id) for item in definition.steps}
            return {
                "run_id": run_id,
                "version_id": row.version_id,
                "location_id": row.location_id,
                "status": row.status,
                "revision": row.revision,
                "error": row.error,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "operator_id": descriptions[step.step_id].operator_id,
                        "review_required": descriptions[step.step_id].review_required,
                        "status": step.status,
                        "job_id": step.job_id,
                        "output_location_id": step.output_location_id,
                        "input_identity": step.input_identity,
                        "input_location_id": step.input_location_id,
                        "decision": {
                            key: value for key, value in (step.decision or {}).items() if key != "parameters"
                        },
                    }
                    for step in steps
                ],
            }

    @staticmethod
    def _output(location, node, run_id, step_id):
        root = PurePosixPath(location["root"])
        output = root.with_name(f"{root.name[:80]}_pipeline_{run_id}_{step_id}")
        if not any(output.is_relative_to(PurePosixPath(path)) for path in node["writable_roots"]):
            raise ValueError("A writable sibling output root is required on this execution node")
        return str(output)

    def _command(self, run, step, location, node, evidence, decision=None):
        spec = get_operator(step.operator_id, step.version)
        options = dict(step.parameters)
        snapshot = self._snapshot_for_location(location)
        task_config = (snapshot or {}).get("task_config") or TaskConfigSnapshot.from_dict(None).to_dict()
        if spec.family == "curation":
            if spec.operator_id != "curation.snapshot" and not snapshot:
                raise ValueError("Synchronize a dataset version with a snapshot step before Curation")
            target = {
                "location_id": location["location_id"],
                "dataset_key": location["dataset_key"],
                "dataset_version_id": (snapshot or {}).get("version", {}).get("version_id"),
            }
            options = {
                "parameters": options,
                "target": target,
                "snapshot": snapshot,
                "task_config": task_config,
                "dataset_id": (snapshot or {})
                .get("version", {})
                .get("dataset_id", "ds_" + content_digest([location["location_id"]])[:20]),
                "input_files": {},
            }
            for previous in evidence:
                record = (
                    self.lifecycle._get_record("curation_runs", previous.job_id) if self.lifecycle else None
                )
                if record:
                    if record["target"] != target:
                        raise ValueError("Evidence belongs to a different dataset version")
                    folder = self.lifecycle.root / "curation" / "runs" / previous.job_id
                    manifest = json.loads((folder / "curation-manifest.json").read_text())
                    aliases = {}
                    variant = record["result"].get("output_variant")
                    if variant and record["operation"] in {"curation.labeling", "curation.tagging"}:
                        group = record["operation"].split(".")[1]
                        stem = "labels" if group == "labeling" else "tags"
                        aliases = {
                            f"{group}/{stem}_{variant}.jsonl": f"{group}/{stem}.jsonl",
                            f"{group}/{stem}_reviewed_{variant}.jsonl": f"{group}/{stem}_reviewed.jsonl",
                            f"{group}/source_{variant}.json": f"{group}/source.json",
                        }
                    for name, descriptor in manifest["files"].items():
                        if not name.startswith("static/"):
                            continue
                        relative = name.removeprefix("static/")
                        if relative in aliases.values():
                            continue
                        relative = aliases.get(relative, relative)
                        if (
                            relative in options["input_files"]
                            and options["input_files"][relative]["sha256"] != descriptor["sha256"]
                        ):
                            raise ValueError("Selected evidence contains conflicting artifacts")
                        options["input_files"][relative] = {
                            **descriptor,
                            "run": previous.job_id,
                            "path": name,
                        }
            if step.options.get("workspace_id"):
                workspace = self.lifecycle.get_workspace(step.options["workspace_id"])
                if workspace.base_dataset_version_id != target["dataset_version_id"] or (
                    workspace.revision != step.options.get("expected_revision")
                ):
                    raise JobConflictError("Curation workspace revision or dataset conflict")
                options["workspace"] = workspace.to_dict()
            if spec.operator_id == "curation.materialize":
                manifest = self.lifecycle.get_manifest(str(step.options.get("manifest_id") or ""))
                if (
                    manifest.status != "published"
                    or manifest.base_dataset_version_id != target["dataset_version_id"]
                ):
                    raise ValueError("Select a published manifest for this input version")
                profile = (
                    self.lifecycle.get_profile(step.options["profile_id"], kind="materialization")
                    if (step.options.get("profile_id"))
                    else self.lifecycle.default_materialization_profile()
                )
                options.update(manifest=manifest.to_dict(), profile=profile.to_dict())
            if spec.output_type == "dataset":
                options["parents"] = [
                    self.lifecycle.get_version(item).to_dict()
                    for item in snapshot["version"]["parent_version_ids"]
                ]
                options["source_batches"] = [
                    self.lifecycle.get_source_batch(item).to_dict()
                    for item in snapshot["version"].get("source_batch_ids", [])
                ]
        elif spec.family == "viewer":
            options["task_config"] = task_config
        elif spec.family == "caption":
            from lerobot.data_platform.temporal_caption_jobs import validate_options, validate_source

            validate_source(self._info(location))
            options = validate_options(options)
        if spec.operator_id == "preprocess.pose_interpolate":
            if (
                not snapshot
                or not evidence
                or not all(item.input_identity == location_identity(location) for item in evidence)
            ):
                raise ValueError("Pose repair requires matching, versioned Quality evidence")
            receipt = self._review_workspace(step, run.owner, location, evidence, decision or {})
            options["_review"] = {
                **receipt,
                "source_fingerprint": snapshot["version"]["fingerprint"],
                "repairs_digest": content_digest(step.parameters["repairs"]),
                "evidence_job_ids": [item.job_id for item in evidence],
                "approved_by": run.owner,
            }
        if spec.family not in {"mcap", "curation", "caption", "viewer", "preprocess", "mutation"}:
            options = {
                "parameters": dict(step.parameters),
                "target": {
                    "dataset_key": location["dataset_key"],
                    "location_id": location["location_id"],
                    "dataset_version_id": (snapshot or {}).get("version", {}).get("version_id"),
                },
            }
            options["input_files"] = {}
            for previous in evidence:
                curation = self.lifecycle._get_record("curation_runs", previous.job_id)
                folder = (
                    self.lifecycle.root / "curation/runs"
                    if curation
                    else self.lifecycle.root / "operator-results"
                ) / previous.job_id
                manifest = json.loads((folder / "curation-manifest.json").read_text())
                if manifest["target"] != options["target"]:
                    raise ValueError("Input artifacts belong to a different dataset version")
                for name, descriptor in manifest["files"].items():
                    if name.startswith("static/"):
                        relative = previous.step_id + "/" + name.removeprefix("static/")
                        options["input_files"][relative] = {
                            **descriptor,
                            "run": previous.job_id,
                            "path": name,
                            "storage": "curation" if curation else "operator",
                        }

        if spec.output_type == "dataset":
            options["out_root"] = self._output(
                {"root": run.input_identity["root"]}, node, run.run_id, step.step_id
            )
        if self.lifecycle and spec.family not in {"curation", "mcap"} and spec.output_type == "dataset":
            if not snapshot:
                raise ValueError("A synchronized input version is required")
            snapshots = [snapshot]
            for source_id in step.options.get("source_location_ids", []):
                if source_id == location["location_id"]:
                    continue
                selected = self._snapshot_for_location(self.store.get_location(source_id))
                if not selected:
                    raise ValueError("Synchronize every Merge input dataset version first")
                snapshots.append(selected)
            options["version_inputs"] = snapshots
            options["target"] = {
                "location_id": location["location_id"],
                "dataset_key": location["dataset_key"],
                "dataset_version_id": snapshot["version"]["version_id"],
            }
            options["parents"] = [
                self.lifecycle.get_version(item).to_dict()
                for value in snapshots
                for item in value["version"]["parent_version_ids"]
            ]
            options["source_batches"] = [
                self.lifecycle.get_source_batch(item).to_dict()
                for value in snapshots
                for item in value["version"].get("source_batch_ids", [])
            ]
            options["task_config"] = task_config
        options["operator_snapshot"] = {
            "operator_id": spec.operator_id,
            "version": spec.version,
            "release": os.environ.get("DATA_PLATFORM_RELEASE", "legacy"),
            "data_format": self._info(location).get("codebase_version", "unknown"),
            "pipeline_run": run.run_id,
            "step_id": step.step_id,
            "input_identity": location_identity(location),
            "source_fingerprint": (snapshot or {}).get("version", {}).get("fingerprint"),
        }
        return options

    def tick(self, run_id: str) -> None:
        from lerobot.data_platform.control_plane import RemoteJob
        from lerobot.data_platform.maintenance import is_maintenance

        self._reconcile_action(run_id)
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            if is_maintenance(session):
                return
            run = session.get(PipelineRun, run_id)
            if not run or run.status not in {"queued", "running", "waiting_review", "cancel_requested"}:
                return
            try:
                source, node = self._location(
                    session,
                    run.location_id,
                    run.owner,
                    run.node_id,
                    allow_retired=run.status == "cancel_requested",
                )
                definition = session.get(PipelineVersion, run.version_id)
                spec = PipelineSpec.from_dict(definition.spec)
                steps = session.scalars(
                    select(PipelineStepRun)
                    .where(PipelineStepRun.run_id == run_id)
                    .order_by(PipelineStepRun.position)
                ).all()
                by_id = {row.step_id: row for row in steps}
                for step, row in zip(spec.steps, steps, strict=True):
                    if row.status in {"done", "skipped"}:
                        continue
                    if row.job_id:
                        job = session.get(RemoteJob, row.job_id)
                        if job.status in {"queued", "running", "cancel_requested"}:
                            return
                        if job.status != "done":
                            row.status = job.status
                            run.status = "cancelled" if run.status == "cancel_requested" else "error"
                            run.error = job.error or f"Step {row.step_id}: {job.status}"
                            run.revision += 1
                            return
                        row.status = "done"
                        row.output_location_id = (job.result or {}).get("output_location_id")
                        if (
                            get_operator(step.operator_id).output_type == "dataset"
                            and not row.output_location_id
                        ):
                            raise ValueError("Published dataset output was not registered")
                        session.flush()
                        continue
                    if run.status == "cancel_requested":
                        run.status = "cancelled"
                        run.revision += 1
                        return
                    location_id = (
                        run.location_id if step.input == "source" else by_id[step.input].output_location_id
                    )
                    location, current_node = self._location(session, location_id, run.owner, run.node_id)
                    identity = location_identity(location)
                    if step.input == "source" and identity != run.input_identity:
                        raise JobConflictError("Pipeline input changed; preview a new run")
                    if row.decision and row.input_identity != identity:
                        raise JobConflictError("Reviewed input changed; preview a new run")
                    reason = availability(
                        get_operator(step.operator_id), current_node, self._info(location), step.parameters
                    )
                    if reason:
                        raise ValueError(reason)
                    if step.review_before and not row.decision:
                        if row.status != "waiting_review":
                            row.status, row.input_location_id, row.input_identity = (
                                "waiting_review",
                                location_id,
                                identity,
                            )
                            run.status = "waiting_review"
                            run.revision += 1
                        return
                    evidence = [by_id[key] for key in step.artifacts]
                    if any(item.status != "done" for item in evidence):
                        raise ValueError("Required evidence step did not finish successfully")
                    options = self._command(run, step, location, node, evidence, row.decision)
                    if step.options.get("source_location_ids"):
                        sources = list(dict.fromkeys([location_id, *step.options["source_location_ids"]]))
                        if len(sources) < 2:
                            raise ValueError("Merge/Subtract requires at least two registered inputs")
                        options["source_location_ids"] = sources
                        options["_source_locations"] = [
                            self._location(session, key, run.owner, run.node_id)[0] for key in sources
                        ]
                    job = self.store.create_job(
                        location_id=location_id,
                        requested_by=run.owner,
                        operation=step.operator_id,
                        options=options,
                        idempotency_key=content_digest([run_id, row.step_id]),
                        _session=session,
                    )
                    row.job_id, row.input_location_id, row.input_identity = (
                        job["job_id"],
                        location_id,
                        identity,
                    )
                    row.status = run.status = "running"
                    run.updated_at = time.time()
                    run.revision += 1
                    return
                run.status = "cancelled" if run.status == "cancel_requested" else "done"
                run.revision += 1
                run.updated_at = time.time()
                self._audit(session, run.owner, "pipeline.finished", run_id=run_id, status=run.status)
            except (ValueError, PermissionError, KeyError, JobConflictError) as exc:
                run.status, run.error = "error", str(exc)
                run.revision += 1

    def review_options(self, owner, run_id, step_id):
        if self.store.restricted_dataset_keys(owner) is not None:
            raise PermissionError("Curation review requires unrestricted dataset access")
        with self.store.sessions() as session:
            actor = self._actor(session, owner, write=False)
            run = session.get(PipelineRun, run_id)
            if not run or run.owner != owner:
                raise KeyError("Pipeline run not found")
            self._location(session, run.location_id, owner, write=False)
            step = session.get(PipelineStepRun, f"{run_id}:{step_id}")
            if not step or step.status != "waiting_review":
                raise JobConflictError("Select a step waiting for review")
            location, _ = self._location(session, step.input_location_id, owner, write=False)
            if location_identity(location) != step.input_identity:
                raise JobConflictError("Reviewed dataset changed")
            head = (
                self.lifecycle._get_record("curation_locations", step.input_location_id)
                if self.lifecycle
                else None
            )
            values = (
                self.lifecycle.repository.workspace_summaries(head["dataset_version_id"], actor.username)
                if head
                else []
            )
            return {"workspaces": values, "run_revision": run.revision}

    def decide(self, owner, run_id, body):
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            run = session.get(PipelineRun, run_id)
            if not run or run.owner != owner:
                raise KeyError("Pipeline run not found")
            self._location(session, run.location_id, owner)
            if run.status != "waiting_review" or run.revision != body.get("expected_revision"):
                raise JobConflictError("Pipeline decision revision conflict")
            row = session.get(PipelineStepRun, f"{run_id}:{body.get('step_id', '')}")
            if not row or row.status != "waiting_review" or body.get("decision") not in {"approve", "skip"}:
                raise ValueError("Select a pending step and approve or skip it")
            location, _ = self._location(session, row.input_location_id, owner)
            if location_identity(location) != row.input_identity:
                raise JobConflictError("Reviewed dataset changed")
            spec = PipelineSpec.from_dict(session.get(PipelineVersion, run.version_id).spec)
            step = spec.steps[row.position]
            evidence = [session.get(PipelineStepRun, f"{run_id}:{key}") for key in step.artifacts]
            if any(not item or item.status != "done" for item in evidence):
                raise JobConflictError("Required evidence is not complete")
            workspace = {}
            if get_operator(step.operator_id).review_required and body["decision"] == "approve":
                workspace = self._review_workspace(step, owner, location, evidence, body)
            row.decision = {
                **workspace,
                "decision": body["decision"],
                "actor": owner,
                "at": time.time(),
                "input_identity": row.input_identity,
                "parameters": step.parameters,
                "evidence_job_ids": [item.job_id for item in evidence],
                "revision": run.revision,
            }
            row.status = "skipped" if body["decision"] == "skip" else "queued"
            if row.status == "skipped" and get_operator(step.operator_id).output_type == "dataset":
                row.output_location_id = row.input_location_id
            run.status = "queued"
            run.revision += 1
            self._audit(
                session, owner, "pipeline.decision", run_id=run_id, step_id=row.step_id, decision=row.decision
            )
        return self.run(owner, run_id)

    def _review_workspace(self, step, owner, location, evidence, answer):
        if self.store.restricted_dataset_keys(owner) is not None:
            raise PermissionError("Curation review requires unrestricted dataset access")
        if not self.lifecycle:
            raise ValueError("A versioned Curation review is required")
        workspace = self.lifecycle.get_workspace(str(answer.get("workspace_id") or ""))
        head = self.lifecycle._get_record("curation_locations", location["location_id"])
        if (
            not head
            or workspace.base_dataset_version_id != head["dataset_version_id"]
            or (workspace.revision != answer.get("workspace_revision"))
        ):
            raise JobConflictError("Curation review revision or dataset conflict")
        with self.store.sessions() as accounts:
            username = self._actor(accounts, owner).username
        if workspace.owner != username:
            raise PermissionError("Use a Curation workspace owned by this account")
        self.lifecycle.validate_workspace(workspace.workspace_id)
        proposed = step.parameters["repairs"]
        reviewed = [
            {key: value for key, value in repair.items() if key != "episode_uid"}
            for recipe in workspace.repair_recipes
            if recipe["op"] == "pose_interpolate"
            for repair in recipe["params"]["repairs"]
        ]
        if content_digest(sorted(reviewed, key=content_digest)) != content_digest(
            sorted(proposed, key=content_digest)
        ):
            raise ValueError("Save the exact proposed pose intervals in the Curation repair draft first")
        if any(item.input_identity != location_identity(location) for item in evidence):
            raise JobConflictError("Quality evidence belongs to a different input")
        return {"workspace_id": workspace.workspace_id, "workspace_revision": workspace.revision}

    def action(self, owner: str, run_id: str, action: str, body: dict) -> dict:
        from lerobot.data_platform.control_plane import RemoteJob

        if action not in {"cancel", "retry"}:
            raise ValueError("Unknown pipeline action")
        controlled = []
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            self._actor(session, owner)
            run = session.get(PipelineRun, run_id)
            if not run or run.owner != owner:
                raise KeyError("Pipeline run not found")
            if run.revision != body.get("expected_revision"):
                raise JobConflictError("Pipeline run revision conflict")
            self._location(session, run.location_id, owner, allow_retired=action == "cancel")
            if action == "cancel":
                if run.status not in {"queued", "running", "waiting_review", "error", "cancel_requested"}:
                    raise JobConflictError("Pipeline is already finished")
                run.status = "cancel_requested"
            elif run.status != "error":
                raise JobConflictError("Only failed pipelines can be retried")
            for row in session.scalars(select(PipelineStepRun).where(PipelineStepRun.run_id == run_id)):
                job = session.get(RemoteJob, row.job_id) if row.job_id else None
                if job and (
                    (action == "cancel" and job.status in {"queued", "running"})
                    or (action == "retry" and row.status not in {"done", "skipped"} and job.status != "done")
                ):
                    controlled.append(job.job_id)
            run.revision += 1
            run.pending_action = {"action": action, "revision": body["expected_revision"], "jobs": controlled}
            if action == "retry":
                run.status = "retry_requested"
            self._audit(session, owner, f"pipeline.{action}", run_id=run_id)
        self._reconcile_action(run_id)
        self.tick(run_id)
        return self.run(owner, run_id)

    def _reconcile_action(self, run_id):
        """Replay durable intent after a coordinator crash; job commands are idempotent."""
        with self.store.sessions() as session:
            run = session.get(PipelineRun, run_id)
            if not run or not run.pending_action:
                return
            pending = dict(run.pending_action)
            actor = self._actor(session, run.owner)
            actor_dict = self.store._user_dict(actor)
        action = pending["action"]
        for job_id in pending["jobs"]:
            payload = self.store.get_job(job_id)
            if action == "cancel" and payload["status"] not in {"queued", "running"}:
                continue
            # A replay may see the already accepted retry, including its next attempt.
            if action == "retry" and payload["status"] in {"queued", "running", "done"}:
                continue
            control = self.store.job_manager.decorate(payload, actor_dict)
            self.store.job_manager.command(
                job_id,
                actor_dict,
                action,
                {
                    "revision": control["revision"],
                    "idempotency_key": content_digest([run_id, action, pending["revision"], job_id]),
                },
                owner_only=True,
            )
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            run = session.get(PipelineRun, run_id)
            if run.pending_action != pending:
                return
            if action == "retry":
                for row in session.scalars(select(PipelineStepRun).where(PipelineStepRun.run_id == run_id)):
                    if row.status not in {"done", "skipped"}:
                        row.status = "running" if row.job_id else "queued"
                run.status, run.error = "queued", None
            run.pending_action = {}
            run.revision += 1
