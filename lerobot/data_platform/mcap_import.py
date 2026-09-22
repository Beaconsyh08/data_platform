"""Platform side of the GenDAS import protocol and isolated worker execution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from lerobot.data_platform.mcap_contract import PROTOCOL, McapImportConfig


def runtime_python() -> Path:
    return Path(
        os.environ.get("DATA_PLATFORM_MCAP_PYTHON") or Path(sys.prefix).parent / "mcap-runtime/bin/python"
    )


@lru_cache(maxsize=8)
def _runtime_status(python: str, modified: int, worker_modified: int) -> dict:
    del modified, worker_modified
    try:
        result = subprocess.run(
            [python, "-s", str(Path(__file__).with_name("mcap_worker.py")), "--check"],
            env=worker_environment(),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            return {"ready": False, "reason": "MCAP runtime self-check failed; run mcap_runtime installer"}
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ready": False, "reason": "MCAP runtime is unavailable"}


def worker_environment() -> dict:
    return {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}


def runtime_status() -> dict:
    python = runtime_python()
    if not python.is_file():
        return {"ready": False, "reason": "Install the GenDAS MCAP runtime on this Agent"}
    return _runtime_status(
        str(python), python.stat().st_mtime_ns, Path(__file__).with_name("mcap_worker.py").stat().st_mtime_ns
    )


def raw_location(root: Path, node_name: str, files: list[Path]) -> dict:
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return {
        "dataset_key": f"node-{node_name}/mcap-{root.name}-{digest}",
        "root": str(root),
        "output_dir": "",
        "metadata": {
            "source_format": "mcap",
            "adapter": "genrobot_das",
            "mcap_count": len(files),
            "source_bytes": sum(path.stat().st_size for path in files),
            "task": root.parent.name.replace("_", " "),
        },
    }


def execute_import(agent, job, source: Path) -> dict:
    from lerobot.data_platform.agent import _dataset_payload, _require_safe_output
    from lerobot.data_platform.execution import StopRequestedError, atomic_json, input_fingerprint

    if (job.get("location", {}).get("metadata") or {}).get("source_format") != "mcap":
        raise ValueError("MCAP imports require a registered raw MCAP source")
    status = runtime_status()
    if not status.get("ready"):
        raise RuntimeError(status["reason"])
    options = job.get("options") or {}
    config = McapImportConfig.from_dict(options.get("config") or {})
    fingerprint = input_fingerprint([source])
    if options.get("input_fingerprint") not in (None, fingerprint):
        raise ValueError("MCAP source changed after inspection; inspect again")
    convert = job["operation"] == "import.mcap.convert"
    attempt = (job.get("execution") or {}).get("attempt_id") or job["job_id"]
    work = agent.state_path.parent / "executions" / attempt / "mcap"
    work.mkdir(parents=True, exist_ok=True)
    output = None
    checkpoints = work / "checkpoints"
    if convert:
        output = _require_safe_output(Path(options["out_root"]), source, agent.writable_roots)
        final = Path((job.get("execution") or {}).get("final_output") or output)
        final = _require_safe_output(final, source, agent.writable_roots)
        checkpoints = final.parent / ".mcap-checkpoints" / job["job_id"]
    request = {
        "operation": "convert" if convert else "inspect",
        "source": str(source),
        "output": str(output) if output else None,
        "config": config.to_dict(),
        "input_fingerprint": fingerprint,
        "checkpoint_root": str(checkpoints),
        "result": str(work / "report.json"),
    }
    atomic_json(work / "request.json", request)
    started = time.monotonic()
    with (work / "worker.log").open("w") as error_log:
        process = subprocess.Popen(
            [
                str(runtime_python()),
                "-s",
                str(Path(__file__).with_name("mcap_worker.py")),
                str(work / "request.json"),
            ],
            stdout=subprocess.PIPE,
            stderr=error_log,
            text=True,
            cwd=work,
            env=worker_environment(),
        )
        last_error = "MCAP conversion worker failed; see the Agent execution log"
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or "phase" not in event:
                    continue
                last_error = event.get("error") or last_error
                event["elapsed_s"] = round(time.monotonic() - started, 1)
                agent.client.event(agent.state, job["job_id"], f"MCAP: {event['phase']}", event)
            if process.wait():
                stop = os.environ.get("DATA_PLATFORM_EXECUTION_STOP_FILE")
                if stop and Path(stop).exists():
                    raise StopRequestedError("Import cancelled")
                raise RuntimeError(last_error)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    report = json.loads((work / "report.json").read_text())
    if input_fingerprint([source]) != fingerprint:
        raise ValueError("MCAP source changed during import")
    result = {"mcap_import": report, "mcap_import_protocol": PROTOCOL}
    if output:
        from lerobot.data_platform.precompute.preprocess.dataset_version import validate_v3_dataset

        validate_v3_dataset(output)
        result["dataset_location"] = _dataset_payload(
            output,
            node_name=agent.name,
            metadata={
                "stage": "standard",
                "source_format": "lerobot",
                "import_source_location_id": job["location_id"],
            },
        )
    return result
