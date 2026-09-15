"""Public deployment progress derived from durable state, without subprocess logs or secrets."""

import json
import time
from pathlib import Path

PHASES = {
    "queued": "Waiting for the deployment process",
    "preflight": "Checking deployment prerequisites and Agent connectivity",
    "dependencies": "Preparing the server and installing dependencies",
    "draining": "Entering maintenance and waiting for running jobs",
    "stopping": "Stopping production services",
    "backup": "Backing up databases and platform state",
    "restore": "Restoring the selected backup",
    "migration": "Migrating database schemas",
    "restart": "Switching release and restarting services",
    "health": "Waiting for server health checks",
    "agents": "Updating remote Agents",
    "heartbeats": "Verifying Agent versions and heartbeats",
    "reopening": "Leaving maintenance mode",
    "complete": "Deployment completed successfully",
    "server-only": "Server installed; Agent verification is still required",
}


def _read(path):
    try:
        result = json.loads(Path(path).read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def production_progress(root: Path, unit_state: str):
    request = _read(root / "promotion-request.json")
    state = _read(root / "deployment.json")
    if not request:
        return None
    started = request.get("started_at", 0)
    matching = state.get("release") == request.get("release") and state.get("started_at", 0) >= started
    active = unit_state in {"activating", "active", "deactivating"}
    if not matching:
        state = {"phase": "queued", "status": "running"}
    status = state.get("status", "running")
    if request.get("status") == "failed" or unit_state == "failed" or (
        not active
        and status not in {"installed", "failed", "server-installed"}
        and time.time() - started > 15
    ):
        status = "failed"
    phase = state.get("phase", "queued")
    message = PHASES.get(phase, "Deployment is running")
    if status == "installed" and active:
        status = "running"
        message = "Finalizing deployment"
    if status == "failed":
        message = f"Deployment failed or stopped during: {message}. Check deployment logs before retrying."
    return {
        "id": request.get("id"),
        "release": request.get("release"),
        "status": status,
        "phase": phase,
        "message": message,
        "started_at": started,
        "updated_at": state.get("updated_at", started),
        "finished_at": state.get("finished_at") or request.get("finished_at"),
        "steps": [
            {"phase": step["phase"], "message": PHASES[step["phase"]], "at": step.get("at")}
            for step in state.get("steps", [])
            if isinstance(step, dict) and step.get("phase") in PHASES
        ],
    }
