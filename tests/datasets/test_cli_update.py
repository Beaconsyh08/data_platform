"""Client updates preserve custom installations and reject invalid distribution responses."""

import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients/data-platform-cli"))
from data_platform_cli import shell  # noqa: E402
from data_platform_cli import update as updater  # noqa: E402


@pytest.fixture
def managed(monkeypatch, tmp_path):
    target = tmp_path / "custom prefix" / updater.__version__
    target.mkdir(parents=True)
    (target / "data-platform-install.json").write_text("{}")
    bin_dir = tmp_path / "custom bin"
    bin_dir.mkdir()
    (bin_dir / "data-platform").write_text("previous launcher")
    monkeypatch.setattr(sys, "prefix", str(target))
    monkeypatch.setenv("DATA_PLATFORM_CLI_BIN_DIR", str(bin_dir))
    return target, bin_dir


def response(monkeypatch, content, url=None):
    value = io.BytesIO(content)
    value.url = url or updater.INSTALLER_URL
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_, **__: value)


def test_update_preserves_custom_paths_and_cleans_download(managed, monkeypatch):
    target, bin_dir = managed
    response(monkeypatch, b'VERSION = "99.0.0"\n')
    calls = []

    def run(args, **kwargs):
        assert Path(args[1]).read_bytes() == b'VERSION = "99.0.0"\n'
        assert args[2:] == ["--prefix", str(target.parent), "--bin-dir", str(bin_dir)]
        assert kwargs == {"check": True}
        calls.append(args)

    monkeypatch.setattr(updater.subprocess, "run", run)
    assert updater.update() == 0
    assert len(calls) == 1 and not Path(calls[0][1]).exists()


@pytest.mark.parametrize("version", ["0.0.1", updater.__version__])
def test_same_or_older_latest_never_reinstalls(managed, monkeypatch, version):
    response(monkeypatch, f'VERSION = "{version}"\n'.encode())
    monkeypatch.setattr(updater.subprocess, "run", lambda *_, **__: pytest.fail("Unexpected installation"))
    assert updater.update() == 0


@pytest.mark.parametrize("bad", ["redirect", "oversize", "invalid", "network"])
def test_bad_download_keeps_launcher(managed, monkeypatch, bad):
    if bad == "network":

        def fail(*_, **__):
            raise OSError("network failed")

        monkeypatch.setattr(updater.urllib.request, "urlopen", fail)
    else:
        response(
            monkeypatch,
            b"x" * (1024 * 1024 + 1) if bad == "oversize" else b"invalid",
            "https://elsewhere.example/install.py" if bad == "redirect" else None,
        )
    monkeypatch.setattr(updater.subprocess, "run", lambda *_, **__: pytest.fail("Unexpected installation"))
    assert updater.update() == 1
    assert (managed[1] / "data-platform").read_text() == "previous launcher"


def test_failed_installer_cleans_download(managed, monkeypatch):
    response(monkeypatch, b'VERSION = "99.0.0"\n')
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(updater.subprocess, "run", fail)
    assert updater.update() == 1
    assert not Path(calls[0][1]).exists()
    assert (managed[1] / "data-platform").read_text() == "previous launcher"


def test_update_requires_managed_launcher(monkeypatch):
    monkeypatch.delenv("DATA_PLATFORM_CLI_BIN_DIR", raising=False)
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *_, **__: pytest.fail("Unexpected download")
    )
    assert updater.update() == 1


def test_update_command_does_not_require_login_or_server(monkeypatch):
    monkeypatch.setattr(updater, "update", lambda: 7)
    monkeypatch.setattr(shell.getpass, "getpass", lambda *_: pytest.fail("Unexpected login"))
    assert shell.main(["update"]) == 7
    assert shell.main(["--server", "https://platform.example.com", "update"]) == 7
