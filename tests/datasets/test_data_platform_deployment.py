import os
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY_ROOT = Path(__file__).parents[2] / "deploy" / "data-platform"


def test_dynamic_ip_nginx_config_does_not_bind_the_current_address():
    config = (DEPLOY_ROOT / "nginx-dynamic-ip.conf").read_text()

    assert "10.8.8.106" not in config
    assert "listen 80 default_server;" in config
    assert "listen 443 ssl http2 default_server;" in config
    assert "allow 127.0.0.1;" in config
    assert "allow 10.8.0.0/16;" in config
    assert "deny all;" in config
    assert "proxy_pass http://127.0.0.1:9091;" in config


def test_named_deployment_uses_explicit_environment_launcher():
    for script in ("update-server.sh", "update-all.sh", "restart-server.sh", "configure-dynamic-ip.sh"):
        assert "run-command.sh" in (DEPLOY_ROOT / script).read_text()
    assert "Explicit --env dev|prod is required" in (DEPLOY_ROOT / "run-command.sh").read_text()


@pytest.mark.parametrize(
    "status,paused,allowed",
    [
        ("running", "1", False),
        ("interrupted", "1", False),
        ("queued", "0", False),
        ("queued", "1", True),
        ("done", "0", True),
    ],
)
def test_upgrade_preflight_refuses_unfinished_execution(tmp_path, status, paused, allowed):
    import sqlite3

    database = tmp_path / "control.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE dp_jobs (status TEXT)")
        connection.execute("INSERT INTO dp_jobs VALUES (?)", (status,))
    result = subprocess.run(
        [sys.executable, str(DEPLOY_ROOT / "prepare-server-update.py"), "--check-idle"],
        env={
            **os.environ,
            "DATA_PLATFORM_DATABASE_URL": f"sqlite:///{database}",
            "DATA_PLATFORM_PAUSE_CLAIMS": paused,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == allowed
    if not allowed:
        assert "Upgrade stopped" in result.stderr


@pytest.mark.parametrize("code", [1045, 1054, 1698, 2003, 2005, 9999])
def test_preflight_database_error_reports_code_without_secrets(code):
    import runpy

    from sqlalchemy.exc import OperationalError

    module = runpy.run_path(str(DEPLOY_ROOT / "prepare-server-update.py"))
    driver = Exception(code, "mysql://private-user:secret-password@private-host/database")
    error = OperationalError("SELECT secret", {}, driver)
    message = module["database_failure"](error)
    assert f"driver_code={code}" in message
    assert "secret" not in message and "private" not in message


def test_agent_extra_exports_execution_logging_dependency():
    import shutil

    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required to validate the deployment dependency export")
    result = subprocess.run(
        [uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--extra", "data-platform-agent"],
        cwd=DEPLOY_ROOT.parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("sqlalchemy==") for line in result.stdout.splitlines())
    installer = (DEPLOY_ROOT / "agent-bundle/install.sh").read_text()
    assert installer.index(
        "from lerobot.data_platform.management_storage import EventSpool"
    ) < installer.index('mv -Tf "$INSTALL_ROOT/.current-next" "$INSTALL_ROOT/current"')


@pytest.mark.parametrize("name", ["../outside", "", "bad name", "a" * 101])
def test_upgrade_rejects_unsafe_package_names(name):
    from lerobot.data_platform.releases import validate_version

    with pytest.raises(ValueError):
        validate_version(name)
