"""Terminal result navigation is usable without model-generated relative URLs or repeated cards."""

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients/data-platform-cli"))
from data_platform_cli import shell  # noqa: E402
from data_platform_cli.client import PlatformClient  # noqa: E402
from data_platform_cli.presentation import navigation_request, response_markdown  # noqa: E402


def test_markdown_links_use_authorized_full_url_and_title():
    output = io.StringIO()
    console = Console(file=output, width=200)
    console.print(
        response_markdown(
            "**Viewer**: [wrong dataset label](/node/data/episode_0)\n\n[private](https://elsewhere.example)",
            [{"title": "Open prepared Viewer", "url": "https://platform.example.com/node/data/episode_0"}],
            "https://platform.example.com",
        )
    )
    text = output.getvalue()
    assert "Open prepared Viewer (https://platform.example.com/node/data/episode_0)" in text
    assert "wrong dataset label" not in text and "elsewhere.example" not in text
    assert "**" not in text and "Unverified link omitted" in text


@pytest.mark.parametrize("message", ["网址在哪儿", "你给我的链接不行呀", "网址在哪？", "Where is the link?"])
def test_explicit_link_questions_are_navigation(message):
    assert navigation_request(message)


@pytest.mark.parametrize(
    "message", ["准备viewer", "重建 Viewer", "链接打不开，请重新运行 Viewer", "Explain URLs"]
)
def test_other_requests_are_not_intercepted(message):
    assert not navigation_request(message)


def test_followup_has_no_old_card_and_link_questions_create_no_model_turn(monkeypatch, capsys):
    import prompt_toolkit

    clients = []

    class Client(PlatformClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.messages = []
            clients.append(self)

        def login(self, username, password):
            self.user = {"username": username, "role": "operator"}
            return self.user

        def datasets(self):
            return [{"location_id": "dataset"}]

        def chat(self, message):
            self.messages.append(message)
            if len(self.messages) == 1:
                self.last_action = {
                    "action_id": "action",
                    "title": "Prepare Viewer",
                    "dataset": "fixture",
                    "node": "node",
                    "parameters": {},
                    "can_execute": True,
                    "is_current": True,
                }
                self.conversation["operation"] = self.status()
                return {"message": "A previous result exists.", "action": self.last_action}
            self.last_action["is_current"] = False
            return {"message": "**Answer**: this is a description, not a new operation."}

        def status(self):
            return {
                "state": "history",
                "status": "done",
                "requires_rerun": True,
                "links": [{"title": "Open prepared Viewer", "url": "/node/fixture/episode_0"}],
            }

        def logout(self):
            self.user = None

    inputs = iter(["1", "准备viewer", "解释一下数据", "网址在哪儿", "你给我的链接不行呀", "exit"])
    monkeypatch.setattr(shell, "PlatformClient", Client)
    monkeypatch.setattr(shell.getpass, "getpass", lambda *_: "fixture-password")
    monkeypatch.setattr(
        prompt_toolkit, "PromptSession", lambda **_: SimpleNamespace(prompt=lambda *_: next(inputs))
    )
    assert shell.main(["--server", "https://platform.example.com", "--username", "test"]) == 0
    output = capsys.readouterr().out
    assert output.count("Prepare Viewer · Dataset:") == 1
    assert "Action is stale" not in output and "{}" not in output
    assert output.count("https://platform.example.com/node/fixture/episode_0") == 3
    assert clients[0].messages == ["准备viewer", "解释一下数据"]
    assert "**Answer**" not in output


def test_untrusted_server_link_forms_are_rejected():
    client = PlatformClient("https://platform.example.com")
    observation = {
        "links": [
            {"title": "bad", "url": path}
            for path in ["//other.example", "https://other.example", "/\\other.example", "/x\nsecret"]
        ]
    }
    assert client.result_links(observation) == []
