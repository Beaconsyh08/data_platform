"""Lightweight client shares authenticated APIs without retaining credentials or replaying actions."""

import json
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from werkzeug.serving import make_server, make_ssl_devcert

from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_ai_assistant import assistant, call, processing_setup  # noqa: F401
from tests.datasets.test_ai_assistant_context import bind_lifecycle
from tests.datasets.test_control_plane import _app

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients/data-platform-cli"))
from data_platform_cli.client import ClientError, PlatformClient  # noqa: E402
from data_platform_cli.shell import safe_terminal  # noqa: E402


class Transport(requests.Session):
    def __init__(self, replies):
        super().__init__()
        self.replies, self.calls = replies, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        code, payload = reply
        response = requests.Response()
        response.status_code = code
        response._content = json.dumps(payload).encode()
        return response


def test_login_uses_tls_csrf_and_no_redirects():
    transport = Transport(
        [
            (200, {}),
            (200, {"authenticated": True, "csrf_token": "token-from-server", "user": {"username": "test"}}),
            (200, {}),
        ]
    )
    client = PlatformClient("https://platform.example.com", session=transport, ca_bundle="internal-ca.pem")
    client.login("test", "fake-test-password")
    assert client.user["username"] == "test" and transport.verify == "internal-ca.pem"
    assert all(not call[2]["allow_redirects"] for call in transport.calls)
    assert transport.calls[0][2]["headers"]["X-Data-Platform-CSRF"] == "anonymous"
    client.logout()
    assert transport.calls[-1][2]["headers"]["X-Data-Platform-CSRF"] == "token-from-server"
    assert client.user is None and not transport.cookies
    assert not hasattr(client, "password")


@pytest.mark.parametrize(
    "url", ["http://server", "https://user:password@server", "https://server/path", "https://server?token=x"]
)
def test_untrusted_origins_are_rejected(url):
    with pytest.raises(ClientError):
        PlatformClient(url)


def test_timeout_recovery_keeps_request_identity_and_context():
    transport = Transport(
        [
            requests.Timeout(),
            (
                200,
                {
                    "request_id": "request",
                    "conversation_id": "conversation",
                    "status": "done",
                    "message": "ok",
                },
            ),
            (
                200,
                {
                    "conversation_id": "conversation",
                    "revision": 1,
                    "history": [],
                    "context": {"type": "quality", "target_id": "report"},
                },
            ),
        ]
    )
    client = PlatformClient("https://platform.example.com", session=transport)
    client.select({"location_id": "dataset"})
    client.conversation["context"] = {"type": "quality", "target_id": "report"}
    with pytest.raises(ClientError):
        client.chat("Explain coverage")
    pending = dict(client.pending)
    client.resume()
    assert transport.calls[0][2]["json"] == transport.calls[1][2]["json"] == pending
    assert pending["context"]["type"] == "quality" and client.pending is None


def test_confirm_transmits_only_immutable_action_identity_and_revision():
    transport = Transport([(200, {"run_id": "run", "status": "queued"})])
    client = PlatformClient("https://platform.example.com", session=transport)
    client.last_action = {
        "action_id": "action",
        "revision": 3,
        "parameters": {"ignored": "client cannot override"},
    }
    client.confirm()
    assert transport.calls[0][2]["json"] == {"revision": 3}
    assert client.last_action["execution"]["run_id"] == "run"


def test_rejected_admission_does_not_leave_an_unrecoverable_pending_request():
    client = PlatformClient("https://platform.example.com", session=Transport([(429, {})]))
    client.select({"location_id": "dataset"})
    with pytest.raises(ClientError):
        client.chat("Explain the dataset")
    assert client.pending is None


def test_terminal_control_sequences_and_error_bodies_are_not_rendered():
    assert safe_terminal("before\x1b[2Jafter\x1b]52;c;secret\x07\u009b2J") == "beforeafter2J"
    transport = Transport([(503, {"error": "password=private SELECT * FROM users"})])
    client = PlatformClient("https://platform.example.com", session=transport)
    with pytest.raises(ClientError) as error:
        client.request("GET", "/api/auth/status")
    assert "private" not in str(error.value) and "SELECT" not in str(error.value)


def test_real_https_login_chat_restore_confirm_and_logout(assistant, tmp_path, monkeypatch):  # noqa: F811
    service, fake, owner, body = assistant
    bind_lifecycle(service, tmp_path)
    fake.responses = [
        call("describe_operator", {"operator_id": "viewer.prepare"}),
        call("prepare_action", {"operator_id": "viewer.prepare", "parameters": {}}),
    ]
    monkeypatch.setenv("DATA_PLATFORM_ENV", "dev")
    monkeypatch.setenv("DATA_PLATFORM_INSTANCE_ID", str(uuid.uuid4()))
    app = _app(tmp_path, service.store)
    ctx = SimpleNamespace(control_plane_store=service.store, lifecycle_store=None)
    register_processing_routes(app, ctx)
    register_ai_assistant_routes(app, ctx)
    app.extensions["data_platform_assistant"] = service
    certificate, key = make_ssl_devcert(str(tmp_path / "localhost"), host="localhost")
    server = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=(certificate, key))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"https://localhost:{server.server_port}"
    client = PlatformClient(origin, ca_bundle=certificate)
    try:
        with pytest.raises(ClientError):
            PlatformClient(origin).request("GET", "/api/auth/status")
        assert client.login("admin", "strong-admin-password")["user_id"] == owner
        client.select(next(item for item in client.datasets() if item["location_id"] == body["location_id"]))
        assert client.chat("帮我准备 Viewer")["action"]["can_execute"]
        assert not service.manager.runs(owner) and not service.store.list_jobs()
        conversation_id = client.conversation["conversation_id"]
        page = client.conversations()
        assert page["conversations"][0]["conversation_id"] == conversation_id
        settings = client.preferences()
        settings["settings"]["language"] = "en"
        saved = client.save_preferences(settings["revision"], settings["settings"])
        from lerobot.data_platform.ai_tools.personalization import preferences

        assert preferences(service, owner) == saved
        token, client.csrf = client.csrf, "invalid"
        with pytest.raises(ClientError):
            client.clear_preferences(saved["revision"])
        client.csrf = token
        assert client.clear_preferences(saved["revision"])["settings"]["language"] == "auto"
        client.new_chat()
        client.restore(conversation_id)
        # The confirmation route accepts no client override, and CSRF still protects it.
        path = "/api/ai-assistant/actions/" + client.last_action["action_id"] + "/confirm"
        with pytest.raises(ClientError):
            client.request("POST", path, {"revision": 1, "parameters": {}})
        token, client.csrf = client.csrf, "invalid"
        with pytest.raises(ClientError):
            client.confirm()
        client.csrf = token
        receipt = client.confirm()
        assert client.confirm()["run_id"] == receipt["run_id"]
        assert len(service.manager.runs(owner)) == 1
        assert client.status()["status"] == "queued"
        client.logout()
        assert not client.request("GET", "/api/auth/status")["authenticated"]
    finally:
        client.session.close()
        server.shutdown()
        thread.join(timeout=5)


def test_status_before_confirmation_and_safe_result_links():
    transport = Transport(
        [
            (
                200,
                {
                    "state": "ready",
                    "links": [
                        {"title": "Report", "url": "/curation?run=example"},
                        {"title": "Unsafe", "url": "//other.example"},
                    ],
                },
            )
        ]
    )
    client = PlatformClient("https://platform.example.com", session=transport)
    client.select({"location_id": "location"})
    client.last_action = {"action_id": "action"}
    assert client.links() == [{"title": "Report", "url": "https://platform.example.com/curation?run=example"}]
    assert client.conversation["operation"]["state"] == "ready"
    assert transport.calls[0][0] == "GET"


def test_explicit_repeat_is_distinct_from_default_confirmation():
    transport = Transport([(200, {"run_id": "run", "status": "queued"})])
    client = PlatformClient("https://platform.example.com", session=transport)
    client.last_action = {"action_id": "action", "revision": 2}
    client.confirm(rerun=True)
    assert transport.calls[0][2]["json"] == {"revision": 2, "rerun": True}


def test_web_handoff_preserves_environment_context_and_has_no_credentials():
    transport = Transport([])
    client = PlatformClient("https://platform.example.com:8443", session=transport)
    client.select({"location_id": "location&other=value"})
    assert client.links() == [{"title": "Web console", "url": client.origin + "/"}]
    client.conversation.update(revision=1, context={"type": "quality", "target_id": "report/one?x=2"})
    client.csrf = "private-csrf"
    transport.cookies.set("session", "private-session")
    link = client.web_links()[1]["url"]
    assert urlsplit(link).netloc == "platform.example.com:8443"
    assert parse_qs(urlsplit(link).query) == {
        "location_id": ["location&other=value"],
        "assistant_conversation": [client.conversation["conversation_id"]],
        "type": ["quality"],
        "target_id": ["report/one?x=2"],
    }
    assert "private" not in link and not transport.calls
    client.new_chat()
    assert len(client.web_links()) == 1


def test_web_and_version_flags_do_not_login(monkeypatch, capsys):
    from data_platform_cli import __version__, shell

    monkeypatch.setattr(shell.getpass, "getpass", lambda *_: pytest.fail("Unexpected login"))
    monkeypatch.setattr(requests.Session, "request", lambda *_, **__: pytest.fail("Unexpected request"))
    assert shell.main(["--server", "https://platform.example.com:8443", "--web"]) == 0
    assert capsys.readouterr().out == "https://platform.example.com:8443/\n"
    with pytest.raises(SystemExit) as result:
        shell.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"Data Platform CLI {__version__}"


@pytest.mark.parametrize("exit_command", ["/exit", "/quit", "exit", "quit"])
def test_shell_prints_web_links_and_exits_with_pending_response(monkeypatch, capsys, exit_command):
    import prompt_toolkit
    from data_platform_cli import shell

    class FakeClient(PlatformClient):
        def login(self, username, password):
            self.user = {"username": username, "role": "viewer"}
            return self.user

        def datasets(self):
            return [{"location_id": "location"}]

        def select(self, location):
            super().select(location)
            self.pending = {"request_id": "pending"}

        def chat(self, message):
            pytest.fail("Exit command must not be sent as a chat message")

        def logout(self):
            self.user = None

    inputs = iter(["1", "/web", "/open", exit_command])
    monkeypatch.setattr(shell, "PlatformClient", FakeClient)
    monkeypatch.setattr(shell.getpass, "getpass", lambda *_: "test-password")
    monkeypatch.setattr(
        prompt_toolkit, "PromptSession", lambda **_: SimpleNamespace(prompt=lambda *_: next(inputs))
    )
    assert shell.main(["--server", "https://platform.example.com", "--username", "test"]) == 0
    output = capsys.readouterr().out
    assert output.count("Web console: https://platform.example.com/") == 3
    assert "Ask the assistant to prepare" not in output
