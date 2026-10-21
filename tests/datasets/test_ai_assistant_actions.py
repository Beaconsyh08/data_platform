"""Preparation is inert; confirmation remains authorized, version-bound and retry-safe."""

import json
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
        ("preprocess.pose_interpolate", {}),
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


def published_viewer(service, location_id, tmp_path):
    cache = tmp_path / "published"
    (cache / "static").mkdir(parents=True)
    with service.store.sessions.begin() as session:
        location = session.get(DatasetLocation, location_id)
        (cache / "static" / "viewer_manifest.json").write_text(
            json.dumps({"version": 1, "root": location.root, "episodes": [{"episode_index": 0}]})
        )
        location.details = {
            **location.details,
            "viewer_ready": True,
            "viewer_url": "/fixture/dataset/episode_0",
            "cache_root": str(cache),
        }
    return cache


def test_prepared_viewer_offers_authorized_reference_without_submitting(assistant, tmp_path):  # noqa: F811
    service, _, owner, body = assistant
    published_viewer(service, body["location_id"], tmp_path)
    result = prepared(assistant, tmp_path)
    assert result["action"]["viewer_available"] and result["action"]["prefer_existing"]
    assert "已有历史结果" in result["message"]
    restored = service.conversation(owner, body["conversation_id"])
    assert any(ref["id"] == "viewer" for ref in restored["references"])
    assert not service.store.list_jobs() and not service.manager.list(owner)
    with service.store.sessions.begin() as session:
        location = session.get(DatasetLocation, body["location_id"])
        location.details = {**location.details, "viewer_ready": False}
    assert not any(
        ref["id"] == "viewer" for ref in service.conversation(owner, body["conversation_id"])["references"]
    )


@pytest.mark.parametrize("invalid", ["missing", "oversized", "wrong_source", "unsafe_url", "symlink"])
def test_viewer_reference_rejects_unavailable_or_untrusted_cache(assistant, tmp_path, invalid):  # noqa: F811
    from lerobot.data_platform.ai_tools.context import viewer_reference

    service, _, _, body = assistant
    cache = published_viewer(service, body["location_id"], tmp_path)
    manifest = cache / "static" / "viewer_manifest.json"
    if invalid == "missing":
        manifest.unlink()
    elif invalid == "oversized":
        manifest.write_text("x" * (1024 * 1024 + 1))
    elif invalid == "wrong_source":
        data = json.loads(manifest.read_text())
        data["root"] = "/unrelated"
        manifest.write_text(json.dumps(data))
    elif invalid == "symlink":
        other = tmp_path / "outside.json"
        manifest.rename(other)
        manifest.symlink_to(other)
    else:
        with service.store.sessions.begin() as session:
            location = session.get(DatasetLocation, body["location_id"])
            location.details = {**location.details, "viewer_url": "//example.test/fixture/episode_0"}
    assert viewer_reference(service.store.get_location(body["location_id"])) is None


def quality_artifact(fixture, tmp_path, *, status="done", parameters=None):
    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.curation import bundle_manifest
    from lerobot.data_platform.processing.service import location_identity

    service, _, owner, body = fixture
    ledger = service.manager.lifecycle
    target = {"location_id": body["location_id"], "dataset_version_id": "fixture-version"}
    ledger._put_record("curation_locations", body["location_id"], target)
    location = service.store.get_location(body["location_id"])
    job = service.store.create_job(
        location_id=body["location_id"],
        requested_by=owner,
        operation="curation.quality",
        options={
            "parameters": parameters or {"checks": ["s3"]},
            "target": target,
            "operator_snapshot": {"version": "1.0.0", "input_identity": location_identity(location)},
        },
    )
    with service.store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = status
    directory = ledger.root / "curation" / "runs" / job["job_id"]
    (directory / "static").mkdir(parents=True)
    (directory / "static" / "quality_flagged_episodes.json").write_text('{"summary":{"flagged":0}}')
    bundle_manifest(directory, operation="curation.quality", target=target)
    return job, directory


def test_quality_card_new_running_published_and_missing_artifact(assistant, tmp_path):  # noqa: F811
    from lerobot.data_platform.ai_tools.operation_state import action_status
    from lerobot.data_platform.control_plane import RemoteJob

    result = prepared(assistant, tmp_path, "curation.quality")
    service, _, owner, body = assistant
    assert action_status(service, owner, result["request_id"])["state"] == "new"
    job, directory = quality_artifact(assistant, tmp_path, status="running")
    state = action_status(service, owner, result["request_id"])
    assert state["state"] == "running" and not state["can_confirm"]
    receipt = confirm(service, owner, result["request_id"], result["revision"])
    assert receipt["reused"] and not service.manager.runs(owner)
    with service.store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).status = "done"
    state = action_status(service, owner, result["request_id"])
    assert state["state"] == "ready" and state["requires_rerun"]
    assert state["links"][0]["title"] == "Open Quality report"
    assert "coverage" in state["coverage_notice"]
    with pytest.raises(JobConflictError, match="explicitly run again"):
        confirm(service, owner, result["request_id"], result["revision"])
    (directory / "static" / "quality_flagged_episodes.json").unlink()
    assert action_status(service, owner, result["request_id"])["state"] == "history"
    assert len(service.store.list_jobs()) == 1


def test_quality_configuration_version_and_oversized_history_are_not_reused(assistant, tmp_path):  # noqa: F811
    from lerobot.data_platform.ai_tools.operation_state import action_status
    from lerobot.data_platform.control_plane import RemoteJob

    result = prepared(assistant, tmp_path, "curation.quality")
    service, _, owner, body = assistant
    job, _ = quality_artifact(assistant, tmp_path, parameters={"checks": ["s3"], "episodes": [0]})
    assert action_status(service, owner, result["request_id"])["state"] == "history"
    with service.store.sessions.begin() as session:
        row = session.get(RemoteJob, job["job_id"])
        row.options = {**row.options, "parameters": {"checks": ["s3"], "qc_config": {"note": "x" * 1000000}}}
    state = action_status(service, owner, result["request_id"])
    assert state["state"] == "history" and len(json.dumps(state)) < 5000
    service.manager.lifecycle._put_record(
        "curation_locations", body["location_id"], {"dataset_version_id": "other"}
    )
    assert action_status(service, owner, result["request_id"])["state"] == "history"


def test_cross_conversation_concurrent_confirmations_share_one_active_run(assistant, tmp_path):  # noqa: F811
    from lerobot.data_platform.ai_tools.operation_state import action_status

    first = prepared(assistant, tmp_path, "curation.quality")
    service, fake, owner, body = assistant
    fake.responses = [
        call("describe_operator", {"operator_id": "curation.quality"}),
        call("prepare_action", {"operator_id": "curation.quality", "parameters": {"checks": ["s3"]}}),
    ]
    second = service.submit(
        owner, {**body, "conversation_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4())}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda item: confirm(service, owner, item["request_id"], item["revision"]), [first, second]
            )
        )
    assert receipts[0]["run_id"] == receipts[1]["run_id"]
    assert len(service.manager.runs(owner)) == 1
    assert action_status(service, owner, second["request_id"])["state"] == "running"
    assert (
        confirm(service, owner, second["request_id"], second["revision"])["run_id"] == receipts[0]["run_id"]
    )


@pytest.mark.parametrize(
    "operator,parameters",
    [
        ("preprocess.split", {"episode_range": "0:1"}),
        ("curation.stage", {"episodes": [0]}),
        ("curation.labeling", {"backend": "qwen_dashscope", "trial": True}),
        ("curation.tagging", {"vlm_backend": "qwen_dashscope", "selected_tags": ["pick"]}),
    ],
)
def test_additional_operations_prepare_without_running(assistant, tmp_path, operator, parameters):  # noqa: F811
    from lerobot.data_platform.ai_tools.actions import prepare

    service, _, owner, body = assistant
    bind_lifecycle(service, tmp_path)
    action = prepare(service, owner, body["location_id"], operator, parameters)
    assert action["operator_id"] == operator
    assert action["parameters"].items() >= parameters.items()
    assert not service.store.list_jobs() and not service.manager.list(owner)


def test_status_rechecks_role_and_result_links_never_cross_accounts(assistant, tmp_path):  # noqa: F811
    from lerobot.data_platform.ai_tools.operation_state import action_status

    result = prepared(assistant, tmp_path, "curation.quality")
    service, _, owner, _ = assistant
    quality_artifact(assistant, tmp_path)
    other = service.store.register_user(username="status-other", password="strong-other-password")
    with pytest.raises(KeyError):
        action_status(service, other["user_id"], result["request_id"])
    with service.store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner).role = "viewer"
    with pytest.raises(PermissionError):
        action_status(service, owner, result["request_id"])
