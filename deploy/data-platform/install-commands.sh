#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 0755 /usr/local/lib/data-platform
for script in run-command.sh update-server.sh update-all.sh restart-server.sh release.sh environment.sh configure-dynamic-ip.sh; do
    install -m 0755 "$SCRIPT_DIR/$script" "/usr/local/lib/data-platform/$script"
done
ln -sfn /usr/local/lib/data-platform/update-server.sh /usr/local/bin/data-platform-update
ln -sfn /usr/local/lib/data-platform/update-all.sh /usr/local/bin/data-platform-update-all
ln -sfn /usr/local/lib/data-platform/restart-server.sh /usr/local/bin/data-platform-restart
ln -sfn /usr/local/lib/data-platform/release.sh /usr/local/bin/data-platform-release
ln -sfn /usr/local/lib/data-platform/environment.sh /usr/local/bin/data-platform-environment
install -m 0644 "$SCRIPT_DIR/data-platform-promotion.service" /etc/systemd/system/data-platform-promotion.service
systemctl daemon-reload
if [[ -x /opt/data-platform/dev/current/.venv/bin/python ]]; then
    systemctl enable --now data-platform-promotion.service
    systemctl restart data-platform-promotion.service
fi
