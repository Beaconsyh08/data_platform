"""Background derivation cannot bypass source authority, rule edits or interactive budgets."""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest
from sqlalchemy import select

from lerobot.data_platform.ai_tools import experience, experience_worker, followups, hindsight
from lerobot.data_platform.ai_tools.actions import confirm
from lerobot.data_platform.ai_tools.models import (
    AssistantExperience,
    AssistantFollowup,
    AssistantMemory,
    AssistantTurn,
)
from lerobot.data_platform.ai_tools.service import AssistantService
from lerobot.data_platform.control_plane import ControlPlaneUser, DatasetLocation
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.processing.models import PipelineRun
from tests.datasets.test_ai_assistant import assistant  # noqa: F401
from tests.datasets.test_ai_assistant_actions import prepared
from tests.datasets.test_ai_assistant_context import text_response
from tests.datasets.test_ai_assistant_runtime import processing_setup  # noqa: F401
from tests.datasets.test_ai_memory_followups import completed, save
from tests.datasets.test_processing import acknowledge


@pytest.fixture
def history(assistant, processing_setup, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DATA_PLATFORM_AI_EXPERIENCE_ENABLED", "1")
    service, fake, owner, body = assistant
    first = completed(assistant, processing_setup, tmp_path)
    second_body = {**body, "request_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4())}
    result = prepared((service, fake, owner, second_body), tmp_path)
    second = confirm(service, owner, result["request_id"], result["revision"], rerun=True)
    service.manager.tick(second["run_id"])
    job = service.manager.run(owner, second["run_id"])["steps"][0]["job_id"]
    acknowledge(processing_setup, job, output=False)
    service.manager.tick(second["run_id"])
    assert first[1]["run_id"] != second["run_id"]
    followups.tick(service)
    with service.store.sessions() as session:
        snapshot = experience.snapshot(service, session, owner, body["location_id"])
    assert len(snapshot.sources) == 2
    return service, fake, owner, body, snapshot


def verified(snapshot):
    return [
        {
            "text": "Two historical Viewer tasks completed with acknowledged results.",
            "historical_only": True,
            "sources": [
                {"source_id": s.source_id, "run_id": s.run_id, "digest": s.digest, "quote": s.text[:100]}
                for s in snapshot.sources
            ],
        }
    ]


class Backend:
    calls = []

    def __init__(self, config, bank_id, **kwargs):
        self.bank_id = bank_id

    def observations(self, snapshot):
        self.calls.append(snapshot)
        return snapshot

    def delete(self):
        pass


def tick(service, **kwargs):
    return experience_worker.tick(
        service,
        config=hindsight.Config("http://127.0.0.1:1", "test"),
        backend_factory=kwargs.pop("backend_factory", Backend),
        verifier=kwargs.pop("verifier", lambda snapshot, **_: verified(snapshot)),
        **kwargs,
    )


def test_real_completion_queue_restart_cache_and_no_network_foreground(history, monkeypatch):
    service, _, owner, body, _ = history
    assert tick(service)
    restarted = AssistantService(service.manager)
    monkeypatch.setattr(hindsight.Backend, "request", lambda *a, **k: pytest.fail("No foreground network"))
    value = experience.context(restarted, owner, body["location_id"])
    assert len(value) == 1 and len(value[0]["sources"]) == 2
    assert not tick(restarted)
    assert len(service.store.list_jobs()) == 2
    assert len(service.manager.runs(owner)) == 2


@pytest.mark.parametrize("mutation", ["retry", "delete", "identity", "revoke", "disable"])
def test_changed_sources_never_reuse_old_cache(history, mutation):
    service, _, owner, body, snapshot = history
    tick(service)
    with service.store.sessions.begin() as session:
        source = snapshot.sources[0]
        if mutation == "retry":
            session.get(PipelineRun, source.run_id).revision += 1
        elif mutation == "delete":
            session.delete(session.get(AssistantFollowup, source.source_id))
        elif mutation == "identity":
            session.get(DatasetLocation, body["location_id"]).details = {"fingerprint": "changed"}
        elif mutation == "revoke":
            session.get(ControlPlaneUser, owner).active = False
        else:
            session.add(
                AssistantMemory(
                    owner=owner,
                    revision=1,
                    settings={"enabled": False, "followups_enabled": True},
                    notes=[],
                    updated_at=int(time.time()),
                )
            )
    if mutation == "revoke":
        with pytest.raises(PermissionError):
            experience.context(service, owner, body["location_id"])
    else:
        assert experience.context(service, owner, body["location_id"]) == []


def test_new_rule_clears_old_derivation_and_late_completion_cannot_restore_it(history):
    service, _, owner, body, snapshot = history
    tick(service)
    save(service, owner, "Use only the newly approved rule; older task settings are obsolete")
    assert experience.context(service, owner, body["location_id"]) == []

    # Even a late completion imported after the edit still belongs to an older Run.
    with service.store.sessions.begin() as session:
        note = session.get(AssistantMemory, owner)
        session.get(AssistantFollowup, snapshot.sources[0].source_id).updated_at = note.updated_at + 5
        session.get(PipelineRun, snapshot.sources[0].run_id).created_at = note.updated_at - 5
    assert tick(service, backend_factory=lambda *a, **k: pytest.fail("Old tasks cannot be re-derived"))
    assert experience.context(service, owner, body["location_id"]) == []


def test_exact_action_time_survives_mysql_float_run_timestamp_loss(history):
    service, _, owner, body, original = history
    with service.store.sessions.begin() as session:
        for source in original.sources:
            session.get(PipelineRun, source.run_id).created_at = 0
    with service.store.sessions() as session:
        current = experience.snapshot(service, session, owner, body["location_id"])
    assert len(current.sources) == 2
    tick(service)
    assert experience.context(service, owner, body["location_id"])


@pytest.mark.parametrize("mutation", ["missing", "older", "reused"])
def test_origin_proof_rejects_old_or_missing_actions_and_later_reuse(history, mutation):
    service, _, owner, body, original = history
    with service.store.sessions.begin() as session:
        for source in original.sources:
            origin = session.get(AssistantTurn, source.source_id)
            session.get(PipelineRun, source.run_id).created_at = time.time() + 86400
            if mutation == "missing":
                session.delete(origin)
            elif mutation == "older":
                origin.created_at = 0
            else:
                # A new action points at an already-existing Run. Its recent
                # timestamp must not renew that Run's original evidence age.
                values = {c.name: getattr(origin, c.name) for c in AssistantTurn.__table__.columns}
                values.update(request_id=str(uuid.uuid4()), created_at=int(time.time()) + 5)
                session.add(AssistantTurn(**values))
                session.get(AssistantFollowup, source.source_id).action_id = values["request_id"]
    with service.store.sessions() as session:
        assert not experience.snapshot(service, session, owner, body["location_id"]).sources


def test_rule_change_during_derivation_fences_publisher(history):
    service, _, owner, body, _ = history

    def supersede(snapshot, **_):
        save(service, owner, "New rule")
        return verified(snapshot)

    assert tick(service, verifier=supersede)
    assert experience.context(service, owner, body["location_id"]) == []
    with service.store.sessions() as session:
        row = session.get(AssistantExperience, experience.scope_id(owner, body["location_id"]))
        assert row.status == "pending" and row.lease_until == 0


def test_busy_background_has_one_owner_and_does_not_block_foreground(history):
    service, _, owner, body, _ = history
    started, release = threading.Event(), threading.Event()

    class Slow(Backend):
        def observations(self, snapshot):
            started.set()
            assert release.wait(5)
            raise TimeoutError("simulated slow provider")

    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(tick, service, backend_factory=Slow)
        assert started.wait(3)
        try:
            assert not tick(service)
            start = time.monotonic()
            assert experience.context(service, owner, body["location_id"]) == []
            assert time.monotonic() - start < 1
        finally:
            release.set()
        assert work.result()
    with service.store.sessions() as session:
        row = session.get(AssistantExperience, experience.scope_id(owner, body["location_id"]))
        assert row.status == "error" and row.next_check_at >= time.time() + 50
        assert row.observations == [] and row.lease_until == 0


def test_caches_are_account_and_environment_private(history, monkeypatch):
    service, _, owner, body, _ = history
    tick(service)
    other = service.store.register_user(username="other", password="strong-other-password")
    assert experience.context(service, other["user_id"], body["location_id"]) == []
    before = experience.scope_id(owner, body["location_id"])
    monkeypatch.setenv("DATA_PLATFORM_ENV", "dev")
    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", str(uuid.uuid4()))
    assert experience.scope_id(owner, body["location_id"]) != before
    assert experience.context(service, owner, body["location_id"]) == []


def test_memory_changes_abort_inflight_generation_even_after_model_response(assistant):  # noqa: F811
    service, fake, owner, body = assistant

    def change_while_answering(payload):
        save(service, owner, "A newer rule")
        return text_response("Old answer")

    fake.post_chat_completion = change_while_answering
    with pytest.raises(JobConflictError):
        service.submit(owner, body)
    assert service.turn(owner, body["request_id"])["status"] == "error"
    assert not service.store.list_jobs()


def graph(snapshot):
    facts = [
        {
            "id": f"f{i}",
            "fact_type": "world",
            "document_id": s.source_id,
            "metadata": {"source_digest": s.digest},
        }
        for i, s in enumerate(snapshot.sources)
    ]
    return facts + [
        {
            "id": "o",
            "fact_type": "observation",
            "text": "Two tasks completed.",
            "source_memory_ids": [f["id"] for f in facts],
        }
    ]


@pytest.mark.parametrize("bad", ["missing", "cycle", "hash", "invalid", "single_run", "unlinked_id"])
def test_forged_or_incomplete_provenance_is_rejected(history, bad):
    snapshot = history[-1]
    units = graph(snapshot)
    assert len(hindsight.candidates(snapshot, units)) == 1
    if bad == "missing":
        units.pop(0)
    elif bad == "cycle":
        units[-1]["source_memory_ids"] = ["o"]
    elif bad == "hash":
        units[0]["metadata"]["source_digest"] = "changed"
    elif bad == "invalid":
        units[0]["state"] = "invalidated"
    elif bad == "single_run":
        units[-1]["source_memory_ids"] = ["f0", "f0"]
    else:
        units[-1]["text"] += " Also supported by " + str(uuid.uuid4())
    assert hindsight.candidates(snapshot, units) == []


@pytest.mark.parametrize("bad", [None, "unsupported", "invented_quote", "missing_source", "foreign_source"])
def test_semantic_check_needs_all_exact_source_quotes(history, bad):
    snapshot = history[-1]
    items = [{"text": "Two historical tasks completed.", "sources": [asdict(s) for s in snapshot.sources]}]
    verdict = {
        "index": 0,
        "supported": bad != "unsupported",
        "evidence": [{"source_id": s.source_id, "quote": s.text[:80]} for s in snapshot.sources],
    }
    if bad == "invented_quote":
        verdict["evidence"][0]["quote"] = "This quote was never present in either source"
    elif bad == "missing_source":
        verdict["evidence"].pop()
    elif bad == "foreign_source":
        verdict["evidence"][0]["source_id"] = str(uuid.uuid4())

    class Judge:
        def post_chat_completion(self, payload):
            return text_response(json.dumps({"results": [verdict]}))

    result = hindsight.verify(
        items, model="test", deadline=time.monotonic() + 5, client_factory=lambda **_: Judge()
    )
    assert bool(result) == (bad is None)


def test_deleted_remote_bank_is_retried_after_cleanup_failure(history):
    service = history[0]

    class Dirty(Backend):
        def delete(self):
            raise TimeoutError("unavailable")

    tick(service, backend_factory=Dirty)
    with service.store.sessions.begin() as session:
        row = session.scalar(select(AssistantExperience))
        old = row.bank_id
        assert old and row.status == "error"
        row.next_check_at = 0
    deleted = []

    class Clean(Backend):
        def delete(self):
            deleted.append(self.bank_id)

    tick(service, backend_factory=Clean)
    assert deleted[0] == old and len(deleted) == 2
    with service.store.sessions() as session:
        assert session.scalar(select(AssistantExperience)).bank_id == ""


def test_generation_guard_checks_source_revision(history):
    service, _, owner, body, snapshot = history
    check, _ = experience.generation_guard(service, owner, body["location_id"], has_experiences=True)
    with service.store.sessions.begin() as session:
        session.get(PipelineRun, snapshot.sources[0].run_id).revision += 1
    with pytest.raises(JobConflictError):
        check()


def test_rule_edit_between_generation_and_publication_rejects_old_answer(assistant):  # noqa: F811
    service, fake, owner, body = assistant
    _, (history, proposal, identity) = service._reserve(owner, body)
    fake.responses = [text_response("Old rule explanation")]
    result = service._generate(owner, body, history, proposal, identity)
    assert "_memory_version" in result
    save(service, owner, "New rule")
    published = service._complete(owner, body, history, result)
    assert published["status"] == "error" and "memory" in published["error"]
    assert "_memory_version" not in published
    assert not service.conversation(owner, body["conversation_id"])["history"]


def test_retention_erases_cached_quotes_even_when_enhancement_is_disabled(history, monkeypatch):
    service, _, owner, body, _ = history
    tick(service)
    monkeypatch.setenv("DATA_PLATFORM_AI_EXPERIENCE_ENABLED", "0")
    followups.tick(service, now=int(time.time()) + 31 * 86400)
    with service.store.sessions() as session:
        row = session.get(AssistantExperience, experience.scope_id(owner, body["location_id"]))
        assert row.observations == []
