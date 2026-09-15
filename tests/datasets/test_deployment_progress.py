"""Deployment progress must identify the current attempt and avoid exposing private state."""

import json

import pytest

from lerobot.data_platform.deployment_progress import production_progress


def write(root, name, value):
    (root / name).write_text(json.dumps(value))


def test_old_success_is_not_used_for_a_new_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr("lerobot.data_platform.deployment_progress.time.time", lambda: 101)
    write(tmp_path, "promotion-request.json", {"id": "new", "release": "R2", "started_at": 100})
    write(tmp_path, "deployment.json", {"release": "R2", "started_at": 10, "status": "installed"})
    result = production_progress(tmp_path, "activating")
    assert result["status"] == "running"
    assert result["phase"] == "queued"
    assert result["id"] == "new"


@pytest.mark.parametrize(
    "status,unit,expected",
    [
        ("running", "active", "running"),
        ("installed", "active", "running"),
        ("installed", "inactive", "installed"),
        ("installed", "failed", "failed"),
        ("failed", "failed", "failed"),
        ("running", "inactive", "failed"),
    ],
)
def test_progress_survives_refresh_and_handles_process_exit(tmp_path, monkeypatch, status, unit, expected):
    monkeypatch.setattr("lerobot.data_platform.deployment_progress.time.time", lambda: 200)
    write(tmp_path, "promotion-request.json", {"id": "attempt", "release": "R2", "started_at": 100})
    write(
        tmp_path,
        "deployment.json",
        {
            "release": "R2",
            "started_at": 101,
            "updated_at": 150,
            "status": status,
            "phase": "backup",
            "backup": "/private/backup",
            "requested_by": "private-user",
            "steps": [{"phase": "backup", "at": 150, "password": "private-secret"}],
        },
    )
    result = production_progress(tmp_path, unit)
    assert result["status"] == expected
    assert result == production_progress(tmp_path, unit)
    assert "private" not in json.dumps(result)
    assert result["steps"][0]["message"] == "Backing up databases and platform state"


def test_dispatch_failure_is_durable(tmp_path):
    write(
        tmp_path,
        "promotion-request.json",
        {
            "id": "attempt",
            "release": "R2",
            "started_at": 100,
            "status": "failed",
            "finished_at": 101,
        },
    )
    result = production_progress(tmp_path, "inactive")
    assert result["status"] == "failed"
    assert result["finished_at"] == 101
    assert "failed" in result["message"]


@pytest.mark.parametrize("dispatch_failure", [False, True])
def test_promotion_dispatch_records_attempt_before_start(tmp_path, monkeypatch, dispatch_failure):
    from types import SimpleNamespace

    from lerobot.data_platform import deployment, promotion, releases

    monkeypatch.setattr(promotion.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment, "Deployment", lambda name: SimpleNamespace(root=tmp_path))
    original = promotion.Path
    monkeypatch.setattr(
        promotion, "Path", lambda value: tmp_path / "lock" if value.endswith(".lock") else original(value)
    )
    monkeypatch.setattr(
        promotion,
        "snapshot",
        lambda: {
            "can_promote": True,
            "revision": "rev",
            "dev": {"release": "R2"},
            "reason": None,
        },
    )
    monkeypatch.setattr(releases, "verify_release", lambda *args, **kwargs: None)

    def run(command, **kwargs):
        if command[0] == "systemd-run":
            assert json.loads((tmp_path / "promotion-request.json").read_text())["status"] == "queued"
            if dispatch_failure:
                raise RuntimeError("dispatch failed")

    monkeypatch.setattr(promotion.subprocess, "run", run)
    if dispatch_failure:
        with pytest.raises(RuntimeError):
            promotion.handle_request({"action": "promote", "revision": "rev"})
        assert json.loads((tmp_path / "promotion-request.json").read_text())["status"] == "failed"
    else:
        result = promotion.handle_request({"action": "promote", "revision": "rev"})
        assert result["deployment_id"] == json.loads((tmp_path / "promotion-request.json").read_text())["id"]
