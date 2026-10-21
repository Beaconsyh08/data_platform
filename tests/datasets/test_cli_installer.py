"""Integrity and preservation checks for the standalone CLI installer."""

import hashlib
import importlib.util
import io
import json
import shlex
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).parents[2] / "clients/data-platform-cli/install.py"
    spec = importlib.util.spec_from_file_location("cli_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("changed", ["content", "redirect", "oversize"])
def test_download_rejects_changed_release(installer, monkeypatch, tmp_path, changed):
    name = "client.whl"
    installer.FILES = {name: hashlib.sha256(b"expected").hexdigest()}
    response = io.BytesIO(b"x" * (1024 * 1024 + 1) if changed == "oversize" else b"changed")
    response.url = installer.BASE_URL + "/" + name if changed != "redirect" else "http://elsewhere/file"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError):
        installer.download(name, tmp_path)
    assert not (tmp_path / name).exists()


def test_existing_unrelated_installation_is_preserved(installer, tmp_path):
    prefix = tmp_path / "client"
    old = prefix / installer.VERSION
    old.mkdir(parents=True)
    (old / "user-file").write_text("keep")
    with pytest.raises(RuntimeError, match="unrecognized"):
        installer.install(prefix, tmp_path / "bin")
    assert (old / "user-file").read_text() == "keep"
    assert not (prefix / ".install.lock").exists()


def test_failed_install_does_not_replace_launcher(installer, monkeypatch, tmp_path):
    prefix, bin_dir = tmp_path / "client", tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "data-platform"
    previous = "#!/bin/sh\n" + installer.LAUNCHER_MARKER + "\nexit 0\n"
    launcher.write_text(previous)

    def download(name, directory):
        path = directory / name
        path.write_bytes(b"fixture")
        return path

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "venv")

    monkeypatch.setattr(installer, "download", download)
    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(prefix, bin_dir)
    assert launcher.read_text() == previous
    assert not (prefix / installer.VERSION).exists()
    assert not (prefix / ".install.lock").exists()


def test_managed_launcher_preserves_custom_update_directory(installer, tmp_path):
    prefix, bin_dir = tmp_path / "custom install", tmp_path / "custom bin"
    target = prefix / installer.VERSION
    target.mkdir(parents=True)
    ca = target / "data-platform-ca.crt"
    ca.write_bytes(b"test CA")
    installer.FILES = {ca.name: hashlib.sha256(ca.read_bytes()).hexdigest()}
    (target / "data-platform-install.json").write_text(json.dumps(installer.FILES))
    launcher = installer.install(prefix, bin_dir)
    assert f"export DATA_PLATFORM_CLI_BIN_DIR={shlex.quote(str(bin_dir))}\n" in launcher.read_text()
    assert ' "$@"' in launcher.read_text()
