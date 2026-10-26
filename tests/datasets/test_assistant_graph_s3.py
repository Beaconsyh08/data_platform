"""S3 graph integration in private fixtures; model transport is deterministic."""

import json
import uuid

import pytest

pytest.importorskip("langgraph.checkpoint.mysql")
from experiments.assistant_langgraph.checkpointer import FencedMySQLSaver  # noqa: E402
from experiments.assistant_langgraph.s3_graph import build_graph  # noqa: E402
from lerobot.data_platform.ai_tools.actions import confirm  # noqa: E402
from lerobot.data_platform.ai_tools.personalization import DEFAULTS, preferences  # noqa: E402
from lerobot.data_platform.control_plane import ControlPlaneUser, DatasetLocation  # noqa: E402
from lerobot.data_platform.job_management import JobConflictError  # noqa: E402
from tests.datasets.test_ai_assistant import assistant, call, processing_setup  # noqa: E402, F401
from tests.datasets.test_ai_assistant_context import bind_lifecycle, text_response  # noqa: E402
from tests.datasets.test_assistant_graph_mysql import config, database, expire, pytestmark  # noqa: E402, F401


@pytest.fixture
def graph_assistant(assistant, database, tmp_path, monkeypatch):  # noqa: F811
    service, fake, owner, body = assistant
    bind_lifecycle(service, tmp_path)
    body["message"] = "只准备 S3 数值有效性检查，确认后再运行。"
    _, connect, repo = database

    def generate(actor, value, history, proposal, identity):
        execution_id = str(uuid.uuid4())
        repo.enqueue(execution_id)
        lease = repo.claim(execution_id, "test-worker")
        with connect() as connection:
            graph = build_graph(
                service,
                actor,
                value,
                history,
                identity,
                repo,
                lease,
                FencedMySQLSaver(connection, lease),
            )
            state = graph.invoke({}, config(lease), durability="sync", recursion_limit=32)
        repo.finish(lease, state["result"])
        return state["result"]

    monkeypatch.setattr(service, "_generate", generate)
    return service, fake, owner, body


def s3_call():
    return call("prepare_action", {"operator_id": "curation.quality", "parameters": {"checks": ["s3"]}})


def test_s3_context_and_preferences_precede_model_confirmation_remains_idempotent(graph_assistant):
    service, fake, owner, body = graph_assistant
    preferences(
        service,
        owner,
        change={"expected_revision": 0, "settings": {**DEFAULTS, "language": "zh", "detail": "detailed"}},
    )
    # The model intentionally skips both read_context and describe_operator.
    fake.responses = [s3_call()]
    result = service.submit(owner, body)
    assert result["status"] == "done" and result["action"]["can_execute"]
    context = json.loads(fake.requests[0]["messages"][2]["content"])
    assert context["preferences"]["detail"] == "detailed"
    assert context["operator"]["operator_id"] == "curation.quality"
    assert fake.requests[0]["enable_thinking"] is False
    assert not service.manager.runs(owner) and not service.store.list_jobs()
    first = confirm(service, owner, result["request_id"], result["revision"])
    second = confirm(service, owner, result["request_id"], result["revision"])
    assert first["run_id"] == second["run_id"]
    assert len(service.manager.runs(owner)) == 1
    service.manager.tick(first["run_id"])
    assert len(service.store.list_jobs()) == 1


@pytest.mark.parametrize("change", ["role", "identity"])
def test_s3_rechecks_access_and_identity_after_model(graph_assistant, change, monkeypatch):
    service, fake, owner, body = graph_assistant

    def respond(payload):
        with service.store.sessions.begin() as session:
            if change == "role":
                session.get(ControlPlaneUser, owner).role = "viewer"
            else:
                session.get(DatasetLocation, body["location_id"]).details = {"fingerprint": "changed"}
        return s3_call()

    monkeypatch.setattr(fake, "post_chat_completion", respond)
    with pytest.raises(PermissionError if change == "role" else JobConflictError):
        service.submit(owner, body)
    assert not service.store.list_jobs()


def test_model_cannot_confirm_or_invent_extra_actions(graph_assistant):
    service, fake, owner, body = graph_assistant
    fake.responses = [call("confirm_action", {"approved": True}), text_response("仍需用户确认")]
    result = service.submit(owner, body)
    assert result["status"] == "done" and not result.get("action")
    assert not service.manager.runs(owner) and not service.store.list_jobs()
    assert all(tool["function"]["name"] != "confirm_action" for tool in fake.requests[0]["tools"])


def test_disabled_preferences_do_not_enter_context(graph_assistant):
    service, fake, owner, body = graph_assistant
    preferences(service, owner, change={"expected_revision": 0, "settings": {**DEFAULTS, "enabled": False}})
    fake.responses = [text_response("说明")]
    service.submit(owner, body)
    assert json.loads(fake.requests[0]["messages"][2]["content"])["preferences"] == {}


def test_s3_recovers_after_response_receipt_before_checkpoint(assistant, database, tmp_path, monkeypatch):  # noqa: F811
    service, fake, owner, body = assistant
    bind_lifecycle(service, tmp_path)
    _, connect, repo = database
    execution_id = str(uuid.uuid4())
    repo.enqueue(execution_id)
    lease = repo.claim(execution_id, "first")
    with service.store.sessions() as session:
        identity = service._target(session, owner, body["location_id"])
    fake.responses = [s3_call()]
    record = repo.record_response

    def crash_after_record(*args):
        record(*args)
        raise RuntimeError("Injected crash after model receipt")

    monkeypatch.setattr(repo, "record_response", crash_after_record)
    with connect() as connection:
        graph = build_graph(
            service, owner, body, [], identity, repo, lease, FencedMySQLSaver(connection, lease)
        )
        with pytest.raises(RuntimeError, match="Injected"):
            graph.invoke({}, config(lease), durability="sync")
    expire(repo, lease)
    resumed = repo.claim(execution_id, "replacement")
    monkeypatch.setattr(repo, "record_response", record)
    with connect() as connection:
        graph = build_graph(
            service, owner, body, [], identity, repo, resumed, FencedMySQLSaver(connection, resumed)
        )
        state = graph.invoke(None, config(resumed), durability="sync")
    assert state["result"]["action"]["can_execute"]
    assert len(fake.requests) == 1  # Reuse the durable response without another provider request.
    assert not service.manager.runs(owner) and not service.store.list_jobs()
