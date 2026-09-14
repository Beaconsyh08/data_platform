#!/usr/bin/env bash
set -euo pipefail

BUNDLE_VERSION="@BUNDLE_VERSION@"
BUNDLE_PLATFORM="@BUNDLE_PLATFORM@"
BUNDLE_ARCH="@BUNDLE_ARCH@"
WHEEL_NAME="@WHEEL_NAME@"

INSTALL_ROOT="/opt/data-platform-agent"
CONFIG_FILE="/etc/data-platform/agent.env"
STATE_DIR="/var/lib/data-platform-agent"
CACHE_DIR="/var/cache/data-platform-agent"
SERVICE_FILE="/etc/systemd/system/data-platform-agent.service"
SERVICE_USER="data-platform-agent"

BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ADOPT_LEGACY=0
DEPLOY_ENV=""
INSTANCE_ID=""
VERIFY_ONLY=0
NO_START=0
SERVER_URL=""
NODE_NAME=$(hostname -s)
ENROLLMENT_TOKEN_FILE=""
ALLOWED_ROOTS=()
WRITABLE_ROOTS=()

usage() {
    cat <<'EOF'
Install the Data Platform agent without a source checkout.

Usage:
  sudo ./install.sh [options]

Options:
  --env dev|prod             Required installation environment.
  --adopt-legacy             Adopt the existing production node during maintenance.
  --instance-id UUID          Environment identity for first enrollment.
  --server-url URL            Central Data Platform HTTPS URL.
  --name NAME                 Stable node name (default: short hostname).
  --allowed-root PATH         Readable dataset parent; repeat for multiple roots.
  --writable-root PATH        Writable output parent; repeat for multiple roots.
  --enrollment-token-file FILE
                              Read the first-enrollment token from FILE.
  --no-start                  Install files but do not start the systemd service.
  --verify-only               Verify bundle files and exit; root is not required.
  -h, --help                  Show this help.

If an agent configuration already exists, it is preserved during upgrades.
EOF
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --adopt-legacy) ADOPT_LEGACY=1; shift ;;
        --env) DEPLOY_ENV=${2:?Missing environment}; shift 2 ;;
        --instance-id) INSTANCE_ID=${2:?Missing instance UUID}; shift 2 ;;
        --server-url)
            (($# >= 2)) || fail "--server-url requires a value"
            SERVER_URL=$2
            shift 2
            ;;
        --name)
            (($# >= 2)) || fail "--name requires a value"
            NODE_NAME=$2
            shift 2
            ;;
        --allowed-root)
            (($# >= 2)) || fail "--allowed-root requires a value"
            ALLOWED_ROOTS+=("$2")
            shift 2
            ;;
        --writable-root)
            (($# >= 2)) || fail "--writable-root requires a value"
            WRITABLE_ROOTS+=("$2")
            shift 2
            ;;
        --enrollment-token-file)
            (($# >= 2)) || fail "--enrollment-token-file requires a value"
            ENROLLMENT_TOKEN_FILE=$2
            shift 2
            ;;
        --no-start)
            NO_START=1
            shift
            ;;
        --verify-only)
            VERIFY_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

cd "$BUNDLE_DIR"
sha256sum --check manifest.sha256
[[ -x bin/uv ]] || fail "bundled uv executable is missing"
[[ -f "wheels/$WHEEL_NAME" ]] || fail "agent wheel is missing"

if ((VERIFY_ONLY)); then
    echo "Bundle ${BUNDLE_VERSION} (${BUNDLE_PLATFORM}-${BUNDLE_ARCH}) verified."
    exit 0
fi

[[ $(uname -s | tr '[:upper:]' '[:lower:]') == "$BUNDLE_PLATFORM" ]] ||
    fail "this bundle is for $BUNDLE_PLATFORM"
[[ $(uname -m) == "$BUNDLE_ARCH" ]] || fail "this bundle is for architecture $BUNDLE_ARCH"
[[ $EUID -eq 0 ]] || fail "run the installer with sudo"
command -v systemctl >/dev/null || fail "systemd is required"
command -v runuser >/dev/null || fail "runuser is required"
if [[ -x /usr/bin/python3.10 ]]; then
    PYTHON_BIN=/usr/bin/python3.10
else
    PYTHON_BIN=$(command -v python3 || true)
fi
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || fail "Python 3.10 or newer is required"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

[[ $DEPLOY_ENV == dev || $DEPLOY_ENV == prod ]] || fail "--env dev|prod is required"
INSTALL_ROOT="/opt/data-platform-agent/$DEPLOY_ENV"
CONFIG_FILE="/etc/data-platform/$DEPLOY_ENV/agent.env"
STATE_DIR="/var/lib/data-platform-agent-$DEPLOY_ENV"
CACHE_DIR="/var/cache/data-platform-agent/$DEPLOY_ENV"
SERVICE_FILE="/etc/systemd/system/data-platform-agent@$DEPLOY_ENV.service"
SERVICE_USER="data-platform-agent-$DEPLOY_ENV"
if [[ -f "$CONFIG_FILE" ]]; then
    existing_env=$(sed -n 's/^DATA_PLATFORM_ENV=//p' "$CONFIG_FILE" | tr -d '\"')
    [[ $existing_env == "$DEPLOY_ENV" ]] || fail "Agent configuration belongs to another environment"
else
    [[ $INSTANCE_ID =~ ^[0-9a-fA-F-]{36}$ ]] || fail "--instance-id UUID is required for first install"
fi
install -d -o root -g root -m 0755 "/etc/data-platform/$DEPLOY_ENV"
if ! getent passwd "$SERVICE_USER" >/dev/null; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -c "pass" >/dev/null 2>&1 ||
    fail "Python must be executable by $SERVICE_USER: $PYTHON_BIN"

install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases" "$CACHE_DIR"
install -d -o root -g root -m 0755 /etc/data-platform
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR"

RELEASE_DIR="$INSTALL_ROOT/releases/$BUNDLE_VERSION"
BUNDLE_DIGEST=$(sha256sum manifest.sha256 | cut -d ' ' -f 1)
if [[ -e "$RELEASE_DIR" ]]; then
    [[ -f "$RELEASE_DIR/bundle.sha256" && $(cat "$RELEASE_DIR/bundle.sha256") == "$BUNDLE_DIGEST" ]] || fail "release already exists with different or incomplete contents"
fi
install -d -o root -g root -m 0755 "$RELEASE_DIR"

cleanup_release() {
    if [[ -n ${RELEASE_DIR:-} && ! -x "$RELEASE_DIR/.venv/bin/data-platform-agent" ]]; then
        rm -rf -- "$RELEASE_DIR"
    fi
}
trap cleanup_release EXIT

if [[ ! -f "$RELEASE_DIR/bundle.sha256" ]]; then
install -m 0755 bin/uv "$RELEASE_DIR/uv"
install -m 0644 requirements.lock "$RELEASE_DIR/requirements.lock"
install -d -m 0755 "$RELEASE_DIR/wheels"
install -m 0644 "wheels/$WHEEL_NAME" "$RELEASE_DIR/wheels/$WHEEL_NAME"

UV_CACHE_DIR="$CACHE_DIR/uv" UV_NO_MANAGED_PYTHON=1 \
    "$RELEASE_DIR/uv" venv --python "$PYTHON_BIN" "$RELEASE_DIR/.venv"
UV_CACHE_DIR="$CACHE_DIR/uv" "$RELEASE_DIR/uv" pip install \
    --python "$RELEASE_DIR/.venv/bin/python" \
    --require-hashes \
    --requirements "$RELEASE_DIR/requirements.lock"
UV_CACHE_DIR="$CACHE_DIR/uv" "$RELEASE_DIR/uv" pip install \
    --python "$RELEASE_DIR/.venv/bin/python" \
    --no-deps \
    "$RELEASE_DIR/wheels/$WHEEL_NAME"

printf '%s\n' "$BUNDLE_DIGEST" > "$RELEASE_DIR/bundle.sha256"
fi
if ((ADOPT_LEGACY)); then
    [[ $DEPLOY_ENV == prod && -n $INSTANCE_ID ]] || fail "Legacy adoption requires --env prod --instance-id UUID"
    "$RELEASE_DIR/.venv/bin/python" - "$CONFIG_FILE" "$STATE_DIR" "$INSTANCE_ID" "$BUNDLE_VERSION" "$BUNDLE_DIGEST" <<'PYADOPT'
import sys
from pathlib import Path
from lerobot.data_platform.agent_install import adopt_legacy
adopt_legacy(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
PYADOPT
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ -z "$SERVER_URL" ]]; then
        read -r -p "Central platform URL (https://...): " SERVER_URL
    fi
    if ((${#ALLOWED_ROOTS[@]} == 0)); then
        read -r -p "Readable dataset parent: " root
        ALLOWED_ROOTS+=("$root")
    fi
    if ((${#WRITABLE_ROOTS[@]} == 0)); then
        read -r -p "Writable output parent: " root
        WRITABLE_ROOTS+=("$root")
    fi
    if [[ -n "$ENROLLMENT_TOKEN_FILE" ]]; then
        [[ -r "$ENROLLMENT_TOKEN_FILE" ]] || fail "cannot read enrollment token file"
        IFS= read -r ENROLLMENT_TOKEN < "$ENROLLMENT_TOKEN_FILE"
    else
        read -r -s -p "First-enrollment token: " ENROLLMENT_TOKEN
        echo
    fi

    [[ $SERVER_URL == https://* ]] || fail "central platform URL must use HTTPS"
    [[ $NODE_NAME =~ ^[A-Za-z0-9._-]+$ ]] || fail "node name contains unsupported characters"
    [[ -n "$ENROLLMENT_TOKEN" ]] || fail "enrollment token is empty"

    for root in "${ALLOWED_ROOTS[@]}" "${WRITABLE_ROOTS[@]}"; do
        [[ $root != *:* ]] || fail "dataset paths may not contain ':'"
        [[ -d $root ]] || fail "directory does not exist: $root"
    done

    ALLOWED_VALUE=$(IFS=:; echo "${ALLOWED_ROOTS[*]}")
    WRITABLE_VALUE=$(IFS=:; echo "${WRITABLE_ROOTS[*]}")
    CONFIG_TEMP=$(mktemp "$CONFIG_FILE.XXXXXX")
    chmod 0640 "$CONFIG_TEMP"
    chown root:"$SERVICE_USER" "$CONFIG_TEMP"
    {
        printf 'DATA_PLATFORM_ENV=%s\n' "$DEPLOY_ENV"
        printf 'DATA_PLATFORM_INSTANCE_ID=%s\n' "$INSTANCE_ID"
        printf 'DATA_PLATFORM_STATE_ROOT=/srv/data-platform-dev\n'
        printf 'DATA_PLATFORM_SERVER_URL=%q\n' "$SERVER_URL"
        printf 'DATA_PLATFORM_AGENT_NAME=%q\n' "$NODE_NAME"
        printf 'DATA_PLATFORM_AGENT_ALLOWED_ROOTS=%q\n' "$ALLOWED_VALUE"
        printf 'DATA_PLATFORM_AGENT_WRITABLE_ROOTS=%q\n' "$WRITABLE_VALUE"
        printf 'DATA_PLATFORM_AGENT_ALLOW_SOURCE_MUTATIONS=0\n'
        printf 'DATA_PLATFORM_AGENT_STATE=%q\n' "$STATE_DIR/agent.json"
        printf 'DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN=%q\n' "$ENROLLMENT_TOKEN"
    } > "$CONFIG_TEMP"
    mv "$CONFIG_TEMP" "$CONFIG_FILE"
fi

# Verify task-path dependencies before switching the live release.
"$RELEASE_DIR/.venv/bin/python" -c \
    "from lerobot.data_platform.execution import SpoolingClient; from lerobot.data_platform.management_storage import EventSpool"
# Bind the state directory before starting, and keep credentials out of command arguments.
"$RELEASE_DIR/.venv/bin/python" - "$CONFIG_FILE" "$STATE_DIR" "$DEPLOY_ENV" "$BUNDLE_VERSION" "$BUNDLE_DIGEST" <<'PYENV'
import os, sys
from pathlib import Path
from lerobot.data_platform.deployment import read_environment_file
from lerobot.data_platform.environment import EnvironmentIdentity, verify_directory
values = read_environment_file(Path(sys.argv[1]))
if values.get("DATA_PLATFORM_ENV") != sys.argv[3]:
    raise SystemExit("Agent environment mismatch")
os.environ.update(values)
identity = EnvironmentIdentity.from_env()
from lerobot.data_platform.agent import AgentClient
client = AgentClient(values["DATA_PLATFORM_SERVER_URL"])
health = client._request("GET", "/healthz")
if (health.get("release"), health.get("agent_manifest_sha256")) != (sys.argv[4], sys.argv[5]):
    raise SystemExit("Agent bundle does not match the installed server release")
if (health.get("environment"), health.get("instance_id")) != (identity.name, identity.instance_id):
    raise SystemExit("Agent server identity mismatch")
if (Path(sys.argv[2]) / "agent.json").exists() and (not health.get("maintenance") or health.get("active_jobs") != 0):
    raise SystemExit("Pause the selected environment before upgrading its Agent")
verify_directory(Path(sys.argv[2]), "agent", initialize=True)
PYENV
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR/.data-platform-environment.json"
printf 'DATA_PLATFORM_RELEASE=%s\n' "$BUNDLE_VERSION" > "$RELEASE_DIR/release.env"
# Render a separate unit; never overwrite the other environment's current pointer.
sed -e "s|/opt/data-platform-agent/current|$INSTALL_ROOT/current|g" \
    -e "s|/etc/data-platform/agent.env|$CONFIG_FILE|g" \
    -e "s|User=data-platform-agent$|User=$SERVICE_USER|" \
    -e "s|Group=data-platform-agent$|Group=$SERVICE_USER|" \
    -e "/EnvironmentFile=/a EnvironmentFile=$INSTALL_ROOT/current/release.env" \
    systemd/data-platform-agent.service > "$SERVICE_FILE"
if [[ $DEPLOY_ENV == dev ]]; then
    sed -i '/\[Service\]/a CPUQuota=200%\nMemoryMax=4G\nCPUWeight=10\nIOWeight=10\nEnvironment=DATA_PLATFORM_GPU_DEVICES=\nProtectSystem=strict\nReadWritePaths=/srv/data-platform-dev /var/lib/data-platform-agent-dev\nInaccessiblePaths=-/etc/data-platform/prod -/var/lib/data-platform-agent-prod -/var/lib/data-platform-agent' "$SERVICE_FILE"
fi
ln -sfnT "$RELEASE_DIR" "$INSTALL_ROOT/.current-next"
mv -Tf "$INSTALL_ROOT/.current-next" "$INSTALL_ROOT/current"
systemctl daemon-reload

if ((NO_START)); then
    echo "Installed Data Platform agent $BUNDLE_VERSION; service was not started."
    exit 0
fi

systemctl enable "data-platform-agent@$DEPLOY_ENV.service"
systemctl restart "data-platform-agent@$DEPLOY_ENV.service"

if grep -q '^DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN=' "$CONFIG_FILE"; then
    for _ in {1..30}; do
        [[ -s "$STATE_DIR/agent.json" ]] && break
        sleep 1
    done
    if [[ -s "$STATE_DIR/agent.json" ]]; then
        CONFIG_TEMP=$(mktemp "$CONFIG_FILE.XXXXXX")
        grep -v '^DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN=' "$CONFIG_FILE" > "$CONFIG_TEMP"
        chmod 0640 "$CONFIG_TEMP"
        chown root:"$SERVICE_USER" "$CONFIG_TEMP"
        mv "$CONFIG_TEMP" "$CONFIG_FILE"
        systemctl restart "data-platform-agent@$DEPLOY_ENV.service"
        echo "Agent enrolled; the one-time enrollment token was removed from $CONFIG_FILE."
    else
        echo "Agent did not enroll within 30 seconds; token retained for retry." >&2
        echo "Inspect logs with: journalctl -u data-platform-agent -n 100 --no-pager" >&2
        exit 1
    fi
fi

systemctl --no-pager --full status "data-platform-agent@$DEPLOY_ENV.service"
