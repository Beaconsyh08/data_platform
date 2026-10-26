"""Progress is attempt-scoped state, independent from sampled human logs."""

from types import SimpleNamespace

from lerobot.data_platform.execution import ExecutionSupervisor, SpoolingClient
from lerobot.data_platform.job_progress import progress_snapshot, visible_progress_log
from lerobot.data_platform.local_execution import local_job_payload
from tests.datasets.test_job_management import command, setup  # noqa: F401


def test_progress_survives_text_stage_changes_out_of_order_and_invalid_numbers():
    state = progress_snapshot({}, {"current": 8, "total": 10, "step": "compute"})
    assert state["progress"] == 80
    state = progress_snapshot(state, {}, message="Writing metadata")
    assert state["progress"] == 80
    state = progress_snapshot(state, {"phase": "upload", "current": 1, "total": 20})
    assert state["progress"] == 80
    assert state["phase"] == "upload" and "step" not in state
    for payload in ({"progress": float("nan")}, {"progress": float("inf")}, {"current": -1}):
        state = progress_snapshot(state, payload)
        assert state["progress"] == 80
    assert progress_snapshot(state, {"current": 20, "total": 20})["progress"] == 99
    assert "progress" not in progress_snapshot({}, {}, message="Loading")


def test_logs_keep_stage_milestones_and_all_warnings():
    state = {}
    kept = []
    for index in range(1001):
        payload = {"phase": "upload", "current": index, "total": 1000}
        message = f"Uploaded artifacts {index}/1000"
        if visible_progress_log(state, payload, message):
            kept.append(index)
        state = progress_snapshot(state, payload, message=message)
    assert kept == list(range(0, 1001, 100))
    assert not visible_progress_log(state, {}, state["message"])
    assert visible_progress_log(state, {}, state["message"], "warning")
    assert visible_progress_log(state, {"phase": "finalize"}, "Publishing")


def test_durable_snapshot_survives_logs_completion_and_retry(setup):  # noqa: F811
    store, _, owner, node, _, create = setup
    job = create()
    claimed = store.claim_job(node["node_id"], worker_instance_id="worker")
    execution = claimed["execution"]
    auth = {
        "node_id": node["node_id"],
        "attempt_id": execution["attempt_id"],
        "credential": execution["credential"],
    }
    for message, payload in [
        ("Compute", {"current": 8, "total": 10}),
        ("Saving", {}),
        ("Upload", {"current": 1, "total": 20, "phase": "upload"}),
    ]:
        store.add_job_event(job["job_id"], message=message, payload=payload, **auth)
    persistent = store.job_manager.decorate(store.get_job(job["job_id"]), owner)
    assert persistent["progress"]["progress"] == 80
    assert local_job_payload(store, persistent)["progress"] == 80
    assert local_job_payload(store, persistent)["message"] == "Upload"
    store.complete_job(job["job_id"], status="error", error="upload failed", **auth)
    assert store.get_job(job["job_id"])["result"]["progress"]["progress"] == 80
    command(store, job, owner, "retry")
    assert store.job_manager.decorate(store.get_job(job["job_id"]), owner)["progress"] == {}
    next_job = store.claim_job(node["node_id"], worker_instance_id="worker")
    auth.update(
        attempt_id=next_job["execution"]["attempt_id"], credential=next_job["execution"]["credential"]
    )
    store.add_job_event(job["job_id"], message="Restarted", payload={"current": 1, "total": 10}, **auth)
    assert store.get_job(job["job_id"])["result"]["progress"]["progress"] == 10
    store.job_manager.checkpoint(job["job_id"], phase="finalizing", **auth)
    store.complete_job(job["job_id"], status="done", result={"answer": 42}, **auth)
    result = store.get_job(job["job_id"])["result"]
    assert result["answer"] == 42 and result["progress"]["progress"] == 100


def test_final_event_drain_delivers_more_than_one_batch(tmp_path):
    client = SpoolingClient("http://unused", tmp_path)
    for index in range(205):
        client.event(None, "job", str(index), {"current": index, "total": 205})
    events = []
    supervisor = object.__new__(ExecutionSupervisor)
    supervisor.agent = SimpleNamespace(
        state=None, lease_seconds=60, client=SimpleNamespace(event=lambda *args: events.append(args[2]))
    )
    supervisor.flush_events({"job_id": "job"}, tmp_path)
    assert len(events) == 100
    supervisor.flush_events({"job_id": "job"}, tmp_path, drain=True)
    assert events == [str(index) for index in range(205)]
    assert not list((tmp_path / "events").glob("*.json"))


def test_viewer_stage_changes_bypass_throttle_and_upload_counts_success(tmp_path, monkeypatch):
    from lerobot.data_platform import agent as agent_module
    from tests.datasets.test_data_platform_agent import _agent

    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "cache"
    agent = _agent(tmp_path, source, tmp_path)
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: 100.0)

    def precompute(**kwargs):
        for step, percent in (("check_cache", 0), ("prepare", 50), ("done", 100)):
            kwargs["progress_callback"]({"step": step, "progress": percent, "status": "running"})
        static = output / "static"
        static.mkdir(parents=True)
        (static / "viewer_manifest.json").write_text("{}")
        return {}

    monkeypatch.setattr(agent_module, "run_precompute", precompute)
    observed_uploads = []
    original_event = agent.client.event

    def event(*args):
        if args[-1].get("phase") == "upload":
            observed_uploads.append(len(agent.client.uploads))
        original_event(*args)

    agent.client.event = event
    agent._prepare_viewer(
        {"job_id": "job", "options": {}}, source, {"dataset_key": "source", "output_dir": str(output)}
    )
    assert [entry[2]["progress"] for entry in agent.client.events] == [0, 47.5, 95, 99]
    assert observed_uploads == [1]


def test_failed_upload_does_not_report_success(tmp_path):
    import pytest

    from tests.datasets.test_data_platform_agent import _agent

    agent = _agent(tmp_path, tmp_path, tmp_path)
    (tmp_path / "viewer_manifest.json").write_text("{}")

    def fail(*args, **kwargs):
        raise OSError("connection lost")

    agent.client.upload_artifact = fail
    with pytest.raises(OSError, match="connection lost"):
        agent._upload_viewer_artifacts({"job_id": "job"}, tmp_path)
    assert not agent.client.events


def test_local_progress_uses_common_snapshot_and_keeps_warnings():
    from threading import Lock

    from lerobot.data_platform.routes.context import RouteContext

    logs = []
    ctx = SimpleNamespace(
        jobs_lock=Lock(),
        append_job_log=lambda job, message: logs.append(message),
        job_timing_snapshot=lambda *args: (0, None),
    )
    job = {"status": "running"}
    for payload in (
        {"current": 8, "total": 10, "message": "Compute"},
        {"message": "Writing metadata"},
        {"phase": "upload", "current": 1, "total": 10, "message": "Uploading"},
    ):
        RouteContext.update_job(ctx, job, payload)
    assert job["progress"] == 80
    for _ in range(2):
        RouteContext.update_job(ctx, job, {"message": "Partial input", "level": "warning"})
    assert logs[-2:] == ["Partial input", "Partial input"]
    job.update(status="done", progress=100)
    RouteContext.update_job(ctx, job, {"status": "running", "current": 0, "total": 10})
    assert job["status"] == "done" and job["progress"] == 100


def test_draining_renews_lease_and_preserves_stop_request(tmp_path, monkeypatch):
    import itertools
    import json

    from lerobot.data_platform import execution

    client = SpoolingClient("http://unused", tmp_path)
    client.event(None, "job", "Computed")
    clock = itertools.count(step=21)
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(clock))
    heartbeats = []

    def heartbeat(*args, **kwargs):
        heartbeats.append(kwargs)
        return {"stop_mode": "cooperative"}

    supervisor = object.__new__(ExecutionSupervisor)
    supervisor.agent = SimpleNamespace(
        state=None,
        lease_seconds=60,
        client=SimpleNamespace(event=lambda *args: None, control_heartbeat=heartbeat),
    )
    supervisor.flush_events({"job_id": "job"}, tmp_path, drain=True)
    assert heartbeats == [{"lease_seconds": 60}]
    assert json.loads((tmp_path / "stop.json").read_text()) == {"mode": "cooperative"}
