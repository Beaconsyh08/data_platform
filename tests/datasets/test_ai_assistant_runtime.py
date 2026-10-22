"""Asynchronous admission, durable generation and unchanged domain confirmation."""

import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import URL, func, select

pytest.importorskip("langgraph")
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402

from lerobot.data_platform.ai_tools.actions import confirm  # noqa: E402
from lerobot.data_platform.ai_tools.models import (  # noqa: E402
    AssistantConversation,
    AssistantExecution,
    AssistantTurn,
)
from lerobot.data_platform.ai_tools.runner import AssistantRunner  # noqa: E402
from lerobot.data_platform.ai_tools.runtime import generate  # noqa: E402
from lerobot.data_platform.ai_tools.runtime_checkpointer import RuntimeSaver  # noqa: E402
from lerobot.data_platform.ai_tools.runtime_repository import (  # noqa: E402
    BudgetExceededError,
    LeaseLostError,
    RuntimeRepository,
)
from lerobot.data_platform.ai_tools.service import AssistantBusyError  # noqa: E402
from lerobot.data_platform.control_plane import ControlPlaneUser, DatasetLocation  # noqa: E402
from lerobot.data_platform.job_management import JobConflictError  # noqa: E402
from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes  # noqa: E402
from lerobot.data_platform.routes.processing import register_processing_routes  # noqa: E402
from tests.datasets.test_ai_assistant import assistant, call  # noqa: E402, F401
from tests.datasets.test_ai_assistant_context import bind_lifecycle, text_response  # noqa: E402
from tests.datasets.test_control_plane import _app  # noqa: E402


@pytest.fixture
def processing_setup(tmp_path, monkeypatch):
    """Run the same domain fixture on SQLite or a disposable real MySQL database."""
    from tests.datasets import test_processing

    socket = os.environ.get("ASSISTANT_TEST_MYSQL_SOCKET")
    if not socket:
        yield test_processing.setup.__wrapped__(tmp_path)
        return
    import pymysql

    path = Path(socket).resolve()
    assert path.is_socket() and path.parent.stat().st_uid == os.getuid()
    assert path.parent.stat().st_mode & 0o077 == 0
    database = "assistant_runtime_test_" + uuid.uuid4().hex
    with pymysql.connect(unix_socket=str(path), user="root", autocommit=True) as admin:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        store = None
        try:
            url = URL.create(
                "mysql+pymysql", username="root", database=database, query={"unix_socket": str(path)}
            )
            store = test_processing.ControlPlaneStore(url.render_as_string(hide_password=False))
            monkeypatch.setattr(test_processing, "ControlPlaneStore", lambda _: store)
            yield test_processing.setup.__wrapped__(tmp_path)
        finally:
            if store is not None:
                store.engine.dispose()
            with admin.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{database}`")


@pytest.fixture
def asynchronous(assistant):  # noqa: F811
    service, fake, owner, value = assistant
    service.config = replace(service.config, backend="langgraph")
    fake.responses = [text_response("Authorized result")]
    return service, fake, owner, value


def expire_lease(service, lease):
    with service.store.sessions.begin() as session:
        session.get(AssistantExecution, lease.request_id).lease_until = 0


def test_async_http_202_polling_and_backend_pinning(asynchronous, tmp_path):
    service, fake, owner, value = asynchronous
    app = _app(tmp_path, service.store)
    ctx = SimpleNamespace(control_plane_store=service.store, lifecycle_store=None)
    register_processing_routes(app, ctx)
    register_ai_assistant_routes(app, ctx)
    app.extensions["data_platform_assistant"] = service
    client = app.test_client()
    assert client.post("/api/ai-assistant/turns", json=value).status_code == 401
    client.post("/api/auth/login", json={"username": "admin", "password": "strong-admin-password"})
    for _ in range(2):
        response = client.post("/api/ai-assistant/turns", json=value)
        assert response.status_code == 202 and response.json["status"] == "running"
    assert fake.requests == []
    assert service.conversation(owner, value["conversation_id"])["pending_request"] == value["request_id"]
    with service.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AssistantExecution)) == 1
        assert session.get(AssistantConversation, value["conversation_id"]).revision == 1
    # Rolling back the default backend cannot execute the queued request synchronously.
    service.config = replace(service.config, backend="legacy")
    assert client.post("/api/ai-assistant/turns", json=value).status_code == 202
    assert AssistantRunner(service).tick()
    result = client.get("/api/ai-assistant/turns/" + value["request_id"]).json
    assert result["status"] == "done" and result["message"] == "Authorized result"
    assert client.post("/api/ai-assistant/turns", json=value).json == result
    assert len(fake.requests) == 1
    assert len(service.conversation(owner, value["conversation_id"])["history"]) == 2


def test_admission_is_atomic_and_legacy_shares_quota(asynchronous, monkeypatch):
    service, _, owner, value = asynchronous
    audit = service.manager._audit

    def fail(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(service.manager, "_audit", fail)
    with pytest.raises(RuntimeError):
        service.submit(owner, value)
    with service.store.sessions() as session:
        for model in (AssistantTurn, AssistantExecution, AssistantConversation):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    monkeypatch.setattr(service.manager, "_audit", audit)
    service.submit(owner, value)
    service.config = replace(service.config, backend="legacy")
    with pytest.raises(AssistantBusyError):
        service.submit(owner, {**value, "request_id": str(uuid.uuid4()), "expected_revision": 1})
    with pytest.raises(JobConflictError):
        service.submit(owner, {**value, "message": "different"})


def test_claim_race_and_stale_worker_cannot_publish(asynchronous):
    service, _, owner, value = asynchronous
    service.submit(owner, value)
    repo = RuntimeRepository(service.store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda i: repo.claim(str(i)), range(8)))
    old = next(lease for lease in leases if lease)
    assert len([lease for lease in leases if lease]) == 1
    saver = RuntimeSaver(repo, old)
    config = {"configurable": {"thread_id": old.request_id}}
    saved = saver.put(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
    repo.reserve_call(old, 0, {"test": True})
    expire_lease(service, old)
    new = repo.claim("replacement")
    assert new.epoch == old.epoch + 1
    for action in (
        lambda: saver.put(saved, empty_checkpoint(), {"source": "loop", "step": 1}, {}),
        lambda: saver.put_writes(saved, [("result", "stale")], "task"),
        lambda: repo.record_response(old, 0, {"stale": True}),
        lambda: service._complete(owner, value, [], {"message": "stale"}, lease=old),
    ):
        with pytest.raises(LeaseLostError):
            action()
    assert service.turn(owner, value["request_id"])["status"] == "running"


@pytest.mark.parametrize("saved_response", [False, True])
def test_restart_at_model_response_window(asynchronous, monkeypatch, saved_response):
    service, fake, owner, value = asynchronous
    service.submit(owner, value)
    repo = RuntimeRepository(service.store)
    lease = repo.claim("first")
    record = repo.record_response

    class ProcessLost(BaseException):
        pass

    def crash(*args):
        if saved_response:
            record(*args)
        raise ProcessLost()

    monkeypatch.setattr(repo, "record_response", crash)
    with pytest.raises(ProcessLost):
        generate(service, repo, lease)
    expire_lease(service, lease)
    assert AssistantRunner(service).tick()
    result = service.turn(owner, value["request_id"])
    assert result["status"] == ("done" if saved_response else "error")
    if not saved_response:
        assert "outcome is unknown" in result["error"]
    assert len(fake.requests) == 1


def test_checkpoint_completion_before_publication_recovers_once(asynchronous, monkeypatch):
    service, fake, owner, value = asynchronous
    from langsmith.utils import tracing_is_enabled

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    original = fake.post_chat_completion

    def without_trace(payload):
        assert not tracing_is_enabled()
        return original(payload)

    monkeypatch.setattr(fake, "post_chat_completion", without_trace)
    service.submit(owner, value)
    repo = RuntimeRepository(service.store)
    lease = repo.claim("first")
    assert generate(service, repo, lease)["message"] == "Authorized result"
    expire_lease(service, lease)
    assert AssistantRunner(service).tick()
    assert len(fake.requests) == 1
    assert service.turn(owner, value["request_id"])["status"] == "done"
    assert len(service.conversation(owner, value["conversation_id"])["history"]) == 2


def test_another_process_publishes_completed_checkpoint_without_model(asynchronous):
    service, fake, owner, value = asynchronous
    service.submit(owner, value)
    repo = RuntimeRepository(service.store)
    lease = repo.claim("lost-process")
    generate(service, repo, lease)
    expire_lease(service, lease)
    code = """
import sys
from lerobot.data_platform.control_plane import ControlPlaneStore
from lerobot.data_platform.processing.service import PipelineManager
from lerobot.data_platform.ai_tools.service import AssistantService
from lerobot.data_platform.ai_tools.runner import AssistantRunner
def no_provider(**options):
    raise AssertionError('Completed checkpoint must not call the provider')
store = ControlPlaneStore(sys.argv[1], initialize_schema=False)
service = AssistantService(PipelineManager(store), client_factory=no_provider)
assert AssistantRunner(service).tick()
store.engine.dispose()
"""
    subprocess.run(
        [sys.executable, "-c", code, service.store.engine.url.render_as_string(hide_password=False)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert service.turn(owner, value["request_id"])["status"] == "done"
    assert len(fake.requests) == 1


def test_checkpoint_transaction_rolls_back_when_lease_expires(asynchronous):
    service, _, _, value = asynchronous
    service.submit(asynchronous[2], value)
    repo = RuntimeRepository(service.store)
    lease = repo.claim("worker")
    with pytest.raises(LeaseLostError), repo.transaction(lease) as session:
        row = session.get(AssistantExecution, lease.request_id)
        row.lease_until = 0
        row.calls_used = 7
    with service.store.sessions() as session:
        row = session.get(AssistantExecution, lease.request_id)
        assert row.calls_used == 0 and row.lease_until > 0


@pytest.mark.parametrize("change", ["role", "identity", "revision"])
def test_async_rechecks_permission_identity_revision(asynchronous, tmp_path, change):
    service, fake, owner, value = asynchronous
    bind_lifecycle(service, tmp_path)
    fake.responses = [
        call("describe_operator", {"operator_id": "curation.quality"}),
        call("prepare_action", {"operator_id": "curation.quality", "parameters": {"checks": ["s3"]}}),
    ]
    service.submit(owner, value)
    with service.store.sessions.begin() as session:
        if change == "role":
            session.get(ControlPlaneUser, owner).role = "viewer"
        elif change == "identity":
            session.get(DatasetLocation, value["location_id"]).details = {"fingerprint": "changed"}
        else:
            session.get(AssistantConversation, value["conversation_id"]).revision += 1
    AssistantRunner(service).tick()
    with service.store.sessions() as session:
        turn = session.get(AssistantTurn, value["request_id"])
        assert not turn.result.get("action")
        if change != "role":
            assert turn.status == "error"
    assert not service.store.list_jobs()


def test_async_s3_confirmation_is_explicit_and_idempotent(asynchronous, tmp_path):
    service, fake, owner, value = asynchronous
    bind_lifecycle(service, tmp_path)
    fake.responses = [
        call("describe_operator", {"operator_id": "curation.quality"}),
        call("prepare_action", {"operator_id": "curation.quality", "parameters": {"checks": ["s3"]}}),
    ]
    service.submit(owner, value)
    AssistantRunner(service).tick()
    result = service.turn(owner, value["request_id"])
    assert result["status"] == "done" and result["action"]["can_execute"]
    assert not service.manager.runs(owner) and not service.store.list_jobs()
    first = confirm(service, owner, result["request_id"], result["revision"])
    second = confirm(service, owner, result["request_id"], result["revision"])
    assert first["run_id"] == second["run_id"]
    assert len(service.manager.runs(owner)) == 1


def test_expiry_budget_version_and_owner_boundaries(asynchronous):
    service, _, owner, value = asynchronous
    service.submit(owner, value)
    repo = RuntimeRepository(service.store)
    other = service.store.register_user(username="other", password="strong-other-password")
    with pytest.raises(KeyError):
        service.turn(other["user_id"], value["request_id"])
    with service.store.sessions.begin() as session:
        session.get(AssistantExecution, value["request_id"]).graph_version = "future"
    assert repo.claim("old-runner") is None
    with service.store.sessions.begin() as session:
        row = session.get(AssistantExecution, value["request_id"])
        row.graph_version, row.queue_until = "bounded-v1", 0
    assert service.turn(owner, value["request_id"])["status"] == "error"
    value = {**value, "request_id": str(uuid.uuid4()), "expected_revision": 1}
    service.submit(owner, value)
    lease = repo.claim("runner")
    for index in range(8):
        repo.reserve_call(lease, index, {"call": index})
        repo.record_response(lease, index, {"ok": True})
    expire_lease(service, lease)
    resumed = repo.claim("restarted")
    assert repo.reserve_call(resumed, 0, {"call": 0}) == {"ok": True}
    with pytest.raises(BudgetExceededError):
        repo.reserve_call(resumed, 8, {"call": 8})
