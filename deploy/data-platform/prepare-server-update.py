"""Prepare only management directories; never change dataset ownership or print credentials."""

import os
import pwd
import sys
from pathlib import Path


def database_failure(exc):
    """Expose driver codes and known causes without echoing URLs or driver messages."""
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    code = args[0] if args and isinstance(args[0], int) else None
    causes = {
        1044: "Database account lacks access to the configured database",
        1045: "Database authentication failed; check the control database account/password",
        1049: "Configured control database does not exist",
        1054: "Control database table schema does not match the task-state query",
        1142: "Database account lacks permission for the task-state query",
        1146: "Required control database table does not exist",
        1698: "MySQL authentication denied; check socket authentication and the service account identity",
        2002: "Cannot connect to the configured MySQL socket",
        2003: "Cannot connect to the configured MySQL host/port",
        2005: "Cannot resolve the configured MySQL hostname",
        2013: "Database connection was lost during the query",
    }
    reason = causes.get(code, "Database preflight failed; credentials and raw driver text withheld")
    return f"{reason} [exception={type(original).__name__}, driver_code={code}]"


def check_idle():
    from sqlalchemy import create_engine, inspect, text

    url = os.environ.get("DATA_PLATFORM_DATABASE_URL")
    if not url:
        raise SystemExit("DATA_PLATFORM_DATABASE_URL is missing; cannot verify task state")
    engine = None
    try:
        engine = create_engine(url.strip())
        if not inspect(engine).has_table("dp_jobs"):
            return
        with engine.connect() as connection:
            active = connection.execute(
                text(
                    "SELECT COUNT(*) FROM dp_jobs WHERE status IN ('running','cancel_requested','interrupted')"
                )
            ).scalar_one()
            queued = connection.execute(
                text("SELECT COUNT(*) FROM dp_jobs WHERE status = 'queued'")
            ).scalar_one()
        if active:
            raise SystemExit(
                f"Upgrade stopped: {active} active/unconfirmed jobs; wait or reconcile them first"
            )
        if queued and os.environ.get("DATA_PLATFORM_PAUSE_CLAIMS") != "1":
            raise SystemExit("Upgrade stopped: queued jobs exist; pause claims before upgrading")
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"Cannot verify task state: {database_failure(exc)}") from None
    finally:
        if engine is not None:
            engine.dispose()


def prepare_directories():
    output = os.environ.get("DATA_PLATFORM_OUTPUT_DIR")
    remote = os.environ.get("DATA_PLATFORM_REMOTE_CACHE_ROOT")
    if not output or not remote:
        raise SystemExit("Set DATA_PLATFORM_OUTPUT_DIR and DATA_PLATFORM_REMOTE_CACHE_ROOT before upgrading")
    account = pwd.getpwnam(
        "data-platform-dev" if os.environ.get("DATA_PLATFORM_ENV") == "dev" else "data-platform"
    )
    for path in (Path(output) / "management", Path(remote).parent / "management"):
        if not path.is_absolute() or path.is_symlink():
            raise SystemExit("Management directories must be absolute paths, not symlinks")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, account.pw_uid, account.pw_gid)
        path.chmod(0o700)
    print("Management directories prepared")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-idle"]:
        check_idle()
    elif sys.argv[1:] == ["--prepare-directories"]:
        prepare_directories()
    else:
        raise SystemExit("Expected --check-idle or --prepare-directories")
