#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
exec bash "$SCRIPT_DIR/run-command.sh" lerobot.data_platform.release_cli deploy --server-only --source "$PWD" "$@"
