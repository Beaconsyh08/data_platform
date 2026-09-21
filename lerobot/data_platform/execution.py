"""Durable Agent execution supervision, process isolation and result reconciliation."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


def serialize_completion(root):
    """Serialize filesystem finalization across web threads and processes."""
    import functools

    def decorate(func):
        @functools.wraps(func)
        def wrapped(job_id, *args, **kwargs):
            directory = Path(root) / ".completion-locks"
            directory.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(str(job_id).encode()).hexdigest()
            with (directory / key).open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                return func(job_id, *args, **kwargs)

        return wrapped

    return decorate


class StopRequestedError(Exception):
    pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def input_fingerprint(roots):
    """Fingerprint metadata content and resolved data/video file identity and timestamps."""
    digest = hashlib.sha256()
    for root in sorted({Path(value).resolve() for value in roots}):
        digest.update(str(root).encode())
        for directory in ("meta", "data", "videos"):
            for path in sorted((root / directory).rglob("*")):
                if not path.is_file():
                    continue
                stat = path.stat()
                digest.update(str(path.relative_to(root)).encode())
                digest.update(str((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)).encode())
                if directory == "meta":
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
    return digest.hexdigest()


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_members(pgid):
    members = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[2]) == pgid:
                members.append(int(path.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return members


class ComputeGroup:
    """Use a delegated cgroup v2 subtree; development can explicitly allow process groups."""

    def __init__(self, attempt_id):
        self.path = None
        root_value = os.environ.get("DATA_PLATFORM_CGROUP_ROOT")
        if root_value:
            root = Path(root_value)
        else:
            entries = Path("/proc/self/cgroup").read_text().splitlines()
            relative = next((line[3:] for line in entries if line.startswith("0::")), None)
            root = Path("/sys/fs/cgroup") / relative.lstrip("/") if relative else None
        try:
            if root is None:
                raise OSError("cgroup v2 unavailable")
            if root.name == "supervisor":
                root = root.parent
            supervisor = root / "supervisor"
            supervisor.mkdir(exist_ok=True)
            (supervisor / "cgroup.procs").write_text(str(os.getpid()))
            (root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
            target = root / f"job-{attempt_id}"
            target.mkdir(exist_ok=True)
            cpus = max(
                0.1,
                (os.cpu_count() or 1) * float(os.environ.get("DATA_PLATFORM_COMPUTE_CPU_FRACTION", "0.5")),
            )
            (target / "cpu.max").write_text(f"{int(cpus * 100000)} 100000")
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            (target / "memory.max").write_text(
                str(int(memory * float(os.environ.get("DATA_PLATFORM_COMPUTE_MEMORY_FRACTION", "0.6"))))
            )
            (target / "memory.oom.group").write_text("1")
            self.path = target
        except OSError:
            if os.environ.get("DATA_PLATFORM_REQUIRE_CGROUP", "0") == "1":
                raise RuntimeError("Delegated cgroup v2 is required for compute execution") from None

    def pids(self):
        return [int(value) for value in (self.path / "cgroup.procs").read_text().split()] if self.path else []


def request_stop(marker, mode):
    atomic_json(Path(marker["work"]) / "stop.json", {"mode": mode})
    if mode == "force":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(marker["pid"], signal.SIGTERM)


def replace_paths(value, replacements):
    if isinstance(value, dict):
        return {key: replace_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            if value == old or value.startswith(old + "/"):
                return new + value[len(old) :]
    return value


def publish_viewer_cache(cache: Path, target: Path, source: Path) -> None:
    """Promote one attempt's cache, retaining the previous cache and supporting replay."""
    cache, target, source = Path(cache), Path(target), Path(source).resolve()
    if cache.is_symlink() or target.is_symlink():
        raise ValueError("Viewer cache publication does not accept symbolic links")
    if (
        target.resolve() == source
        or target.resolve().is_relative_to(source)
        or source.is_relative_to(target.resolve())
    ):
        raise ValueError("Viewer cache must be separate from the source dataset")
    if cache.resolve() == target.resolve() or cache.resolve().is_relative_to(target.resolve()):
        raise ValueError("Viewer cache staging must be separate from the publication target")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = target.parent / ".publication-locks"
    lock_dir.mkdir(exist_ok=True)
    identity = {"staging": str(cache), "target": str(target), "source": str(source)}
    receipt_name = ".viewer-publication.json"

    def validate(directory):
        manifest = json.loads((directory / "static" / "viewer_manifest.json").read_text())
        if Path(manifest["root"]).resolve() != source:
            raise ValueError("Viewer cache manifest does not match the source dataset")

    with (lock_dir / hashlib.sha256(target.name.encode()).hexdigest()).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not cache.exists():
            receipt = target / receipt_name
            if not receipt.is_file() or json.loads(receipt.read_text()) != identity:
                raise FileNotFoundError("Execution produced no viewer cache owned by this attempt")
            validate(target)
            return
        validate(cache)
        backup = cache.parent / "previous-cache"
        if target.exists():
            validate(target)
            if backup.exists():
                raise FileExistsError("Previous viewer cache backup already exists")
            from lerobot.data_platform.dataset_results import preserve_results

            preserve_results(target, cache)
        atomic_json(cache / receipt_name, identity)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(cache, target)
        except OSError:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise


class SpoolingClient:
    """Compute spools control-plane telemetry/artifacts; provider clients may make inference calls."""

    def __init__(self, server_url, work, *, server_identity=None):
        self.server_url, self.work = server_url, Path(work)
        self.sequence = 0
        self.server_identity = server_identity

    def _request(self, method, path):
        # The parent has verified the server; workers inherit that identity without network access.
        if method != "GET" or path != "/healthz" or not self.server_identity:
            raise RuntimeError("Offline worker requires the supervisor's verified server identity")
        return dict(self.server_identity)

    def check_stop(self):
        if (self.work / "stop.json").exists():
            raise StopRequestedError("Stop requested")

    def event(self, state, job_id, message, payload=None):
        self.check_stop()
        self.sequence += 1
        from lerobot.data_platform.management_storage import EventSpool
        from lerobot.data_platform.operation_log import sanitize_for_log

        spool = EventSpool(
            self.work / "events",
            max_bytes=int(os.environ.get("DATA_PLATFORM_AGENT_EVENT_SPOOL_BYTES", str(256 * 1024 * 1024))),
        )
        spool.write(
            sanitize_for_log(
                {
                    "event_id": f"{self.work.name}:{self.sequence}",
                    "message": str(message),
                    "payload": payload or {},
                }
            )
        )

    def upload_artifact(
        self, state, job_id, relative_path, path, *, derived=False, caption=False, curation=False
    ):
        self.check_stop()
        if caption or curation:
            retained = (
                self.work / ("curation-artifacts" if curation else "caption-artifacts") / Path(relative_path)
            )
            retained.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, retained)
            path = retained
        identity = f"{curation}:{caption}:{derived}:{relative_path}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        atomic_json(
            self.work / "uploads" / f"{key}.json",
            {
                "relative_path": str(relative_path),
                "path": str(path),
                "derived": derived,
                "caption": caption,
                "curation": curation,
            },
        )


def run_worker(config_path):
    config = json.loads(Path(config_path).read_text())
    work = Path(config["work"])
    # The parent persists the PID before allowing any data access.
    deadline = time.monotonic() + 30
    while not (work / "launch.json").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Supervisor did not confirm launch")
        time.sleep(0.05)
    if config.get("cgroup"):
        (Path(config["cgroup"]) / "cgroup.procs").write_text(str(os.getpid()))
    from lerobot.data_platform.agent import DataPlatformAgent, _json_value

    client = SpoolingClient(config["server_url"], work, server_identity=config.get("server_identity"))
    agent = DataPlatformAgent(
        client=client,
        state_path=Path(config["state_path"]),
        name=config["name"],
        allowed_roots=[Path(value) for value in config["allowed_roots"]],
        writable_roots=[Path(value) for value in config["writable_roots"]],
        enrollment_token="",
        allow_source_mutations=config["allow_source_mutations"],
    )
    try:
        client.check_stop()
        observed = input_fingerprint(config["input_roots"])
        if observed != config["fingerprint"]:
            raise ValueError("Input changed before execution")
        if config["job"]["operation"].startswith("local.request."):
            from lerobot.data_platform.local_execution import execute_local_request

            result = execute_local_request(config)
        else:
            result = agent.execute_job(config["job"])
        client.check_stop()
        if (
            not config["job"]["operation"].startswith(("mutation.", "local.request."))
            and input_fingerprint(config["input_roots"]) != observed
        ):
            raise ValueError("Input changed during execution")
        atomic_json(work / "result.json", {"status": "done", "result": _json_value(result)})
    except StopRequestedError:
        atomic_json(work / "result.json", {"status": "cancelled"})
    except BaseException as exc:
        from lerobot.data_platform.operation_log import sanitize_for_log

        atomic_json(work / "result.json", {"status": "error", "error": sanitize_for_log(str(exc))})


class ExecutionSupervisor:
    def __init__(self, agent):
        self.agent = agent
        self.root = agent.state_path.parent / "executions"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def flush_events(self, job, work):
        for path in sorted((work / "events").glob("*.json"))[:100]:
            payload = json.loads(path.read_text())
            self.agent.client.event(
                self.agent.state,
                job["job_id"],
                payload["message"],
                {**payload["payload"], "delivery_event_id": payload["event_id"]},
            )
            path.unlink(missing_ok=True)

    def reconcile(self):
        """Pending executions always take precedence over claiming more work."""
        for path in sorted(self.root.glob("*/marker.json")):
            marker = json.loads(path.read_text())
            if marker.get("acknowledged"):
                continue
            self.agent.client.activate(marker["job"])
            self.monitor(marker, None)
            return True
        return False

    def start(self, job):
        from lerobot.data_platform.agent import _require_safe_output, _require_within

        agent = self.agent
        execution = job["execution"]
        work = self.root / execution["attempt_id"]
        work.mkdir(mode=0o700)
        job_copy = copy.deepcopy(job)
        if job["operation"].startswith("curation."):
            from lerobot.data_platform.curation import artifact_path

            inputs = work / "curation-inputs"
            input_files = job["options"].get("input_files", {})
            for index, (name, expected) in enumerate(input_files.items()):
                agent.client.event(
                    agent.state,
                    job["job_id"],
                    "Preparing selected result inputs",
                    {"phase": "preparing", "current": index, "total": len(input_files)},
                )
                agent.client.download_curation_input(
                    agent.state, job, name, artifact_path(inputs, name), expected
                )
            job_copy["options"]["input_root"] = str(inputs)
        source = _require_within(Path(job["location"]["root"]), agent.allowed_roots, "dataset root")
        roots = [str(source)] + [item["root"] for item in job["options"].get("source_locations", [])]
        roots = [str(_require_within(Path(root), agent.allowed_roots, "input dataset")) for root in roots]
        fingerprint = input_fingerprint(roots)
        agent.client.checkpoint(agent.state, job["job_id"], "executing", fingerprint=fingerprint)
        final = Path(execution["final_output"]) if execution.get("final_output") else None
        if final:
            for root in roots:
                final = _require_safe_output(final, Path(root), agent.writable_roots)
            if final.exists():
                raise FileExistsError("Final output already exists; reconcile it before retrying")
            staging = final.parent / f".dp-{job['job_id']}-{execution['attempt_id']}"
        elif job["operation"] == "caption.annotate":
            staging = work / "staging"
        else:
            output = _require_within(
                Path(job["location"]["output_dir"]), agent.writable_roots, "viewer cache"
            )
            staging = output.parent / f".dp-{job['job_id']}-{execution['attempt_id']}"
        if staging.exists():
            raise FileExistsError("Execution staging already exists")
        staging.mkdir(mode=0o700, parents=True)
        if final:
            job_copy["options"]["out_root"] = str(staging / final.name)
            # Never let a retry overwrite output, including legacy overwrite options.
            job_copy["options"].pop("overwrite_output", None)
            if "overwrite" in job_copy["options"]:
                job_copy["options"]["overwrite"] = False
        job_copy["location"]["output_dir"] = str(staging / "source-cache" if final else staging / "cache")
        cpus = max(
            1,
            min(
                4,
                int(
                    (os.cpu_count() or 1) * float(os.environ.get("DATA_PLATFORM_COMPUTE_CPU_FRACTION", "0.5"))
                ),
            ),
        )
        for key in ("workers", "prepare_workers"):
            if key in job_copy["options"]:
                job_copy["options"][key] = min(cpus, max(1, int(job_copy["options"][key])))
        if job["operation"] == "viewer.prepare":
            job_copy["options"].setdefault("prepare_workers", cpus)
        elif job["operation"] in {
            "preprocess.standardize",
            "preprocess.smooth_action",
            "preprocess.convert_v3",
            "preprocess.merge",
        }:
            job_copy["options"].setdefault("workers", cpus)
        group = ComputeGroup(execution["attempt_id"])
        config = {
            "work": str(work),
            "job": job_copy,
            "state_path": str(agent.state_path),
            "name": agent.name,
            "server_url": agent.client.server_url,
            "server_identity": (
                agent.environment_identity.record("agent")
                if getattr(agent, "environment_identity", None)
                else None
            ),
            "allowed_roots": [str(path) for path in agent.allowed_roots],
            "writable_roots": [str(path) for path in agent.writable_roots],
            "allow_source_mutations": agent.allow_source_mutations,
            "fingerprint": fingerprint,
            "input_roots": roots,
            "cgroup": str(group.path) if group.path else None,
        }
        atomic_json(work / "config.json", config)
        lock = (self.root / ".node-lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = dict(
            os.environ, OMP_NUM_THREADS=str(cpus), OPENBLAS_NUM_THREADS=str(cpus), MKL_NUM_THREADS=str(cpus)
        )
        env["DATA_PLATFORM_EXECUTION_STOP_FILE"] = str(work / "stop.json")
        env["DATA_PLATFORM_EXECUTION_WORKERS"] = str(cpus)
        env["CUDA_VISIBLE_DEVICES"] = os.environ.get("DATA_PLATFORM_GPU_DEVICES", "")
        process = subprocess.Popen(
            [sys.executable, "-m", "lerobot.data_platform.execution", str(work / "config.json")],
            start_new_session=True,
            pass_fds=(lock.fileno(),),
            env=env,
        )
        marker = {
            "job": job,
            "work": str(work),
            "staging": str(staging),
            "final": str(final) if final else None,
            "pid": process.pid,
            "process_identity": process_identity(process.pid),
            "cgroup": config["cgroup"],
            "acknowledged": False,
            "publishing": False,
        }
        atomic_json(work / "marker.json", marker)
        atomic_json(work / "launch.json", {"pid": process.pid})
        try:
            self.monitor(marker, process)
        finally:
            lock.close()

    def monitor(self, marker, process):
        agent, job = self.agent, marker["job"]
        work = Path(marker["work"])
        force_at = None
        while True:
            if process:
                process.poll()
            alive = (
                process_identity(marker["pid"]) == marker["process_identity"]
                and marker["process_identity"] is not None
            )
            members = group_members(marker["pid"])
            if marker.get("cgroup") and Path(marker["cgroup"]).exists():
                members = list(
                    set(
                        members
                        + [int(pid) for pid in (Path(marker["cgroup"]) / "cgroup.procs").read_text().split()]
                    )
                )
            if not alive and not members:
                break
            try:
                heartbeat = agent.client.control_heartbeat(
                    agent.state, job["job_id"], lease_seconds=agent.lease_seconds
                )
                mode = heartbeat.get("stop_mode")
                if mode:
                    request_stop(marker, mode)
                    if mode == "force" and force_at is None:
                        force_at = time.monotonic()
                self.flush_events(job, work)
            except Exception:
                # Disconnected execution may compute locally, but can never publish without the handshake.
                pass
            if force_at is not None and time.monotonic() - force_at >= 30:
                for pid in members:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        if process:
            process.wait()
        if (work / "published.json").exists():
            payload = json.loads((work / "published.json").read_text())
        else:
            result_path = work / "result.json"
            payload = (
                json.loads(result_path.read_text())
                if result_path.exists()
                else {
                    "status": "cancelled" if (work / "stop.json").exists() else "error",
                    "error": "Compute process exited without a result",
                }
            )
            if (work / "stop.json").exists() and not marker["publishing"]:
                payload = {"status": "cancelled"}
            if payload["status"] == "done":
                agent.client.checkpoint(agent.state, job["job_id"], "finalizing")
                marker["publishing"] = True
                atomic_json(work / "marker.json", marker)
                payload = self.publish(marker, payload)
                atomic_json(work / "published.json", payload)
        if payload["status"] != "done" and Path(marker["staging"]).exists():
            shutil.rmtree(marker["staging"], ignore_errors=False)
        self.flush_events(job, work)
        if payload["status"] == "done":
            # Retry uploads and confirmation after a network failure, without executing again.
            for path in sorted(
                (work / "uploads").glob("*.json"),
                key=lambda value: (
                    json.loads(value.read_text())["relative_path"] == "viewer_manifest.json",
                    value.name,
                ),
            ):
                upload = json.loads(path.read_text())
                agent.client.upload_artifact(
                    agent.state,
                    job["job_id"],
                    Path(upload["relative_path"]),
                    Path(upload["path"]),
                    derived=upload["derived"],
                    **({"caption": True} if upload.get("caption") else {}),
                    **({"curation": True} if upload.get("curation") else {}),
                )
        agent.client.complete(
            agent.state,
            job["job_id"],
            status=payload["status"],
            result=payload.get("result"),
            error=payload.get("error"),
        )
        marker["acknowledged"] = True
        atomic_json(work / "marker.json", marker)
        if marker.get("cgroup"):
            with contextlib.suppress(OSError):
                Path(marker["cgroup"]).rmdir()

    def publish(self, marker, payload):
        from lerobot.data_platform.cli import get_default_output_dir

        staging = Path(marker["staging"])
        replacements = {}
        if marker["job"].get("operation") == "viewer.prepare":
            from lerobot.data_platform.agent import _require_safe_output

            location = marker["job"]["location"]
            source = Path(location["root"])
            target = _require_safe_output(Path(location["output_dir"]), source, self.agent.writable_roots)
            publish_viewer_cache(staging / "cache", target, source)
            replacements[str(staging / "cache")] = str(target)
        if marker["final"] and not marker["job"].get("options", {}).get("dry_run"):
            new = Path(marker["final"])
            old = staging / new.name
            if old.exists():
                if new.exists():
                    raise FileExistsError("Final output exists; refusing to overwrite")
                os.replace(old, new)
            elif not new.exists():
                raise FileNotFoundError("Execution produced no dataset")
            replacements[str(old)] = str(new)
            old_cache, new_cache = get_default_output_dir(old), get_default_output_dir(new)
            if old_cache.exists():
                if new_cache.exists():
                    raise FileExistsError("Final cache exists; refusing to overwrite")
                new_cache.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old_cache, new_cache)
            if new_cache.exists():
                replacements[str(old_cache)] = str(new_cache)
        for path in (Path(marker["work"]) / "uploads").glob("*.json"):
            atomic_json(path, replace_paths(json.loads(path.read_text()), replacements))
        payload = replace_paths(payload, replacements)
        if marker["final"] and (payload.get("result") or {}).get("dataset_location"):
            from lerobot.data_platform.agent import _dataset_payload

            original = payload["result"]["dataset_location"]
            payload["result"]["dataset_location"] = _dataset_payload(
                Path(marker["final"]), node_name=self.agent.name, metadata=original.get("metadata")
            )
        for path in (Path(marker["work"]) / "uploads").glob("*.json"):
            record = json.loads(path.read_text())
            artifact = Path(record["path"])
            if artifact.name == "viewer_manifest.json" and not record.get("curation"):
                atomic_json(artifact, replace_paths(json.loads(artifact.read_text()), replacements))
        return payload


if __name__ == "__main__":
    run_worker(sys.argv[1])
