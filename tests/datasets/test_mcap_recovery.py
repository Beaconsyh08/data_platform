"""MCAP process-loss recovery through durable attempts and the Agent completion API."""

import json
from datetime import timedelta

import pytest

from lerobot.data_platform.control_plane import ControlPlaneStore, RemoteJob, _utcnow
from lerobot.data_platform.execution import ExecutionSupervisor, atomic_json
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.management_storage import JobControl
from tests.datasets.test_execution_supervisor import Client, fake_agent
from tests.datasets.test_job_management import command
from tests.datasets.test_mcap_import import _setup

LOST_COMPUTE_ERROR = "Compute process exited without a result"


def conversion(tmp_path):
    store, client, owner, node, location, headers = _setup(tmp_path)
    job = store.create_job(
        location_id=location["location_id"],
        requested_by=owner["user_id"],
        operation="import.mcap.convert",
        options={"out_root": "/datasets/converted/00001", "config": {"max_files": 0}},
    )
    claimed = store.claim_job(node["node_id"], worker_instance_id="before-restart")
    return store, client, owner, node, headers, job, claimed


def complete(store, node, claimed, **payload):
    execution = claimed["execution"]
    return store.complete_job(
        claimed["job_id"],
        node_id=node["node_id"],
        attempt_id=execution["attempt_id"],
        credential=execution["credential"],
        **payload,
    )


def test_process_loss_requeues_same_job_and_keeps_fingerprint_after_server_restart(tmp_path):
    store, _, _, node, _, job, claimed = conversion(tmp_path)
    execution = claimed["execution"]
    store.job_manager.checkpoint(
        job["job_id"],
        node["node_id"],
        execution["attempt_id"],
        execution["credential"],
        phase="executing",
        fingerprint="source-fingerprint",
    )
    with store.sessions.begin() as session:
        session.get(RemoteJob, job["job_id"]).lease_until = _utcnow() - timedelta(seconds=1)
        queue_time = session.get(JobControl, job["job_id"]).queued_at
    store.job_manager.reap_expired()
    assert store.get_job(job["job_id"])["status"] == "interrupted"
    recovered = complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)
    assert recovered["status"] == "queued"
    assert recovered["job_id"] == job["job_id"]
    assert recovered["result"]["auto_recovery"]["number"] == 1
    with store.sessions() as session:
        control = session.get(JobControl, job["job_id"])
        assert control.input_fingerprint == "source-fingerprint"
        assert control.final_output == "/datasets/converted/00001"
        assert control.queued_at == queue_time
        assert control.stop_confirmed
    restarted = ControlPlaneStore(str(store.engine.url))
    next_attempt = restarted.claim_job(node["node_id"], worker_instance_id="after-restart")
    assert next_attempt["job_id"] == job["job_id"]
    assert next_attempt["execution"]["attempt_id"] != execution["attempt_id"]
    with pytest.raises(JobConflictError, match="Input changed"):
        restarted.job_manager.checkpoint(
            job["job_id"],
            node["node_id"],
            next_attempt["execution"]["attempt_id"],
            next_attempt["execution"]["credential"],
            phase="executing",
            fingerprint="changed",
        )
    attempts = restarted.job_manager.attempts(job["job_id"])
    assert [attempt["status"] for attempt in attempts] == ["error", "running"]


@pytest.mark.parametrize("reason", ["cancel", "force", "ordinary_error", "unstarted", "finalizing", "legacy"])
def test_recovery_excludes_stops_normal_errors_published_work_and_legacy_attempts(tmp_path, reason):
    store, _, owner, node, _, job, claimed = conversion(tmp_path)
    if reason == "cancel":
        command(store, job, owner, "cancel")
    elif reason == "force":
        command(store, job, owner, "terminate", confirm=True, reason="test")
    elif reason in {"finalizing", "legacy"}:
        from lerobot.data_platform.management_storage import JobAttempt

        with store.sessions.begin() as session:
            control = session.get(JobControl, job["job_id"])
            if reason == "finalizing":
                control.phase = "finalizing"
            else:
                session.get(JobAttempt, control.attempt_id).protocol = 1
    error = {
        "ordinary_error": "Invalid source timestamp",
        "unstarted": "Agent restarted before compute launch",
    }.get(reason, LOST_COMPUTE_ERROR)
    result = complete(store, node, claimed, status="error", error=error)
    assert result["status"] == "error"
    assert "auto_recovery" not in result["result"]
    assert store.claim_job(node["node_id"]) is None


def test_repeated_process_loss_stops_after_three_automatic_recoveries(tmp_path):
    store, _, _, node, _, job, claimed = conversion(tmp_path)
    for number in range(1, 5):
        result = complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)
        assert result["status"] == ("queued" if number <= 3 else "error")
        if number <= 3:
            assert result["result"]["auto_recovery"]["number"] == number
            claimed = store.claim_job(node["node_id"], worker_instance_id=f"restart-{number}")
    assert store.get_job(job["job_id"])["status"] == "error"
    assert store.claim_job(node["node_id"]) is None


def test_recovered_job_still_obeys_maintenance_and_revoked_submitter_access(tmp_path):
    from lerobot.data_platform.control_plane import ControlPlaneUser
    from lerobot.data_platform.maintenance import set_maintenance

    store, _, owner, node, _, job, claimed = conversion(tmp_path)
    set_maintenance(store, True)
    assert complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)["status"] == "queued"
    assert store.claim_job(node["node_id"], worker_instance_id="after-restart") is None
    with store.sessions.begin() as session:
        session.get(ControlPlaneUser, owner["user_id"]).active = False
    set_maintenance(store, False)
    assert store.claim_job(node["node_id"], worker_instance_id="after-restart") is None
    assert store.get_job(job["job_id"])["status"] == "queued"


def test_delayed_recovery_acknowledgement_does_not_undo_a_queued_cancellation(tmp_path):
    store, _, owner, node, _, job, claimed = conversion(tmp_path)
    assert complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)["status"] == "queued"
    command(store, job, owner, "cancel")
    assert complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)["status"] == "cancelled"
    assert store.claim_job(node["node_id"]) is None


def test_lost_completion_response_can_be_acknowledged_after_new_attempt_is_claimed(tmp_path):
    store, web, _, node, headers, job, claimed = conversion(tmp_path)
    execution = claimed["execution"]
    headers = headers | {
        "X-Job-Attempt": execution["attempt_id"],
        "X-Job-Credential": execution["credential"],
    }

    class ApiClient(Client):
        lost_response = True

        def complete(self, state, job_id, **payload):
            response = web.post(f"/api/agents/jobs/{job_id}/complete", headers=headers, json=payload)
            assert response.status_code == 200, response.json
            if self.lost_response:
                self.lost_response = False
                raise RuntimeError("completion response lost")
            self.completions.append(payload)
            return response.json["job"]

    client = ApiClient()
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    work = supervisor.root / execution["attempt_id"]
    staging = tmp_path / "partial-output"
    staging.mkdir()
    (staging / "partial").write_bytes(b"partial")
    checkpoints = tmp_path / ".mcap-checkpoints" / job["job_id"]
    checkpoints.mkdir(parents=True)
    (checkpoints / "verified").write_bytes(b"completed file checkpoint")
    marker = {
        "job": claimed,
        "work": str(work),
        "staging": str(staging),
        "final": str(tmp_path / "output"),
        "pid": 99999999,
        "process_identity": "lost",
        "publishing": False,
        "acknowledged": False,
        "reservation": str(work / "reservation.json"),
    }
    atomic_json(work / "reservation.json", {"job_id": job["job_id"]})
    atomic_json(work / "marker.json", marker)
    with pytest.raises(RuntimeError, match="completion response lost"):
        supervisor.reconcile()
    assert store.get_job(job["job_id"])["status"] == "queued"
    replay = web.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={"status": "error", "error": LOST_COMPUTE_ERROR},
    )
    assert replay.status_code == 200
    assert replay.json["job"]["status"] == "queued"
    next_attempt = store.claim_job(node["node_id"], worker_instance_id="after-restart")
    assert next_attempt["execution"]["attempt_id"] != execution["attempt_id"]
    assert supervisor.reconcile()
    assert json.loads((work / "marker.json").read_text())["acknowledged"]
    assert not (work / "reservation.json").exists()
    assert not staging.exists()
    assert (checkpoints / "verified").read_bytes() == b"completed file checkpoint"
    assert store.get_job(job["job_id"])["status"] == "running"
    for endpoint, payload in [("heartbeat", {}), ("checkpoint", {"phase": "executing"})]:
        response = web.post(f"/api/agents/jobs/{job['job_id']}/{endpoint}", headers=headers, json=payload)
        assert response.status_code == 409
    conflict = web.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        headers=headers,
        json={"status": "done"},
    )
    assert conflict.status_code == 409
    invalid_headers = headers | {"X-Job-Credential": "invalid"}
    assert (
        web.post(
            f"/api/agents/jobs/{job['job_id']}/complete",
            headers=invalid_headers,
            json={"status": "error"},
        ).status_code
        == 409
    )
    assert complete(store, node, claimed, status="error", error=LOST_COMPUTE_ERROR)["status"] == "running"


def test_reboot_never_monitors_reused_pids_from_previous_boot(tmp_path, monkeypatch):
    from lerobot.data_platform import execution

    client = Client()
    supervisor = ExecutionSupervisor(fake_agent(tmp_path, client))
    work = supervisor.root / "old-attempt"
    work.mkdir()
    staging = tmp_path / "partial"
    staging.mkdir()
    marker = {
        "job": {"job_id": "old-job", "operation": "import.mcap.convert"},
        "work": str(work),
        "staging": str(staging),
        "final": None,
        "pid": 123,
        "process_identity": "reused",
        "boot_id": "previous-boot",
        "publishing": False,
    }
    monkeypatch.setattr(execution, "boot_identity", lambda: "current-boot")
    monkeypatch.setattr(execution, "process_identity", lambda _: pytest.fail("Old boot PID must be ignored"))
    monkeypatch.setattr(execution, "group_members", lambda _: pytest.fail("Old boot group must be ignored"))
    supervisor.monitor(marker, None)
    assert client.completions == [{"status": "error", "result": None, "error": LOST_COMPUTE_ERROR}]
    assert not staging.exists()
