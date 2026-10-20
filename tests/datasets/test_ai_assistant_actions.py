"""Preparation is inert; confirmation remains authorized, version-bound and retry-safe."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from lerobot.data_platform.ai_tools.actions import confirm
from lerobot.data_platform.control_plane import ControlPlaneUser, DatasetLocation
from lerobot.data_platform.job_management import JobConflictError
from tests.datasets.test_ai_assistant import assistant, call, processing_setup  # noqa: F401
from tests.datasets.test_ai_assistant_context import bind_lifecycle, text_response


def prepared(fixture, tmp_path, operator="viewer.prepare"):
    service, fake, owner, body = fixture
    bind_lifecycle(service, tmp_path)
    fake.responses = [
        call("describe_operator", {"operator_id": operator}),
        call(
            "prepare_action",
            {
                "operator_id": operator,
                "parameters": {"checks": ["s3"]} if operator == "curation.quality" else {},
            },
        ),
    ]
    result = service.submit(owner, body)
    assert result["action"]["can_execute"], result
    return result


@pytest.mark.parametrize("operator", ["viewer.prepare", "curation.quality"])
def test_prepare_does_not_save_or_run_and_confirm_is_idempotent(assistant, tmp_path, operator):  # noqa: F811
    result = prepared(assistant, tmp_path, operator)
    service, _, owner, body = assistant
    assert not service.store.list_jobs() and not service.manager.list(owner)
    action = service.conversation(owner, body["conversation_id"])["action"]
    assert action["action_id"] == result["request_id"] and action["is_current"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _: confirm(service, owner, action["action_id"], action["revision"]), range(2))
        )
    assert receipts[0]["run_id"] == receipts[1]["run_id"]
    assert len(service.manager.list(owner)) == len(service.manager.runs(owner)) == 1
    service.manager.tick(receipts[0]["run_id"])
    assert len(service.store.list_jobs()) == 1
    assert (
        service.conversation(owner, body["conversation_id"])["action"]["execution"]["run_id"]
        == receipts[0]["run_id"]
    )


def test_confirmation_recovers_after_run_commit_but_lost_response(assistant, tmp_path, monkeypatch):  # noqa: F811
    result = prepared(assistant, tmp_path)
    service, _, owner, _ = assistant
    start = service.manager.start

    def lose_response(*args, **kwargs):
        start(*args, **kwargs)
        raise RuntimeError("response lost")

    monkeypatch.setattr(service.manager, "start", lose_response)
    with pytest.raises(RuntimeError):
        confirm(service, owner, result["request_id"], result["revision"])
    monkeypatch.setattr(service.manager, "start", start)
    receipt = confirm(service, owner, result["request_id"], result["revision"])
    assert len(service.manager.runs(owner)) == 1
    assert receipt["run_id"] == service.manager.runs(owner)[0]["run_id"]


def test_changed_conversation_or_dataset_cannot_confirm_stale_action(assistant, tmp_path):  # noqa: F811
    result = prepared(assistant, tmp_path)
    service, fake, owner, body = assistant
    fake.responses = [text_response("新的说明")]
    service.submit(owner, {**body, "request_id": str(uuid.uuid4()), "expected_revision": 1})
    assert not service.conversation(owner, body["conversation_id"])["action"]["is_current"]
    with pytest.raises(JobConflictError):
        confirm(service, owner, result["request_id"], result["revision"])
    with service.store.sessions.begin() as session:
        session.get(DatasetLocation, body["location_id"]).details = {"fingerprint": "changed"}
    with pytest.raises(JobConflictError):
        confirm(service, owner, result["request_id"], result["revision"])
    assert not service.manager.list(owner) and not service.store.list_jobs()


def test_revoked_role_and_other_account_cannot_confirm(assistant, tmp_path):  # noqa: F811
    result = prepared(assistant, tmp_path)
    service, _, owner, body = assistant
    other = service.store.register_user(username="other", password="strong-other-password")
    with pytest.raises(KeyError):
        confirm(service, other["user_id"], result["request_id"], result["revision"])
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).role = "viewer"
    assert service.conversation(owner, body["conversation_id"])["action"] is None
    with pytest.raises(PermissionError):
        confirm(service, owner, result["request_id"], result["revision"])
    assert not service.store.list_jobs()


def test_source_change_during_confirmation_does_not_start_run(assistant, tmp_path, monkeypatch):  # noqa: F811
    result = prepared(assistant, tmp_path)
    service, _, owner, body = assistant
    save = service.manager.save

    def change(*args, **kwargs):
        saved = save(*args, **kwargs)
        with service.store.sessions.begin() as session:
            session.get(DatasetLocation, body["location_id"]).details = {"fingerprint": "changed"}
        return saved

    monkeypatch.setattr(service.manager, "save", change)
    with pytest.raises(JobConflictError):
        confirm(service, owner, result["request_id"], result["revision"])
    assert not service.manager.runs(owner) and not service.store.list_jobs()


def test_no_default_hourly_limit_and_model_cannot_confirm(assistant):  # noqa: F811
    service, fake, owner, body = assistant
    assert service.capabilities(owner)["hourly_limit"] is None
    for revision in range(22):
        fake.responses = [text_response("说明")]
        service.submit(owner, {**body, "request_id": str(uuid.uuid4()), "expected_revision": revision})
    fake.responses = [call("confirm_action", {"revision": 22}), text_response("需要确认")]
    service.submit(owner, {**body, "request_id": str(uuid.uuid4()), "expected_revision": 22})
    assert "Unknown tool" in fake.requests[-1]["messages"][-1]["content"]
    assert not service.store.list_jobs() and not service.manager.list(owner)


@pytest.mark.parametrize(
    "operator,parameters",
    [
        ("curation.quality", {"clear_manual_flags": True}),
        ("viewer.prepare", {"task_config": {"new": "task"}}),
        ("preprocess.split", {}),
    ],
)
def test_destructive_or_advanced_operations_stay_outside_action_allowlist(
    assistant,  # noqa: F811
    tmp_path,
    operator,
    parameters,
):
    from lerobot.data_platform.ai_tools.actions import prepare

    service, _, owner, body = assistant
    bind_lifecycle(service, tmp_path)
    with pytest.raises(ValueError):
        prepare(service, owner, body["location_id"], operator, parameters)
    assert not service.manager.list(owner) and not service.store.list_jobs()
