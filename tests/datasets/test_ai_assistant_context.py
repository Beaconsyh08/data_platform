"""Preview repair, read scopes and bounded evidence never submit compute."""

import copy
import json
import uuid

import pytest

from lerobot.data_platform.ai_tools.context import TOOL_BYTES, bounded, resolve_context, safe_text
from lerobot.data_platform.control_plane import ControlPlaneNode, ControlPlaneUser, RemoteJob
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.management_storage import JobControl
from tests.datasets.test_ai_assistant import assistant, call, draft, processing_setup  # noqa: F401


def text_response(text):
    return {"choices": [{"message": {"content": text}}]}


def quality_plan():
    return {
        "name": "quality_then_viewer",
        "steps": [
            {
                "step_id": "quality",
                "operator_id": "curation.quality",
                "version": "1.0.0",
                "parameters": {"checks": ["s3"]},
            },
            {"step_id": "viewer", "operator_id": "viewer.prepare", "version": "1.0.0", "parameters": {}},
        ],
    }


def bind_lifecycle(service, tmp_path):
    service.manager.lifecycle = LifecycleStore(tmp_path / "ledger")
    with service.store.sessions.begin() as session:
        node = session.query(ControlPlaneNode).first()
        node.capabilities = {**node.capabilities, "quality_checks": ["s3"]}


def test_snapshot_repair_loop_returns_validated_draft_without_job(assistant, tmp_path):  # noqa: F811
    service, fake, owner, value = assistant
    bind_lifecycle(service, tmp_path)
    initial = quality_plan()
    fixed = copy.deepcopy(initial)
    fixed["steps"].insert(
        0, {"step_id": "snapshot", "operator_id": "curation.snapshot", "version": "1.0.0", "parameters": {}}
    )
    fake.responses = [
        call("describe_operator", {"operator_id": op}) for op in ("curation.quality", "viewer.prepare")
    ]
    fake.responses += [
        call("preview_pipeline", {"summary": "检测并准备浏览", "definition": initial}),
        call("describe_operator", {"operator_id": "curation.snapshot"}),
        call("preview_pipeline", {"summary": "已补齐快照", "definition": fixed}),
    ]
    result = service.submit(owner, value)
    assert result["proposal"]["can_execute"]
    assert result["proposal"]["added_steps"] == ["snapshot"]
    assert result["proposal"]["repairs"][0]["action"] == "added"
    assert service.store.list_jobs() == [] and service.manager.list(owner) == []
    assert service.manager.preview(owner, value["location_id"], fixed)["blocked"] == []


def test_repeated_blocked_plan_stops_and_retains_draft(assistant, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "lerobot.data_platform.ai_tools.repairs.bind_source_snapshot",
        lambda definition, blocked: (definition, []),
    )
    service, fake, owner, value = assistant
    bind_lifecycle(service, tmp_path)
    initial = quality_plan()
    fake.responses = [
        call("describe_operator", {"operator_id": op}) for op in ("curation.quality", "viewer.prepare")
    ]
    fake.responses += [call("preview_pipeline", {"summary": "草稿", "definition": initial})] * 2
    result = service.submit(owner, value)
    assert result["status"] == "done" and not result["proposal"]["can_execute"]
    assert len(fake.requests) == 4
    assert {item["position"] for item in result["proposal"]["blocked"]} == {1, 2}


def test_repair_cannot_remove_requested_operation(assistant, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "lerobot.data_platform.ai_tools.repairs.bind_source_snapshot",
        lambda definition, blocked: (definition, []),
    )
    service, fake, owner, value = assistant
    bind_lifecycle(service, tmp_path)
    initial = quality_plan()
    weaker = copy.deepcopy(initial)
    weaker["steps"].pop()
    fake.responses = [
        call("describe_operator", {"operator_id": op}) for op in ("curation.quality", "viewer.prepare")
    ]
    fake.responses += [
        call("preview_pipeline", {"summary": "草稿", "definition": initial}),
        call("preview_pipeline", {"summary": "删掉步骤", "definition": weaker}),
        text_response("需要先解决阻塞"),
    ]
    result = service.submit(owner, value)
    assert len(result["proposal"]["definition"]["steps"]) == 2
    assert "error" in fake.requests[-1]["messages"][-1]["content"]


def test_structured_parse_failure_preserves_strict_save(assistant):  # noqa: F811
    service, _, owner, value = assistant
    definition = {"name": "Evidence", "steps": [{"step_id": "projection", "operator_id": "curation.project"}]}
    preview = service.manager.preview(owner, value["location_id"], definition)
    assert preview["blocked"][0]["code"] == "missing_evidence"
    assert preview["blocked"][0]["step_id"] == "projection"
    with pytest.raises(ValueError, match="Missing required evidence"):
        service.manager.save(owner, definition)
    definition["steps"][0]["input"] = "future"
    assert (
        service.manager.preview(owner, value["location_id"], definition)["blocked"][0]["code"]
        == "invalid_input"
    )


def test_viewer_can_explain_but_cannot_preview(assistant):  # noqa: F811
    service, fake, owner, value = assistant
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).role = "viewer"
    fake.responses = [call("read_context", {}), text_response("字段信息来自 dataset 摘要")]
    result = service.submit(
        owner, {**value, "context": {"type": "dataset", "target_id": value["location_id"]}}
    )
    assert result["status"] == "done" and result["proposal"] is None
    assert all(t["function"]["name"] != "preview_pipeline" for t in fake.requests[0]["tools"])
    fake.responses = [call("preview_pipeline", {"definition": draft(), "summary": "run"})]
    with pytest.raises(PermissionError):
        service.submit(
            owner, {**value, "request_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4())}
        )
    assert service.store.list_jobs() == []


def test_job_context_is_scoped_sanitized_and_attempt_bound(assistant):  # noqa: F811
    service, fake, owner, value = assistant
    job = service.store.create_job(
        location_id=value["location_id"], requested_by=owner, operation="viewer.prepare"
    )
    with service.store.sessions.begin() as session:
        session.get(
            RemoteJob, job["job_id"]
        ).error = "Failed /private/input.parquet token=secret sk-fake-test-secret"
    body = {**value, "context": {"type": "job", "target_id": job["job_id"]}}
    fake.responses = [call("read_context", {}), text_response("任务在排队，详情见 job")]
    result = service.submit(owner, body)
    assert result["status"] == "done"
    sent = json.dumps(fake.requests)
    assert "/private/" not in sent and "sk-fake" not in sent and "token=secret" not in sent
    assert "queue_wait_reason" in sent
    with service.store.sessions.begin() as session:
        session.get(JobControl, job["job_id"]).attempt_id = str(uuid.uuid4())
    with pytest.raises(JobConflictError):
        service.conversation(owner, value["conversation_id"])


def test_context_limits_and_redaction():
    assert bounded({"data": "x" * TOOL_BYTES})["truncated"]
    assert (
        safe_text("https://example.test/private /private/file api_key=abc")
        == "[redacted] [redacted] [redacted]"
    )


def test_quality_context_reads_bounded_report_and_binds_fingerprint(assistant, tmp_path):  # noqa: F811
    service, fake, owner, value = assistant
    bind_lifecycle(service, tmp_path)
    job = service.store.create_job(
        location_id=value["location_id"], requested_by=owner, operation="curation.quality"
    )
    with service.store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = "done"
    ledger = service.manager.lifecycle
    ledger._put_record(
        "curation_runs",
        job["job_id"],
        {"target": {"location_id": value["location_id"], "dataset_version_id": "version"}},
    )
    directory = ledger.root / "curation" / "runs" / job["job_id"] / "static"
    directory.mkdir(parents=True)
    path = directory / "quality_flagged_episodes.json"
    path.write_text(
        json.dumps({"summary": {"flagged": 2}, "flag_reasons": {str(i): {"count": 1} for i in range(30)}})
    )
    scope = {"type": "quality", "target_id": job["job_id"]}
    with service.store.sessions() as session:
        context = resolve_context(service.manager, session, owner, value["location_id"], scope)
    assert context["summary"]["summary"]["flagged"] == 2
    assert len(context["summary"]["examples"]) == 20
    fake.responses = [text_response("两个候选，需要人工复核 quality")]
    service.submit(owner, {**value, "context": scope})
    path.write_text("{}")
    with pytest.raises(JobConflictError):
        service.conversation(owner, value["conversation_id"])


def test_snapshot_repair_moves_existing_step_without_duplication():
    from lerobot.data_platform.ai_tools.repairs import bind_source_snapshot

    definition = quality_plan()
    definition["steps"].append({"step_id": "bind", "operator_id": "curation.snapshot", "parameters": {}})
    fixed, repairs = bind_source_snapshot(definition, [{"code": "missing_snapshot"}])
    assert [row["step_id"] for row in fixed["steps"]] == ["bind", "quality", "viewer"]
    assert repairs[0]["action"] == "moved"
    assert definition["steps"][0]["step_id"] == "quality"


def test_provider_failure_keeps_last_blocked_draft(assistant, tmp_path, monkeypatch):  # noqa: F811
    service, fake, owner, value = assistant
    bind_lifecycle(service, tmp_path)
    monkeypatch.setattr(
        "lerobot.data_platform.ai_tools.repairs.bind_source_snapshot",
        lambda definition, blocked: (definition, []),
    )
    fake.responses = [
        call("describe_operator", {"operator_id": op}) for op in ("curation.quality", "viewer.prepare")
    ]
    fake.responses += [call("preview_pipeline", {"summary": "Blocked", "definition": quality_plan()})]
    result = service.submit(owner, value)
    assert result["proposal"] and not result["proposal"]["can_execute"]


def test_job_context_does_not_bypass_owner_visibility(assistant):  # noqa: F811
    service, _, owner, value = assistant
    job = service.store.create_job(
        location_id=value["location_id"], requested_by=owner, operation="viewer.prepare"
    )
    other = service.store.register_user(
        username="other-viewer", password="strong-viewer-password", display_name="Viewer"
    )
    service.store.update_user(other["user_id"], active=True)
    with service.store.sessions() as session, pytest.raises(KeyError, match="Job not found"):
        resolve_context(
            service.manager,
            session,
            other["user_id"],
            value["location_id"],
            {"type": "job", "target_id": job["job_id"]},
        )


def test_legacy_conversation_schema_migrates_additively(assistant):  # noqa: F811
    from sqlalchemy import inspect

    from lerobot.data_platform.management_storage import migrate_management

    service, _, owner, value = assistant
    service.submit(owner, value)
    with service.store.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE dp_ai_conversations DROP COLUMN context")
    migrate_management(service.store.engine)
    assert "context" in {
        column["name"] for column in inspect(service.store.engine).get_columns("dp_ai_conversations")
    }
    restored = service.conversation(owner, value["conversation_id"])
    assert restored["context"]["type"] == "pipeline" and restored["revision"] == 1


def test_node_choices_do_not_mutate_shared_operator_schema(assistant):  # noqa: F811
    from lerobot.data_platform.operators import get_operator
    from lerobot.data_platform.processing.capabilities import OperatorCatalog

    service, _, owner, value = assistant
    spec = get_operator("curation.quality")
    original = copy.deepcopy(spec.to_dict())
    described = OperatorCatalog(service.manager, owner, value["location_id"]).describe(spec)
    assert "enum" in described["parameters"]["properties"]["checks"]["items"]
    assert spec.to_dict() == original


def test_legacy_job_inputs_recheck_all_nested_dataset_scopes(assistant, monkeypatch):  # noqa: F811
    service, _, owner, value = assistant
    job = service.store.create_job(
        location_id=value["location_id"], requested_by=owner, operation="viewer.prepare"
    )
    with service.store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).options = {"legacy": {"src_keys": ["private/source"]}}
    monkeypatch.setattr(service.store, "restricted_dataset_keys", lambda owner: {"public/source"})
    with service.store.sessions() as session, pytest.raises(PermissionError, match="Task input"):
        resolve_context(
            service.manager, session, owner, value["location_id"], {"type": "job", "target_id": job["job_id"]}
        )
