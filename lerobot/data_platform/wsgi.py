"""Gunicorn application factory for the central Data Platform service."""

from __future__ import annotations

import os
from pathlib import Path

from werkzeug.middleware.proxy_fix import ProxyFix

from lerobot.data_platform.viewer import CONSOLE_MODE_FULL, run_server


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app():
    from lerobot.data_platform.environment import check_server_environment

    check_server_environment()
    datasets_root_value = os.environ.get("DATA_PLATFORM_ROOT", "").strip()
    datasets_root = Path(datasets_root_value).expanduser() if datasets_root_value else None
    output_value = os.environ.get("DATA_PLATFORM_OUTPUT_DIR", "").strip()
    if output_value:
        output_dir = Path(output_value).expanduser()
    elif datasets_root is not None:
        output_dir = datasets_root / "vis" / "_console"
    else:
        raise RuntimeError("DATA_PLATFORM_ROOT or DATA_PLATFORM_OUTPUT_DIR is required")
    static_dir = output_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    template_dir = Path(__file__).resolve().parent / "templates"
    protected_roots = [
        Path(value).expanduser()
        for value in os.environ.get("DATA_PLATFORM_PROTECTED_SOURCE_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    remote_cache_value = os.environ.get("DATA_PLATFORM_REMOTE_CACHE_ROOT", "").strip()
    remote_cache_root = Path(remote_cache_value).expanduser() if remote_cache_value else None
    app = run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static_dir,
        template_folder=template_dir,
        annotate=False,
        datasets_root=datasets_root,
        data_version=None,
        console_mode=os.environ.get("DATA_PLATFORM_CONSOLE_MODE", CONSOLE_MODE_FULL),
        legacy_mutations_enabled=_env_bool("DATA_PLATFORM_ENABLE_LEGACY_MUTATIONS"),
        protected_source_roots=protected_roots,
        database_url=os.environ.get("DATA_PLATFORM_DATABASE_URL") or None,
        allow_registration=_env_bool("DATA_PLATFORM_ALLOW_REGISTRATION"),
        remote_cache_root=remote_cache_root,
        start_server=False,
    )
    if _env_bool("DATA_PLATFORM_TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    return app
