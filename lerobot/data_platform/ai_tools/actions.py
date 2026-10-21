"""Prepared operations require an authenticated, explicit confirmation outside the model loop."""

import uuid

from sqlalchemy import select

from lerobot.data_platform.ai_tools.context import safe_text, viewer_reference
from lerobot.data_platform.ai_tools.models import AssistantTurn
from lerobot.data_platform.ai_tools.repairs import bind_source_snapshot
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.operators.schema import parameter_schema
from lerobot.data_platform.processing.models import PipelineRun
from lerobot.data_platform.processing.service import location_identity
from lerobot.data_platform.task_catalog import content_digest

ALLOWED_ACTIONS = {"viewer.prepare": "Prepare Viewer", "curation.quality": "Run Quality checks"}
ACTION_PARAMETERS = {
    "viewer.prepare": {"prepare_videos", "prepare_csv", "prepare_workers", "downsample", "max_frames"},
    "curation.quality": {"episodes", "workers", "checks", "qc_config", "video_mode", "video_workers"},
}

# Single-input operations only. Evidence/review-driven operations stay in the pipeline editor.
ALLOWED_ACTIONS.update(
    {
        "curation.stage": "Compute stages",
        "curation.labeling": "Run object labeling",
        "curation.tagging": "Run auto-tagging",
        "caption.annotate": "Annotate temporal captions",
        **{
            "preprocess." + name: title
            for name, title in {
                "split": "Create dataset subset",
                "convert_action": "Convert signal dimensions",
                "convert_v3": "Convert dataset format",
                "drop_field": "Remove a field from a derived dataset",
                "smooth_action": "Smooth signals",
                "standardize": "Standardize dataset",
                "value_edit": "Edit derived signal values",
                "trajectory_cleanup": "Clean trajectories",
                "stage_return_alignment": "Align return stages",
                "stage_return_height_alignment": "Align return heights",
            }.items()
        },
    }
)


def allowed_parameters(operator_id):
    if operator_id in ACTION_PARAMETERS:
        return ACTION_PARAMETERS[operator_id]
    # Paths, endpoints, source/review mutation and version overrides never enter action cards.
    return set(parameter_schema(get_operator(operator_id))["properties"]) - {
        "endpoint",
        "vlm_endpoint",
        "model_id",
        "data_version",
        "overwrite",
        "clear_manual_flags",
        "task_config",
        "ckpt_path",
        "output_variant",
    }


def prepare(service, owner, location_id, operator_id, parameters):
    if operator_id not in ALLOWED_ACTIONS or not isinstance(parameters, dict):
        raise ValueError("Choose an available assistant action")
    if set(parameters) - allowed_parameters(operator_id):
        raise ValueError("Use the manual form for advanced or review-changing options")
    if not DisclosureGuard.from_environment().safe_draft(parameters):
        raise ValueError("Unsupported action parameters")
    spec = get_operator(operator_id)
    definition = {
        "name": ALLOWED_ACTIONS[operator_id],
        "steps": [
            {
                "step_id": "operation",
                "operator_id": operator_id,
                "version": spec.version,
                "parameters": parameters,
            }
        ],
    }
    preview = service.manager.preview(owner, location_id, definition)
    definition, _ = bind_source_snapshot(definition, preview["blocked"])
    preview = service.manager.preview(owner, location_id, definition)
    with service.store.sessions() as session:
        location, node = service.manager._location(session, location_id, owner)
    resolved = next(step for step in preview["definition"]["steps"] if step["step_id"] == "operation")
    return {
        "title": ALLOWED_ACTIONS[operator_id],
        "operator_id": operator_id,
        "parameters": {
            name: value
            for name, value in resolved["parameters"].items()
            if name in allowed_parameters(operator_id)
        },
        "definition": preview["definition"],
        "can_execute": preview["can_execute"],
        "blocked": preview["blocked"],
        "location_id": location_id,
        "dataset": safe_text(location["dataset_key"].rsplit("/", 1)[-1]),
        "node": safe_text(node["name"]),
        "viewer_available": operator_id == "viewer.prepare" and bool(viewer_reference(location)),
        "prefer_existing": not parameters,
    }


def current_action(service, session, conversation):
    turn = session.scalar(
        select(AssistantTurn)
        .where(
            AssistantTurn.conversation_id == conversation.conversation_id,
            AssistantTurn.result["action"]["operator_id"].as_string().in_(ALLOWED_ACTIONS),
            AssistantTurn.status == "done",
        )
        .order_by(AssistantTurn.result["revision"].as_integer().desc())
        .limit(1)
    )
    if not turn or not turn.result.get("action"):
        return None
    action = DisclosureGuard.from_environment().response(turn.result).get("action")
    if not action:
        return None
    action = dict(action)
    action.update(
        action_id=turn.request_id,
        revision=turn.result["revision"],
        is_current=turn.result["revision"] == conversation.revision,
    )
    if turn.result.get("execution"):
        action["execution"] = turn.result["execution"]
    return DisclosureGuard.from_environment().tree(action)


def confirm(service, owner, action_id, revision, *, rerun=False):
    # No client-supplied operator, parameters, dataset or model text is executable here.
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        turn = session.get(AssistantTurn, action_id)
        if turn is None or turn.owner != owner:
            raise KeyError("Action not found")
        conversation = service._conversation(session, owner, turn.conversation_id)
        service.manager._actor(session, owner)
        action = turn.result.get("action")
        if not action or action.get("operator_id") not in ALLOWED_ACTIONS:
            raise ValueError("No supported action was prepared")
        location_id = conversation.location_id
        location, _ = service.manager._location(session, location_id, owner)
        identity = location_identity(location)
        delivery = "assistant:" + action_id
        # Reuse a previously submitted run even if acknowledgement or the HTTP response was lost.
        pipeline_id = str(uuid.uuid5(uuid.NAMESPACE_URL, content_digest([owner, delivery])))
        key = content_digest([owner, pipeline_id, 1, location_id, delivery])
        previous = session.scalar(select(PipelineRun).where(PipelineRun.idempotency_key == key))
        previous_id = (turn.result.get("execution") or {}).get("run_id") or (
            previous.run_id if previous else None
        )
        if not previous_id and (
            type(revision) is not int
            or revision != conversation.revision
            or revision != turn.result.get("revision")
        ):
            raise JobConflictError("Conversation changed. Prepare the action again.")
        definition = action["definition"]
        if not DisclosureGuard.from_environment().safe_draft(definition):
            raise ValueError("Action is no longer safe to execute")
    if not previous_id:
        from lerobot.data_platform.ai_tools.operation_state import inspect_operation

        state = inspect_operation(service, owner, action)
        if state["state"] == "running":
            receipt = {**state, "reused": True}
            with service.store.sessions.begin() as session:
                scheduler_lock(session)
                if state.get("run_id"):
                    turn = session.get(AssistantTurn, action_id)
                    turn.result = {
                        **turn.result,
                        "execution": {k: receipt[k] for k in ("run_id", "status", "url")},
                    }
                service.manager._audit(
                    session,
                    owner,
                    "assistant.action.reused",
                    request_id=action_id,
                    run_id=state.get("run_id"),
                    job_id=state.get("job_id"),
                    operator_id=action["operator_id"],
                )
            return receipt
        if not state["can_confirm"]:
            raise JobConflictError(
                "Recent history is incomplete. Inspect Jobs before preparing this operation again."
            )
        if state["state"] in {"ready", "history", "failed"} and not rerun:
            raise JobConflictError(
                "An existing result or attempt is available. Review it, then explicitly run again."
            )
    if previous_id:
        run = service.manager.run(owner, previous_id)
    else:
        # Revalidate availability/parameters and preserve the immutable prepared definition.
        preview = service.manager.preview(owner, location_id, definition)
        if not preview["can_execute"]:
            raise JobConflictError("Action is currently blocked. Prepare it again to see the requirements.")
        saved = service.manager.save(owner, definition, delivery=delivery)
        run = service.manager.start(
            owner,
            saved["pipeline_id"],
            saved["revision"],
            location_id,
            delivery,
            expected_identity=identity,
            reuse_equivalent=True,
        )
    receipt = {
        "run_id": run["run_id"],
        "status": run["status"],
        "reused": run.get("reused", False),
        "url": "/pipelines?location_id=" + location_id + "&run_id=" + run["run_id"],
    }
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        turn = session.get(AssistantTurn, action_id)
        turn.result = {**turn.result, "execution": receipt}
        service.manager._audit(
            session,
            owner,
            "assistant.action.confirmed",
            request_id=action_id,
            run_id=run["run_id"],
            operator_id=action["operator_id"],
        )
    return receipt


def attach_message_actions(service, session, owner, conversation, view):
    """Attach at most six retained cards to their own assistant messages; keep legacy top-level fields."""
    if service.manager._actor(session, owner, write=False).role == "viewer":
        return
    from lerobot.data_platform.ai_tools.operation_state import action_status

    turns = session.scalars(
        select(AssistantTurn)
        .where(
            AssistantTurn.conversation_id == conversation.conversation_id,
            AssistantTurn.status == "done",
        )
        .order_by(AssistantTurn.created_at.desc(), AssistantTurn.result["revision"].as_integer().desc())
        .limit(6)
    ).all()
    used = set()
    for item in reversed(view["history"]):
        if item["role"] != "assistant":
            continue
        turn = next(
            (
                t
                for t in turns
                if t.request_id not in used
                and (
                    item.get("request_id") == t.request_id
                    if item.get("request_id")
                    else item["content"]
                    == DisclosureGuard.from_environment().response(t.result).get("message")
                )
            ),
            None,
        )
        if turn is None:
            continue
        used.add(turn.request_id)
        action = DisclosureGuard.from_environment().response(turn.result).get("action")
        if not action:
            continue
        action = {
            **action,
            "action_id": turn.request_id,
            "revision": turn.result["revision"],
            "is_current": turn.result["revision"] == conversation.revision,
        }
        if turn.result.get("execution"):
            action["execution"] = turn.result["execution"]
        item["action"] = action
        item["operation"] = (
            view["operation"]
            if (view.get("action") or {}).get("action_id") == turn.request_id
            else action_status(service, owner, turn.request_id)
        )
