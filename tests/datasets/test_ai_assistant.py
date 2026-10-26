"""Assistant requests are scoped, bounded, idempotent, and cannot submit computations."""

import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lerobot.data_platform.ai_tools.models import AssistantConversation, AssistantTurn
from lerobot.data_platform.ai_tools.service import (
    AssistantBusyError,
    AssistantConfig,
    AssistantService,
    AssistantUnavailableError,
)
from lerobot.data_platform.control_plane import ControlPlaneUser, DatasetLocation
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.management_storage import AuditOutbox
from lerobot.data_platform.operators import get_operator
from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_control_plane import _app
from tests.datasets.test_processing import setup as processing_setup  # noqa: F401


def call(name, arguments):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(uuid.uuid4()),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def draft():
    return {
        "name": "First episode",
        "steps": [
            {
                "step_id": "subset",
                "operator_id": "preprocess.split",
                "version": get_operator("preprocess.split").version,
                "parameters": {"episode_range": "0:1"},
            }
        ],
    }


class FakeQwen:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = responses or [
            call("describe_operator", {"operator_id": "preprocess.split"}),
            call(
                "preview_pipeline", {"summary": "提取第一个 episode，生成独立副本。", "definition": draft()}
            ),
        ]

    def post_chat_completion(self, payload):
        self.requests.append(copy.deepcopy(payload))
        return self.responses.pop(0)


@pytest.fixture
def assistant(processing_setup, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-server-only-key")
    _, manager, actor, _, location = processing_setup
    fake = FakeQwen()
    service = AssistantService(manager, client_factory=lambda **kwargs: fake)
    value = {
        "request_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "location_id": location["location_id"],
        "expected_revision": 0,
        "message": "取第一个 episode",
    }
    return service, fake, actor["user_id"], value


def test_draft_is_validated_private_and_never_runs(assistant):
    service, fake, owner, value = assistant
    result = service.submit(owner, value)
    assert result["status"] == "done"
    assert result["proposal"]["definition"]["steps"][0]["version"] == get_operator("preprocess.split").version
    assert service.store.list_jobs() == []
    assert service.manager.list(owner) == []
    payload = json.dumps(fake.requests)
    with service.store.sessions() as session:
        location = session.get(DatasetLocation, value["location_id"])
        assert location.root not in payload
        assert "test-server-only-key" not in payload
        events = json.dumps(
            [item.payload for item in session.scalars(select(AuditOutbox))], ensure_ascii=False
        )
        assert value["message"] not in events
        assert result["message"] not in events
    restored = service.conversation(owner, value["conversation_id"])
    assert restored["revision"] == 1 and len(restored["history"]) == 2
    assert service.submit(owner, value) == result
    assert len(fake.requests) == 2
    with pytest.raises(JobConflictError):
        service.submit(owner, {**value, "message": "different content"})


def test_tools_cannot_execute_or_accept_unvalidated_plans(assistant):
    service, fake, owner, value = assistant
    fake.responses = [
        call("run_pipeline", {"definition": draft()}),
        call("preview_pipeline", {"summary": "Run", "definition": draft()}),
        call("describe_operator", {"operator_id": "delete_everything"}),
        {"choices": [{"message": {"content": "请在编辑器确认后运行。"}}]},
    ]
    result = service.submit(owner, value)
    assert result["proposal"] is None
    assert service.store.list_jobs() == []
    assert all("error" in request["messages"][-1]["content"] for request in fake.requests[1:])


def test_identity_and_permissions_rechecked_after_model_call(assistant):
    service, fake, owner, value = assistant

    def change(_payload):
        with service.store.sessions.begin() as session:
            session.get(DatasetLocation, value["location_id"]).details = {"fingerprint": "changed"}
        return {"choices": [{"message": {"content": "Draft ready"}}]}

    fake.post_chat_completion = change
    assert service.submit(owner, value)["status"] == "error"
    with pytest.raises(JobConflictError):
        service.conversation(owner, value["conversation_id"])
    assert service.store.list_jobs() == []


def test_scope_revocation_and_cross_user_access(assistant, monkeypatch):
    service, fake, owner, value = assistant
    result = service.submit(owner, value)
    other = service.store.register_user(
        username="other", password="strong-other-password", display_name="Other"
    )
    with pytest.raises(KeyError):
        service.conversation(other["user_id"], value["conversation_id"])
    with pytest.raises(KeyError):
        service.turn(other["user_id"], result["request_id"])
    monkeypatch.setattr(service.store, "dataset_access_allowed", lambda *args: False)
    with pytest.raises(PermissionError):
        service.conversation(owner, value["conversation_id"])
    with pytest.raises(PermissionError):
        service.submit(owner, value)


def test_inflight_duplicate_and_quota_are_transactional(assistant):
    service, fake, owner, value = assistant
    entered, release = threading.Event(), threading.Event()

    def blocked(_payload):
        entered.set()
        assert release.wait(10)
        return {"choices": [{"message": {"content": "What would you like to process?"}}]}

    fake.post_chat_completion = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.submit, owner, value)
        assert entered.wait(10)
        try:
            assert service.submit(owner, value)["status"] == "running"
            with pytest.raises(AssistantBusyError):
                service.submit(owner, {**value, "request_id": str(uuid.uuid4())})
        finally:
            release.set()
        assert pending.result()["status"] == "done"
    service.config = AssistantConfig(enabled=True, hourly_limit=1)
    with pytest.raises(AssistantBusyError):
        service.submit(owner, {**value, "request_id": str(uuid.uuid4()), "expected_revision": 1})


def test_expired_request_cannot_overwrite_new_conversation_state(assistant):
    service, fake, owner, value = assistant

    def expire(_payload):
        with service.store.sessions.begin() as session:
            session.get(AssistantTurn, value["request_id"]).lease_until = 1
        return {"choices": [{"message": {"content": "late reply"}}]}

    fake.post_chat_completion = expire
    result = service.submit(owner, value)
    assert result["status"] == "error" and "expired" in result["error"]
    assert service.conversation(owner, value["conversation_id"])["history"] == []


def test_failure_recovers_revision_and_does_not_log_provider_body(assistant):
    service, fake, owner, value = assistant

    def fail(_payload):
        raise RuntimeError("provider echoed test-server-only-key and private prompt")

    fake.post_chat_completion = fail
    result = service.submit(owner, value)
    assert result["status"] == "error" and result["revision"] == 1
    assert "test-server-only-key" not in json.dumps(result)
    assert service.conversation(owner, value["conversation_id"])["revision"] == 1


def test_missing_key_and_disabled_feature_do_not_reserve(assistant, monkeypatch):
    service, _, owner, value = assistant
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.delenv("QWEN_DASHSCOPE_API_KEY", raising=False)
    assert not service.capabilities(owner)["available"]
    with pytest.raises(AssistantUnavailableError):
        service.submit(owner, value)
    with service.store.sessions() as session:
        assert session.get(AssistantTurn, value["request_id"]) is None


@pytest.mark.parametrize("response", [{}, {"choices": []}, {"choices": [{"message": {"tool_calls": [{}]}}]}])
def test_malformed_provider_response_is_a_recoverable_error(assistant, response):
    service, fake, owner, value = assistant
    fake.responses = [response]
    result = service.submit(owner, value)
    assert result["status"] == "error"
    assert service.conversation(owner, value["conversation_id"])["revision"] == 1


def test_http_auth_roles_validation_and_server_only_configuration(assistant, tmp_path):
    service, fake, owner, value = assistant
    app = _app(tmp_path, service.store)
    ctx = SimpleNamespace(control_plane_store=service.store, lifecycle_store=None)
    register_processing_routes(app, ctx)
    register_ai_assistant_routes(app, ctx)
    app.extensions["data_platform_assistant"] = service
    client = app.test_client()
    assert client.post("/api/ai-assistant/turns", json=value).status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "strong-admin-password"}
        ).status_code
        == 200
    )
    assert client.post("/api/ai-assistant/turns", json={**value, "api_key": "client-key"}).status_code == 400
    assert client.post("/api/ai-assistant/turns", json={**value, "message": "x" * 20000}).status_code == 413
    assert client.post("/api/ai-assistant/turns", json=value).status_code == 200
    assert (
        client.post(
            "/api/ai-assistant/turns",
            json=value,
            environ_overrides={
                "CONTENT_LENGTH": "",
                "wsgi.input_terminated": True,
            },
        ).status_code
        == 411
    )
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).role = "viewer"
    assert client.get("/api/ai-assistant/capabilities").json["available"]
    assert not client.get("/api/ai-assistant/capabilities").json["can_edit"]
    assert client.post("/api/ai-assistant/turns", json=value).json["proposal"] is None
    assert client.get("/api/ai-assistant/conversations/" + value["conversation_id"]).json["proposal"] is None


def test_retention_history_and_stale_revision(assistant):
    service, fake, owner, value = assistant
    service.submit(owner, value)
    with service.store.sessions.begin() as session:
        row = session.get(AssistantConversation, value["conversation_id"])
        row.history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(12)]
    with pytest.raises(JobConflictError):
        service.submit(owner, {**value, "request_id": str(uuid.uuid4())})
    fake.responses = [{"choices": [{"message": {"content": "Clarification"}}]}]
    service.submit(owner, {**value, "request_id": str(uuid.uuid4()), "expected_revision": 1})
    assert len(service.conversation(owner, value["conversation_id"])["history"]) == 12


def test_pending_restore_and_receipt_retention_in_active_conversation(assistant):
    service, _, owner, value = assistant
    service.submit(owner, value)
    with service.store.sessions.begin() as session:
        session.get(AssistantTurn, value["request_id"]).created_at = 1
    pending = {**value, "request_id": str(uuid.uuid4()), "expected_revision": 1}
    service._reserve(owner, pending)
    assert service.conversation(owner, value["conversation_id"])["pending_request"] == pending["request_id"]
    with service.store.sessions() as session:
        assert session.get(AssistantTurn, value["request_id"]) is None


def test_http_audit_excludes_prompt_and_generated_response(assistant, tmp_path):
    from lerobot.data_platform.operation_log import LOG_FILENAME
    from tests.datasets.test_console_services import console_app

    service, _, _, value = assistant
    app = console_app(tmp_path, database_url=str(service.store.engine.url))
    app.extensions["data_platform_assistant"] = service
    client = app.test_client()
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "strong-admin-password",
            },
        ).status_code
        == 200
    )
    response = client.post("/api/ai-assistant/turns?message=query-private", json=value)
    assert response.status_code == 200, response.json
    events = [
        json.loads(line) for path in tmp_path.rglob(LOG_FILENAME) for line in path.read_text().splitlines()
    ]
    assert events
    text = json.dumps(events, ensure_ascii=False)
    assert value["message"] not in text
    assert response.json["message"] not in text
    assert "query-private" not in text


def test_qwen_transport_caps_response_without_exposing_provider_body(monkeypatch):
    import io

    from lerobot.data_platform import qwen

    response = io.BytesIO(b'{"private": "' + b"x" * 1000 + b'"}')
    monkeypatch.setattr(
        qwen.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response),
    )
    with pytest.raises(RuntimeError, match="invalid response") as error:
        qwen.QwenClient(api_key="test-key", max_response_bytes=100).post_chat_completion({})
    assert "private" not in str(error.value)
