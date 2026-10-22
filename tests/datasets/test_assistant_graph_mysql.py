"""Real MySQL acceptance; opt in with an isolated user-owned test socket.

Creates and drops only a randomly named test database. No production credentials,
control-plane configuration, or live platform tables are used.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ASSISTANT_TEST_MYSQL_SOCKET"), reason="Isolated MySQL socket not configured"
)
pymysql = pytest.importorskip("pymysql")
pytest.importorskip("langgraph.checkpoint.mysql")
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from experiments.assistant_langgraph.checkpointer import (  # noqa: E402
    FencedMySQLSaver,
    NamespacedMySQLSaver,
)
from experiments.assistant_langgraph.repository import (  # noqa: E402
    BudgetExceededError,
    ExecutionRepository,
    LeaseLostError,
    OutcomeUnknownError,
)


@pytest.fixture(scope="module")
def database():
    socket = Path(os.environ["ASSISTANT_TEST_MYSQL_SOCKET"]).resolve()
    assert socket.is_socket() and socket.parent.stat().st_uid == os.getuid()
    assert socket.parent.stat().st_mode & 0o077 == 0, "Test socket directory must be private"
    name = "assistant_graph_test_" + uuid.uuid4().hex

    def connect():
        return pymysql.connect(unix_socket=str(socket), user="root", database=name, autocommit=True)

    with pymysql.connect(unix_socket=str(socket), user="root", autocommit=True) as admin:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        try:
            repo = ExecutionRepository(connect)
            repo.setup()
            with connect() as connection:
                NamespacedMySQLSaver(connection).setup()
                NamespacedMySQLSaver(connection).setup()
            yield name, connect, repo
        finally:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{name}`")


@pytest.fixture
def execution(database):
    _, connect, repo = database
    execution_id = str(uuid.uuid4())
    repo.enqueue(execution_id)
    lease = repo.claim(execution_id, "first")
    return connect, repo, lease


def expire(repo, lease):
    # Fault injection, not a production repository method.
    with repo.transaction() as cursor:
        cursor.execute(
            "UPDATE dp_ai_executions SET lease_until=0 WHERE execution_id=%s", (lease.execution_id,)
        )


def config(lease):
    return {"configurable": {"thread_id": lease.execution_id, "checkpoint_ns": ""}}


def test_concurrent_claim_has_one_owner(database):
    _, _, repo = database
    execution_id = str(uuid.uuid4())
    repo.enqueue(execution_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda i: repo.claim(execution_id, f"worker-{i}"), range(8)))
    assert len([lease for lease in claims if lease]) == 1


def test_claim_waits_for_checkpoint_transaction(execution):
    connect, repo, lease = execution
    locked, release, claiming = threading.Event(), threading.Event(), threading.Event()

    def write():
        with connect() as connection:
            saver = FencedMySQLSaver(connection, lease)
            with saver._cursor(pipeline=True):
                locked.set()
                assert release.wait(5)

    def claim():
        claiming.set()
        return repo.claim(lease.execution_id, "contender")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        assert locked.wait(5)
        contender = pool.submit(claim)
        try:
            assert claiming.wait(5)
            # The second connection must not pass the row lock before the write commits.
            from concurrent.futures import TimeoutError as FutureTimeoutError

            with pytest.raises(FutureTimeoutError):
                contender.result(timeout=0.1)
        finally:
            release.set()
        writer.result(timeout=5)
        assert contender.result(timeout=5) is None


def test_expired_worker_cannot_save_checkpoint_pending_write_or_result(execution):
    connect, repo, old = execution
    repo.reserve_call(old, "model-0", "a" * 64)
    with connect() as connection:
        saver = FencedMySQLSaver(connection, old)
        checkpoint = empty_checkpoint()
        saved = saver.put(config(old), checkpoint, {"source": "input", "step": 0}, {})
        expire(repo, old)
        new = repo.claim(old.execution_id, "second")
        assert new.epoch == old.epoch + 1
        for action in (
            lambda: saver.put(saved, empty_checkpoint(), {"source": "loop", "step": 1}, {}),
            lambda: saver.put_writes(saved, [("answer", "late")], "task"),
            lambda: repo.finish(old, {"answer": "late"}),
            lambda: repo.renew(old),
            lambda: repo.record_response(old, "model-0", {"answer": "late"}),
        ):
            with pytest.raises(LeaseLostError):
                action()
    with connect() as connection:
        accepted = FencedMySQLSaver(connection, new).get_tuple(config(new))
        assert accepted.checkpoint["id"] == checkpoint["id"]
        assert not accepted.pending_writes
    repo.finish(new, {"error": "outcome_unknown"}, failed=True)
    assert repo.claim(new.execution_id, "third") is None


def test_expiry_during_transaction_rolls_back_write(execution):
    connect, repo, lease = execution
    with connect() as connection:
        saver = FencedMySQLSaver(connection, lease)
        with pytest.raises(LeaseLostError), saver._cursor(pipeline=True) as cursor:
            cursor.execute(
                "INSERT INTO dp_ai_model_calls (execution_id,call_id,request_digest) VALUES (%s,%s,%s)",
                (lease.execution_id, "rolled-back", "a" * 64),
            )
            cursor.execute(
                "UPDATE dp_ai_executions SET lease_until=0 WHERE execution_id=%s", (lease.execution_id,)
            )
    # Both the write and injected expiry were rolled back.
    assert repo.reserve_call(lease, "rolled-back", "a" * 64) is None


def test_saver_rejects_other_execution_and_namespace(execution):
    connect, _, lease = execution
    with connect() as connection:
        saver = FencedMySQLSaver(connection, lease)
        payload = {"text": "checkpoints checkpoint_writes checkpoint_blobs 中文"}
        with saver._cursor(pipeline=True) as cursor:
            cursor.executemany(
                "INSERT INTO dp_ai_model_calls (execution_id,call_id,request_digest,response) "
                "VALUES (%s,%s,%s,%s)",
                [(lease.execution_id, "literal-test", "a" * 64, json.dumps(payload))],
            )
            cursor.execute(
                "SELECT response FROM dp_ai_model_calls WHERE execution_id=%s AND call_id=%s",
                (lease.execution_id, "literal-test"),
            )
            assert json.loads(cursor.fetchone()["response"]) == payload
        for wrong in (
            None,
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            {"configurable": {"thread_id": lease.execution_id, "checkpoint_ns": "child"}},
        ):
            with pytest.raises(ValueError):
                saver.get_tuple(wrong)
            with pytest.raises(ValueError):
                list(saver.list(wrong))
        with pytest.raises(NotImplementedError):
            saver.delete_thread(lease.execution_id)
        with pytest.raises(NotImplementedError):
            saver.setup()


def test_unknown_model_response_blocks_replay_and_new_call_ids(execution):
    _, repo, lease = execution
    assert repo.reserve_call(lease, "model-0", "a" * 64) is None
    expire(repo, lease)
    resumed = repo.claim(lease.execution_id, "restarted")
    for call_id in ("model-0", "model-1"):
        with pytest.raises(OutcomeUnknownError):
            repo.reserve_call(resumed, call_id, "a" * 64)
    repo.finish(resumed, {"error": "outcome_unknown"}, failed=True)


def test_durable_response_replays_without_spending_new_budget(execution):
    _, repo, lease = execution
    assert repo.reserve_call(lease, "model-0", "a" * 64) is None
    repo.record_response(lease, "model-0", {"answer": "中文", "usage": {"completion_tokens": 2}})
    with pytest.raises(ValueError):
        repo.record_response(lease, "model-0", {"answer": "overwrite"})
    repo.release(lease)
    resumed = repo.claim(lease.execution_id, "restarted")
    assert repo.reserve_call(resumed, "model-0", "a" * 64)["answer"] == "中文"
    with pytest.raises(ValueError):
        repo.reserve_call(resumed, "model-0", "b" * 64)
    for index in range(1, 8):
        repo.reserve_call(resumed, f"model-{index}", "a" * 64)
        repo.record_response(resumed, f"model-{index}", {"answer": "ok"})
    with pytest.raises(BudgetExceededError):
        repo.reserve_call(resumed, "model-8", "a" * 64)


def test_deadline_and_graph_version_survive_reclaim(execution):
    _, repo, lease = execution
    with repo.transaction() as cursor:
        cursor.execute(
            "UPDATE dp_ai_executions SET deadline=1,lease_until=0 WHERE execution_id=%s",
            (lease.execution_id,),
        )
    with pytest.raises(ValueError, match="version"):
        repo.claim(lease.execution_id, "bad", graph_version="s3-v2")
    resumed = repo.claim(lease.execution_id, "compatible")
    with pytest.raises(BudgetExceededError):
        repo.reserve_call(resumed, "model-0", "a" * 64)


def test_interrupt_resumes_in_another_process(database):
    name, _, repo = database
    execution_id = str(uuid.uuid4())
    repo.enqueue(execution_id)
    results = []
    for operation in ("start", "resume"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "experiments.assistant_langgraph.acceptance_worker",
                name,
                execution_id,
                operation,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        results.append(json.loads(result.stdout))
    assert results[0] == {"context": "合成数据 S3", "paused": True}
    assert results[1] == {"context": "合成数据 S3", "answer": "receipt-only", "paused": False}


def test_saved_parallel_node_is_not_repeated_after_failure(execution):
    connect, repo, lease = execution

    class State(TypedDict, total=False):
        good: str
        flaky: str

    calls = {"good": 0, "flaky": 0}

    def good(state):
        calls["good"] += 1
        return {"good": "persisted"}

    def flaky(state):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("Injected failure")
        return {"flaky": "recovered"}

    builder = StateGraph(State)
    builder.add_node("a", good)
    builder.add_node("b", flaky)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", END)
    builder.add_edge("b", END)
    with connect() as connection:
        app = builder.compile(checkpointer=FencedMySQLSaver(connection, lease))
        with pytest.raises(RuntimeError, match="Injected"):
            app.invoke({}, config(lease), durability="sync")
    repo.release(lease)
    resumed = repo.claim(lease.execution_id, "restarted")
    with connect() as connection:
        app = builder.compile(checkpointer=FencedMySQLSaver(connection, resumed))
        assert app.invoke(None, config(resumed), durability="sync") == {
            "good": "persisted",
            "flaky": "recovered",
        }
    assert calls == {"good": 1, "flaky": 2}
