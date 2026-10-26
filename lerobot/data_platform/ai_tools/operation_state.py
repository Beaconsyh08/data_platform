"""Bounded, authorized operation observations shared by browser and terminal clients.

A successful Job is not proof of a reusable result. Require matching input/configuration
and a published artifact; older unbound results remain explicitly historical.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from sqlalchemy import String, cast, func, or_, select

from lerobot.data_platform.ai_tools.context import safe_summary, viewer_reference
from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.operators.schema import validate_parameters
from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun, PipelineVersion
from lerobot.data_platform.processing.service import location_identity, single_operation

ACTIVE = {"queued", "running", "waiting_review", "cancel_requested", "retry_requested"}
LIMIT = 20


def _small_json(expression):
    # SQL-side truncation bounds transfer even for a legacy oversized configuration.
    return func.substr(cast(expression, String), 1, 8193)


def _decode(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _link(title, path, **query):
    return {"title": title, "url": path + "?" + urlencode(query)}


def _artifact_links(manager, session, owner, location, operator, job_id, output):
    if operator == "viewer.prepare":
        ref = viewer_reference(location)
        return [ref] if ref else []
    if output:
        try:
            manager._location(session, output, owner, write=False)
        except (KeyError, PermissionError, JobConflictError):
            return []
        return [_link("Open derived dataset", "/", select="remote:" + output)]
    if not operator.startswith("curation.") or not manager.lifecycle:
        return []
    # Match the existing Curation scope guard before touching evidence on disk.
    if manager.store.restricted_dataset_keys(owner) is not None:
        return []
    base = manager.lifecycle.root / "curation" / "runs"
    folder = base / job_id
    try:
        manifest = folder / "curation-manifest.json"
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(base.resolve()):
            return []
        with manifest.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            return []
        value = json.loads(raw)
        if (
            value.get("target", {}).get("location_id") != location["location_id"]
            or value.get("operation") != operator
            or value.get("protocol") != 1
        ):
            return []
        files = value.get("files", {})
        if not files or len(files) > 256:
            return []
        for name, descriptor in files.items():
            path = folder / name
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                return []
            if not path.is_file() or path.stat().st_size != descriptor["size"]:
                return []
        page = "quality" if operator == "curation.quality" else "annotation"
        return [
            _link(
                "Open Quality report" if page == "quality" else "Open Review",
                "/curation",
                location_id=location["location_id"],
                run=job_id,
                page=page,
                tab={"curation.quality": "quality_report", "curation.stage": "stage_subtask"}.get(
                    operator, operator.split(".")[-1]
                ),
            )
        ]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def inspect_operation(service, owner, action, *, execution_run_id=None):
    """Own attempts and authorized published results; never expose another account's task details."""
    from lerobot.data_platform.ai_tools.actions import allowed_parameters

    manager = service.manager
    operator = action["operator_id"]
    spec = get_operator(operator)
    parameters = validate_parameters(spec, action["parameters"])
    with service.store.sessions() as session:
        location, _ = manager._location(session, action["location_id"], owner, write=False)
        identity = location_identity(location)
        actor = manager._actor(session, owner, write=False)
        head = (
            manager.lifecycle._get_record("curation_locations", action["location_id"])
            if manager.lifecycle
            else None
        )
        state = {
            "state": "new",
            "status": "not_started",
            "message": "No matching result found in recent operations. Review parameters and confirm to run.",
            "links": [],
            "checked_at": int(time.time()),
            "history_limit": LIMIT,
            "can_confirm": True,
            "requires_rerun": False,
        }
        if operator == "curation.quality":
            state["coverage_notice"] = (
                "Check the report's actual checks, episode selection and media coverage. Sampled or missing coverage cannot certify the whole dataset."
            )
        # Pending pipeline steps have no Job yet. Find those before querying acknowledged jobs.
        pending = session.execute(
            select(PipelineRun.run_id, PipelineRun.status, PipelineRun.input_identity, PipelineVersion.spec)
            .join(PipelineVersion, PipelineVersion.version_id == PipelineRun.version_id)
            .where(
                PipelineRun.owner == owner,
                PipelineRun.location_id == action["location_id"],
                PipelineRun.status.in_(ACTIVE),
            )
            .order_by(PipelineRun.created_at.desc())
            .limit(LIMIT + 1)
        ).all()
        for row in pending[:LIMIT]:
            if execution_run_id and row.run_id != execution_run_id:
                continue
            if row.input_identity == identity and single_operation(row.spec) == (
                operator,
                spec.version,
                parameters,
            ):
                url = _link("Open run", "/pipelines", location_id=action["location_id"], run_id=row.run_id)
                return {
                    **state,
                    "state": "running",
                    "status": row.status,
                    "message": "An equivalent operation is already queued or running. Follow its progress; no duplicate task is needed.",
                    "can_confirm": False,
                    "run_id": row.run_id,
                    "url": url["url"],
                    "links": [url],
                }
        names = sorted(allowed_parameters(operator))
        # Project only bounded action parameters and identities, never full results/options/reports.
        rows = (
            session.execute(
                select(
                    RemoteJob.job_id,
                    RemoteJob.status,
                    RemoteJob.updated_at,
                    _small_json(RemoteJob.options["operator_snapshot"]).label("snapshot"),
                    _small_json(RemoteJob.options["parameters"]).label("parameters"),
                    RemoteJob.options["target"]["dataset_version_id"].as_string().label("input_version"),
                    RemoteJob.requested_by,
                    RemoteJob.result["output_location_id"].as_string().label("output"),
                    *[_small_json(RemoteJob.options[name]).label("p_" + name) for name in names],
                )
                .where(
                    RemoteJob.location_id == action["location_id"],
                    or_(RemoteJob.requested_by == owner, RemoteJob.status == "done"),
                    RemoteJob.operation == operator,
                    *(
                        [
                            RemoteJob.options["operator_snapshot"]["pipeline_run"].as_string()
                            == execution_run_id
                        ]
                        if execution_run_id
                        else []
                    ),
                )
                .order_by(RemoteJob.created_at.desc())
                .limit(LIMIT + 1)
            )
            .mappings()
            .all()
        )
        state["history_truncated"] = len(rows) > LIMIT or len(pending) > LIMIT
        historical = None
        for row in rows[:LIMIT]:
            snapshot = _decode(row["snapshot"])
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            decoded = _decode(row["parameters"])
            raw = (
                decoded
                if isinstance(decoded, dict)
                else {
                    name: _decode(row["p_" + name]) for name in names if _decode(row["p_" + name]) is not None
                }
            )
            try:
                matches = validate_parameters(spec, raw) == parameters and all(
                    value is None or len(value) <= 8192
                    for value in [row["parameters"], *[row["p_" + name] for name in names]]
                )
            except ValueError:
                matches = False
            matches = (
                matches
                and snapshot.get("version") == spec.version
                and snapshot.get("input_identity") == identity
            )
            if operator.startswith("curation."):
                matches = matches and bool(head) and row["input_version"] == head["dataset_version_id"]
            # Viewer caches can be replaced independently of an old Job. Without a cache-to-job
            # provenance binding, offer the published cache as historical rather than asserting settings.
            if operator == "viewer.prepare" and row["status"] == "done":
                matches = False
            own = row["requested_by"] == owner
            job_link = _link("Open Jobs", "/", page="runs", job_id=row["job_id"])
            observation = {
                **state,
                "job_id": row["job_id"],
                "status": row["status"],
                "updated_at": row["updated_at"].isoformat(),
                "url": job_link["url"],
                "links": [job_link] if own or actor.role == "admin" else [],
                "parameters": safe_summary(raw),
                "matches": matches,
            }
            if matches and row["status"] in ACTIVE:
                return {
                    **observation,
                    "state": "running",
                    "can_confirm": False,
                    "message": "An equivalent operation is already running. Open Jobs to follow it.",
                }
            if row["status"] == "done":
                if not matches and historical is not None:
                    continue
                output = row["output"]
                if not output:
                    output = session.scalar(
                        select(PipelineStepRun.output_location_id).where(
                            PipelineStepRun.job_id == row["job_id"]
                        )
                    )
                links = _artifact_links(manager, session, owner, location, operator, row["job_id"], output)
                if not own and not links:
                    continue
                if operator == "caption.annotate":
                    matches = False  # Review can resolve remote results; this bounded probe cannot.
                    links = [
                        _link(
                            "Inspect caption results",
                            "/remote/" + location["location_id"] + "/temporal-caption/review",
                        )
                    ]
                observation.update(links=links + observation["links"], requires_rerun=True)
                if not own and actor.role != "admin":
                    observation.pop("url", None)
                if matches and links:
                    return {
                        **observation,
                        "state": "ready",
                        "message": "A published result with matching input and parameters is available. Open it, or explicitly run again.",
                    }
                if historical is None:
                    historical = {
                        **observation,
                        "state": "history",
                        "message": "A previous operation exists, but its input/configuration or published artifacts cannot be verified as matching. Inspect it before running again.",
                    }
            elif matches and historical is None:
                historical = {
                    **observation,
                    "state": "failed",
                    "requires_rerun": True,
                    "message": "A previous attempt did not finish. Open Jobs to inspect recovery or publication retry before starting new computation.",
                }
        if historical:
            return historical
        if operator == "viewer.prepare":
            links = _artifact_links(manager, session, owner, location, operator, "", None)
            if links:
                return {
                    **state,
                    "state": "history",
                    "requires_rerun": True,
                    "links": links,
                    "message": "A published Viewer is available. Its original settings are not verified here; rebuild if the source or settings changed.",
                }
        if state["history_truncated"]:
            state["can_confirm"] = False
            state["message"] = (
                "No matching result found in the bounded recent history. Check Jobs for older work, and prepare a more specific operation."
            )
        return state


def action_status(service, owner, action_id):
    result = service.turn(owner, action_id)
    action = result.get("action")
    if not action:
        raise PermissionError("No accessible operation card")
    if not result.get("execution") and result.get("strategy"):
        from lerobot.data_platform.ai_tools.strategies import Strategies

        try:
            with service.store.sessions() as session:
                Strategies(service).check(session, result["strategy"])
        except (PermissionError, JobConflictError):
            return {
                "state": "failed",
                "can_confirm": False,
                "message": "Strategy was disabled or its evidence changed. Prepare a new action.",
                "links": [],
            }
    observation = inspect_operation(
        service, owner, action, execution_run_id=(result.get("execution") or {}).get("run_id")
    )
    if result.get("execution"):
        run = service.manager.run(owner, result["execution"]["run_id"])
        job = next((s["job_id"] for s in reversed(run["steps"]) if s.get("job_id")), None)
        observation.update(
            {
                **result["execution"],
                "status": run["status"],
                "steps": [{k: s[k] for k in ("step_id", "status", "job_id")} for s in run["steps"]],
            }
        )
        observation["jobs_url"] = _link("Open Jobs", "/", page="runs", **({"job_id": job} if job else {}))[
            "url"
        ]
        if run["status"] in ACTIVE:
            observation.update(state="running", can_confirm=False)
    return observation
