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


@pytest.mark.parametrize(
    "phase,active_jobs,unit,previous,approved,dev_maintenance,maintenance_release,can_retry",
    [
        ("draining", 0, "inactive", "R1", True, False, "R2", True),
        ("draining", 0, "failed", "R1", True, False, "R2", True),
        ("draining", 1, "inactive", "R1", True, False, "R2", False),
        ("draining", 0, "active", "R1", True, False, "R2", False),
        ("draining", 0, "inactive", "other", True, False, "R2", False),
        ("draining", 0, "inactive", "R1", False, False, "R2", False),
        ("draining", 0, "inactive", "R1", True, True, "R2", False),
        ("draining", 0, "inactive", "R1", True, False, None, True),
        ("draining", 1, "inactive", "R1", True, False, None, False),
        ("draining", 0, "inactive", "R1", True, False, "manual", False),
        ("draining", None, "inactive", "R1", True, False, "R2", False),
        ("stopping", 0, "inactive", "R1", True, False, "R2", False),
        ("migration", 0, "inactive", "R1", True, False, "R2", False),
        ("agents", 0, "inactive", "R1", True, False, "R2", False),
    ],
)
def test_web_retry_preserves_maintenance_and_partial_upgrade_guards(
    tmp_path,
    monkeypatch,
    phase,
    active_jobs,
    unit,
    previous,
    approved,
    dev_maintenance,
    maintenance_release,
    can_retry,
):
    from types import SimpleNamespace

    from lerobot.data_platform import deployment, promotion, releases

    candidate = tmp_path / "R3"
    candidate.mkdir()
    write(candidate, "release.json", {"commit": "test"})
    if approved:
        write(
            candidate,
            "approval.json",
            {
                "environment": "dev",
                "instance_id": "dev-id",
                "manifest_sha256": releases.digest(candidate / "release.json"),
            },
        )
    monkeypatch.setattr(releases, "RELEASE_ROOT", tmp_path)
    monkeypatch.setattr(deployment.Deployment, "root", property(lambda self: tmp_path / self.environment))
    root = tmp_path / "prod"
    root.mkdir()
    write(root, "promotion-request.json", {"id": "old", "release": "R2", "started_at": 100})
    write(
        root,
        "deployment.json",
        {
            "release": "R2",
            "previous": str(root / "releases" / previous),
            "started_at": 101,
            "status": "failed",
            "phase": phase,
            "steps": [{"phase": phase, "at": 101}],
        },
    )

    def get(url, **kwargs):
        dev = ":9092/" in url
        value = {
            "environment": "dev" if dev else "prod",
            "release": "R3" if dev else "R1",
            "instance_id": "dev-id" if dev else "prod-id",
            "maintenance": dev_maintenance if dev else True,
            "active_jobs": 0 if dev else active_jobs,
            "active_job_counts": {
                "running": 0,
                "cancel_requested": 0,
                "interrupted": 0 if dev else active_jobs,
            },
            "maintenance_release": "" if dev else maintenance_release,
        }
        if not dev and maintenance_release is None:
            value.pop("maintenance_release")
            value.pop("active_job_counts")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

    monkeypatch.setattr(promotion.requests, "get", get)
    monkeypatch.setattr(promotion.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=unit))
    state = promotion.snapshot()
    assert state["can_promote"] is can_retry
    assert state["can_retry"] is can_retry
    if can_retry:
        assert state["prod"]["maintenance"] is True
        assert "Retry" in state["progress"]["message"]
    elif phase == "draining" and active_jobs:
        assert "interrupted" in state["progress"]["message"]


def test_old_failure_cannot_make_a_new_request_retryable(tmp_path):
    write(tmp_path, "promotion-request.json", {"id": "new", "release": "R2", "started_at": 200})
    write(
        tmp_path,
        "deployment.json",
        {"release": "R2", "started_at": 100, "status": "failed", "phase": "draining"},
    )
    result = production_progress(tmp_path, "failed")
    assert result["retryable"] is False


@pytest.mark.parametrize(
    "evidence",
    [{"backup": "/private/backup"}, {"steps": [{"phase": "stopping"}]}, {"status": "running"}],
)
def test_uncertain_or_partial_upgrade_cannot_use_drain_retry(tmp_path, evidence):
    write(tmp_path, "promotion-request.json", {"id": "attempt", "release": "R2", "started_at": 100})
    write(
        tmp_path,
        "deployment.json",
        {"release": "R2", "started_at": 101, "status": "failed", "phase": "draining", **evidence},
    )
    assert production_progress(tmp_path, "inactive")["retryable"] is False


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
