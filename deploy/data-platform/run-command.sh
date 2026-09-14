#!/usr/bin/env bash
set -Eeuo pipefail
MODULE=${1:?Missing module}
shift
ARGS=("$@")
ENVIRONMENT=""
for ((i=0; i<${#ARGS[@]}; i++)); do
    if [[ ${ARGS[$i]} == --env ]]; then ENVIRONMENT=${ARGS[$((i+1))]:-}; fi
done
if [[ $ENVIRONMENT == both ]]; then
    [[ $MODULE == lerobot.data_platform.release_cli && ${ARGS[0]:-} == deploy && " ${ARGS[*]} " == *" --hard "* ]] || {
        echo "Combined upgrade requires deploy --env both --hard" >&2; exit 2;
    }
elif [[ $ENVIRONMENT != dev && $ENVIRONMENT != prod ]]; then
    echo "Explicit --env dev|prod is required; combined upgrade uses --env both --hard" >&2; exit 2
fi
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CHECKOUT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
if [[ -f "$CHECKOUT/lerobot/data_platform/release_cli.py" && -x "$CHECKOUT/.venv/bin/python" ]]; then
    TOOL_ROOT=$CHECKOUT
else
    TOOL_ENVIRONMENT=$ENVIRONMENT
    if [[ $TOOL_ENVIRONMENT == both ]]; then TOOL_ENVIRONMENT=dev; fi
    TOOL_ROOT="/opt/data-platform/$TOOL_ENVIRONMENT/current"
fi
[[ -x "$TOOL_ROOT/.venv/bin/python" ]] || { echo "Bootstrap from the source checkout first" >&2; exit 1; }
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$SCRIPT_DIR/run-command.sh" "$MODULE" "$@"
fi
cd -- "$TOOL_ROOT"
exec "$TOOL_ROOT/.venv/bin/python" -m "$MODULE" "$@"
