"""Generate isolated same-IP HTTPS frontends with environment cookie allowlists."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from lerobot.data_platform.deployment import Deployment
from lerobot.data_platform.releases import run


def nginx_config(deployment: Deployment) -> str:
    env = deployment.environment
    redirect = (
        """server {
    listen 80;
    server_name _;
    allow 127.0.0.1;
    allow 10.8.0.0/16;
    deny all;
    return 308 https://$host$request_uri;
}
"""
        if env == "prod"
        else ""
    )
    return (
        redirect
        + f"""# Generated for {env}; loaded in the nginx http context.
map $cookie_data_platform_session_{env} $dp_{env}_cookie {{
    default "data_platform_session_{env}=$cookie_data_platform_session_{env}";
    "" "";
}}
server {{
    listen {deployment.public_port} ssl;
    server_name _;
    ssl_certificate /etc/nginx/tls/data-platform.crt;
    ssl_certificate_key /etc/nginx/tls/data-platform.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    allow 127.0.0.1;
    allow 10.8.0.0/16;
    deny all;
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Cookie $dp_{env}_cookie;
    location /api/agents/ {{
        proxy_pass http://127.0.0.1:{deployment.port};
    }}
    location = /healthz {{
        proxy_pass http://127.0.0.1:{deployment.port};
    }}
    location / {{
        if (-f /var/lib/data-platform-deployment/{env}-maintenance) {{ return 503; }}
        proxy_pass http://127.0.0.1:{deployment.port};
    }}
}}
"""
    )


def configure_network(deployment: Deployment, *, adopt_legacy=False):
    name = f"data-platform-{deployment.environment}"
    target = Path("/etc/nginx/sites-available") / name
    enabled = Path("/etc/nginx/sites-enabled") / name
    legacy = Path("/etc/nginx/sites-enabled/data-platform-internal")
    old_target = target.read_bytes() if target.exists() else None
    old_link = os.readlink(legacy) if legacy.is_symlink() else None
    if deployment.environment == "prod" and legacy.exists() and not adopt_legacy:
        raise RuntimeError("Use --adopt-legacy during the first production maintenance window")
    try:
        target.write_text(nginx_config(deployment))
        if not enabled.exists():
            enabled.symlink_to(target)
        if deployment.environment == "prod" and old_link:
            legacy.unlink()
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
    except BaseException:
        if old_target is None:
            enabled.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(old_target)
        if old_link and not legacy.exists():
            legacy.symlink_to(old_link)
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        raise
    if deployment.environment == "dev":
        legacy_tunnel = Path("/etc/systemd/system/data-platform-h100-tunnel.service")
        if legacy_tunnel.exists():
            source = legacy_tunnel.read_text()
            old_forward = "127.0.0.1:9443:127.0.0.1:443"
            if source.count(old_forward) != 1:
                raise RuntimeError(
                    "Existing tunnel is not the expected loopback-only forward; configure the dev tunnel explicitly"
                )
            unit = Path("/etc/systemd/system/data-platform-h100-tunnel@dev.service")
            if unit.exists():
                shutil.copy2(unit, unit.with_suffix(".service.bak"))
            unit.write_text(source.replace(old_forward, "127.0.0.1:9444:127.0.0.1:8443"))
            run(["systemctl", "daemon-reload"])
            run(["systemctl", "enable", "--now", unit.name])
