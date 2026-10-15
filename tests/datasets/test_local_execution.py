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
