"""Local polling must retain one Agent instead of leaking background readers and transports."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from lerobot.data_platform import agent as agent_module
from lerobot.data_platform import local_execution


class StopPolling(BaseException):
    pass


@pytest.fixture
def worker(tmp_path, monkeypatch):
    state = tmp_path / "local-agent.json"
    state.write_text(json.dumps({"server_url": "http://server.test", "name": "local-test"}))
    config = tmp_path / "local-executor.json"
    config.write_text(
        json.dumps(
            {
                "state_file": str(state),
                "allowed_roots": [str(tmp_path)],
                "writable_roots": [str(tmp_path)],
                "allow_source_mutations": False,
            }
        )
    )
    clients, agents = [], []

    class Client:
        def __init__(self, server_url):
            self.server_url = server_url
            self.closed = False
            self.session = SimpleNamespace(close=self.close)
            clients.append(self)

        def close(self):
            self.closed = True

    class Agent:
        def __init__(self, **kwargs):
            self.client = kwargs["client"]
            self.allowed_roots = kwargs["allowed_roots"]
            self.local_requests = kwargs.get("local_requests", False)
            self.polls = 0
            self.readers = 0
            self.closed = False
            agents.append(self)

        def run_once(self, *, sync):
            assert not sync
            self.polls += 1
            if not self.readers:
                self.readers = 2

        def close(self):
            self.readers = 0
            self.closed = True
            self.client.session.close()

    monkeypatch.setattr(agent_module, "AgentClient", Client)
    monkeypatch.setattr(agent_module, "DataPlatformAgent", Agent)
    monkeypatch.setattr("lerobot.data_platform.environment.check_server_environment", lambda: None)
    monkeypatch.setattr(sys, "argv", ["local-execution", "--config", str(config)])
    return SimpleNamespace(config=config, state=state, clients=clients, agents=agents)


def run_polls(worker, monkeypatch, *, between_polls=None, count=3):
    polls = 0

    def sleep(_seconds):
        nonlocal polls
        polls += 1
        if polls == count:
            raise StopPolling
        if between_polls:
            between_polls(polls)

    monkeypatch.setattr(local_execution.time, "sleep", sleep)
    with pytest.raises(StopPolling):
        local_execution._run(SimpleNamespace(draining=False))


def test_idle_polls_do_not_accumulate_agents_or_caption_readers(worker, monkeypatch):
    snapshots = []
    run_polls(
        worker,
        monkeypatch,
        between_polls=lambda _: snapshots.append(sum(agent.readers for agent in worker.agents)),
    )
    assert snapshots == [2, 2]
    assert len(worker.clients) == len(worker.agents) == 1
    assert worker.agents[0].local_requests is True
    assert worker.agents[0].polls == 3
    assert worker.agents[0].closed and worker.clients[0].closed


def test_config_refresh_closes_previous_readers_and_uses_new_roots(worker, monkeypatch, tmp_path):
    def change_config(poll):
        if poll == 1:
            config = json.loads(worker.config.read_text())
            config["allowed_roots"].append(str(tmp_path / "new-dataset"))
            worker.config.write_text(json.dumps(config))
        else:
            assert worker.agents[0].closed
            assert sum(agent.readers for agent in worker.agents) == 2

    run_polls(worker, monkeypatch, between_polls=change_config)
    assert len(worker.agents) == 2
    assert worker.agents[1].allowed_roots[-1] == tmp_path / "new-dataset"
    assert all(client.closed for client in worker.clients)


def test_poll_error_retries_with_existing_agent(worker, monkeypatch):
    original = agent_module.DataPlatformAgent.run_once

    def run_once(self, **kwargs):
        original(self, **kwargs)
        if self.polls == 1:
            raise OSError("temporary transport failure")

    monkeypatch.setattr(agent_module.DataPlatformAgent, "run_once", run_once)
    run_polls(worker, monkeypatch)
    assert len(worker.agents) == 1
    assert worker.agents[0].polls == 3


def test_failed_initialization_closes_transport_before_retry(worker, monkeypatch):
    def fail(**kwargs):
        raise ValueError("invalid environment identity")

    monkeypatch.setattr(agent_module, "DataPlatformAgent", fail)
    run_polls(worker, monkeypatch, count=2)
    assert len(worker.clients) == 2
    assert all(client.closed for client in worker.clients)


def test_removed_configuration_stops_previous_readers(worker, monkeypatch):
    def remove_config(poll):
        if poll == 1:
            worker.config.unlink()
        else:
            assert worker.agents[0].closed

    run_polls(worker, monkeypatch, between_polls=remove_config)
    assert len(worker.agents) == 1
    assert worker.agents[0].polls == 1


def test_node_identity_refresh_replaces_old_agent(worker, monkeypatch):
    def change_state(poll):
        if poll == 1:
            state = json.loads(worker.state.read_text())
            state["server_url"] = "http://new-server.test"
            worker.state.write_text(json.dumps(state))

    run_polls(worker, monkeypatch, between_polls=change_state)
    assert len(worker.agents) == 2
    assert worker.clients[0].closed
    assert worker.clients[1].server_url == "http://new-server.test"


@pytest.mark.parametrize("transport_error", [False, True])
def test_caption_read_threads_stop_and_close_their_sessions(monkeypatch, transport_error):
    agent = object.__new__(agent_module.DataPlatformAgent)
    started = threading.Barrier(3)
    clients = []

    class Client:
        server_url = "http://server.test"
        verify = True

        def __init__(self, *_args, **_kwargs):
            self.closed = False
            self.session = SimpleNamespace(close=self.close)
            clients.append(self)

        def close(self):
            self.closed = True

        def _request(self, *_args, **_kwargs):
            started.wait(timeout=5)
            if transport_error:
                raise OSError("temporary read failure")
            agent._caption_read_stop.wait(5)
            return {"read": None}

    agent.client = Client()
    agent.state = SimpleNamespace(node_token="test-token")
    monkeypatch.setattr(agent_module, "AgentClient", Client)
    agent._start_caption_reads()
    try:
        started.wait(timeout=5)
        agent._start_caption_reads()
        assert len(clients) == 3
    finally:
        agent.close()
    assert all(not thread.is_alive() for thread in agent._caption_read_threads)
    assert all(client.closed for client in clients)


def test_confirmed_local_output_is_readable_by_the_next_pipeline_step(tmp_path):
    import shutil
    from pathlib import Path

    from lerobot.data_platform.agent import _require_within
    from lerobot.data_platform.operators.registry import worker_manifest
    from tests.datasets.test_control_plane import _app, _bootstrap, _store
    from tests.datasets.test_quality_eef import make_eef

    store = _store(tmp_path)
    app = _app(tmp_path, store)
    app.static_folder = str(tmp_path / "console/static")
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    source = make_eef(tmp_path / "source")
    output_parent = tmp_path / "outputs"
    output_parent.mkdir()
    output = output_parent / "derived"
    token, node = store.enroll_node(
        name="local-test",
        hostname="localhost",
        allowed_roots=[str(source)],
        writable_roots=[str(output_parent)],
        enrollment_token="test",
        expected_token="test",
        capabilities={
            **worker_manifest(),
            "operations": ["preprocess.split"],
            "local_requests": True,
            "job_protocol": 2,
            "data_profile_protocol": 100,
        },
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "root": str(source),
                "dataset_key": "local/source",
                "output_dir": str(tmp_path / "cache"),
                "metadata": json.loads((source / "meta/info.json").read_text()),
            }
        ],
    )[0]
    management = tmp_path / "console/management"
    management.mkdir(parents=True)
    state = management / "local-agent.json"
    state.write_text(json.dumps({"node_id": node["node_id"], "name": node["name"]}))
    config_path = management / "local-executor.json"
    original = {
        "state_file": str(state),
        "allowed_roots": [str(source)],
        "writable_roots": [str(output_parent)],
        "allow_source_mutations": False,
    }
    config_path.write_text(json.dumps(original))
    job = store.create_job(
        location_id=location["location_id"],
        requested_by=store.list_users()[0]["user_id"],
        operation="preprocess.split",
        options={
            "out_root": str(output),
            "operator_snapshot": {"operator_id": "preprocess.split", "version": "1.0.0"},
        },
    )
    claimed = store.claim_job(node["node_id"], worker_instance_id="local-worker")
    assert claimed["job_id"] == job["job_id"]
    shutil.copytree(source, output)
    result = {
        "dataset_location": {
            "root": str(output),
            "dataset_key": "local/derived",
            "output_dir": str(output_parent / "vis/derived"),
            "metadata": json.loads((output / "meta/info.json").read_text()),
        }
    }
    response = client.post(
        f"/api/agents/jobs/{job['job_id']}/complete",
        json={"status": "done", "result": result},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Job-Attempt": claimed["execution"]["attempt_id"],
            "X-Job-Credential": claimed["execution"]["credential"],
        },
    )
    assert response.status_code == 200, response.json
    updated = json.loads(config_path.read_text())
    assert updated["allowed_roots"] == sorted([str(source), str(output)])
    assert updated["writable_roots"] == original["writable_roots"]
    assert updated["allow_source_mutations"] is False
    assert store.list_nodes()[0]["allowed_roots"] == updated["allowed_roots"]
    assert _require_within(output, [Path(root) for root in updated["allowed_roots"]], "next input") == output
    with pytest.raises(PermissionError):
        _require_within(
            output_parent / "unregistered", [Path(root) for root in updated["allowed_roots"]], "input"
        )


@pytest.mark.parametrize("escape", [False, True])
def test_local_derived_read_permission_rejects_unplanned_or_escaping_output(tmp_path, escape):

    from lerobot.data_platform.local_execution import permit_local_derived_input

    source = tmp_path / "source"
    source.mkdir()
    allowed_output = tmp_path / "outputs"
    allowed_output.mkdir()
    output = (tmp_path / "outside") if escape else (allowed_output / "unplanned")
    output.mkdir()
    management = tmp_path / "management"
    management.mkdir()
    state = management / "local-agent.json"
    state.write_text(json.dumps({"node_id": "local-node", "name": "local-test"}))
    config = {
        "state_file": str(state),
        "allowed_roots": [str(source)],
        "writable_roots": [str(allowed_output)],
        "allow_source_mutations": False,
    }
    path = management / "local-executor.json"
    path.write_text(json.dumps(config))
    node = {"node_id": "local-node", "name": "local-test", "capabilities": {"local_requests": True}}
    location = {"node_id": "local-node", "location_id": "derived", "root": str(output)}
    store = SimpleNamespace(
        get_location=lambda key: location if key == "derived" else {"root": str(source)},
        configure_local_execution_roots=lambda *args: pytest.fail("Rejected paths cannot expand read roots"),
    )
    job = {
        "location_id": "source",
        "options": {
            "operator_snapshot": {"operator_id": "preprocess.split"},
            "out_root": str(allowed_output / "planned"),
        },
    }
    with pytest.raises(PermissionError):
        permit_local_derived_input(store, node, job, location, management)
    assert json.loads(path.read_text()) == config
