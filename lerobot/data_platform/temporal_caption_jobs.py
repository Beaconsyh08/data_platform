"""Remote single-episode annotation and validated, atomic result publication."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from pathlib import Path

from lerobot.data_platform.temporal_caption import artifact_key, infer, prepare, validate_result, write_json
from lerobot.data_platform.temporal_caption_demo import infer_video_events

OPERATION = "caption.annotate"
SCHEMES = {
    "multiview_semantics": ("coarse", "refined"),
    "video_events": ("caption",),
    "fusion_review": ("fusion_draft", "fusion_reviewed"),
}
FILES = {
    "episode.json",
    "video.mp4",
    "coarse.json",
    "refined.json",
    "caption.json",
    "fusion_draft.json",
    "fusion_reviewed.json",
}


def validate_options(options: dict) -> dict:
    if not isinstance(options, dict) or set(options) != {"episode_index", "scheme"}:
        raise ValueError("Specify only episode_index and scheme")
    if type(options["episode_index"]) is not int or options["episode_index"] < 0:
        raise ValueError("episode_index must be a non-negative integer")
    if not isinstance(options["scheme"], str) or options["scheme"] not in SCHEMES:
        raise ValueError("Unknown caption scheme")
    return dict(options)


def validate_source(info: dict) -> None:
    cameras = [v for v in info.get("features", {}).values() if v.get("dtype") == "image"]
    if info.get("codebase_version") != "v3.0" or not 1 <= len(cameras) <= 4:
        raise ValueError("Caption jobs currently require v3.0 data with 1–4 embedded image cameras")


def validate_run(source: Path, *, key: str | None = None, options: dict | None = None) -> list[dict]:
    source = Path(source)
    for name in FILES:
        path = source / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Caption artifacts must be regular files")
    meta = json.loads((source / "episode.json").read_text())
    if not (source / "video.mp4").stat().st_size:
        raise ValueError("Missing review video")
    results = []
    for variant in ("coarse", "refined", "caption", "fusion_draft", "fusion_reviewed"):
        path = source / f"{variant}.json"
        if not path.exists():
            continue
        value = json.loads(path.read_text())
        validate_result(value["result"], value["frame_count"])
        scheme = value.get("scheme_id", "multiview_semantics")
        if scheme not in SCHEMES or variant not in SCHEMES[scheme] or value["variant"] != variant:
            raise ValueError("Invalid scheme/variant")
        if scheme == "fusion_review":
            from lerobot.data_platform.temporal_caption_fusion import validate_partition, validate_review

            validate_partition(value["result"], value["frame_count"])
            audit = value.get("review") or {}
            if audit.get("human_reviewed") is not False or not isinstance(audit.get("stages"), list):
                raise ValueError("Missing fusion review evidence")
            validate_review({"issues": audit.get("remaining_issues")}, value["frame_count"])
            expected_status = (
                "draft"
                if variant == "fusion_draft"
                else ("unresolved" if audit["remaining_issues"] else "no_issues_detected")
            )
            if audit.get("status") != expected_status:
                raise ValueError("Fusion review status contradicts unresolved issues")
        for field in ("episode_index", "frame_count", "fps"):
            if value[field] != meta[field]:
                raise ValueError(f"Artifact {field} does not match episode metadata")
        if type(value["episode_index"]) is not int or value["episode_index"] < 0:
            raise ValueError("Invalid episode index")
        if type(value["fps"]) not in (int, float) or not math.isfinite(value["fps"]) or value["fps"] <= 0:
            raise ValueError("Invalid FPS")
        if not math.isclose(value["duration_s"], value["frame_count"] / value["fps"], abs_tol=0.05):
            raise ValueError("Invalid duration")
        if key is not None and value["dataset_key"] != key:
            raise ValueError("Artifact belongs to another dataset")
        if options and (scheme != options["scheme"] or value["episode_index"] != options["episode_index"]):
            raise ValueError("Artifact does not match the queued episode/scheme")
        if not isinstance(value.get("model"), str) or not value["model"]:
            raise ValueError("Missing model provenance")
        if type(value.get("created_at")) not in (int, float) or not math.isfinite(value["created_at"]):
            raise ValueError("Missing creation time")
        if not isinstance(value.get("dataset_key"), str) or not value["dataset_key"]:
            raise ValueError("Missing dataset identity")
        results.append(value)
    if not results:
        raise ValueError("No caption results")
    scheme = results[0].get("scheme_id", "multiview_semantics")
    if {r["variant"] for r in results} != set(SCHEMES[scheme]):
        raise ValueError("Incomplete or mixed caption scheme")
    return results


def publish_run(source: Path, root: Path, key: str, run: str, *, rebind: bool = False, options=None) -> Path:
    """Publish a complete run once; identical retries succeed, conflicting ones fail."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run):
        raise ValueError("Invalid run name")
    records = validate_run(source, key=None if rebind else key, options=options)
    destination = Path(root) / artifact_key(key) / run
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".caption-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "run"
        staged.mkdir()
        for name in FILES:
            if (source / name).is_file():
                shutil.copyfile(source / name, staged / name)
        for record in records:
            if rebind and record["dataset_key"] != key:
                record["imported_from_dataset_key"] = record["dataset_key"]
                record["dataset_key"] = key
                write_json(staged / f"{record['variant']}.json", record)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(staged.iterdir())}
        write_json(staged / "manifest.json", {"sha256": hashes})
        # Serialize CLI imports and agent completions, including duplicate deliveries.
        import fcntl

        with (destination.parent / ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.exists():
                if not (destination / "manifest.json").is_file():
                    raise FileExistsError("Existing run has no publication manifest")
                if json.loads((destination / "manifest.json").read_text())["sha256"] != hashes:
                    raise FileExistsError("Existing run has different contents")
            else:
                staged.rename(destination)
    return destination


def execute_caption_job(agent, job: dict, source: Path, location: dict) -> dict:
    options = validate_options(job.get("options"))
    validate_source(json.loads((source / "meta/info.json").read_text()))
    from lerobot.data_platform.lifecycle import dataset_snapshot
    from lerobot.data_platform.qwen import QwenClient

    client = QwenClient(timeout_s=600)  # Fail before export if the Agent has no runtime credential.
    fingerprint, _ = dataset_snapshot(source)

    def progress(message):
        agent.client.event(agent.state, job["job_id"], message, {"phase": "annotation"})

    workspace = agent.state_path.parent / "caption-work"
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="episode-", dir=workspace) as temporary:
        bundle, output = Path(temporary) / "bundle", Path(temporary) / "result"
        progress(f"Preparing episode {options['episode_index']} camera frames and measured signals")
        prepare(source, options["episode_index"], bundle)
        from lerobot.data_platform.temporal_caption_fusion import infer_fusion

        run = {
            "multiview_semantics": infer,
            "video_events": infer_video_events,
            "fusion_review": infer_fusion,
        }[options["scheme"]]
        run(bundle, output, location["dataset_key"], "qwen3.8-max", client=client, progress=progress)
        if dataset_snapshot(source)[0] != fingerprint:
            raise ValueError("Source changed during caption inference")
        for variant in SCHEMES[options["scheme"]]:
            artifact = output / f"{variant}.json"
            value = json.loads(artifact.read_text())
            write_json(artifact, {**value, "source_fingerprint": fingerprint})
        validate_run(output, key=location["dataset_key"], options=options)
        for name in sorted(FILES):
            if not (output / name).is_file():
                continue
            progress(f"Uploading annotation artifact: {name}")
            agent.client.upload_artifact(agent.state, job["job_id"], Path(name), output / name, caption=True)
    return {"episode_index": options["episode_index"], "scheme": options["scheme"], "model": "qwen3.8-max"}


def import_archive(stream, root: Path, key: str, dataset_name: str) -> list[str]:
    """Import a trusted administrator's portable review ZIP without extracting arbitrary paths."""
    import zipfile

    with tempfile.TemporaryDirectory(prefix="caption-import-") as temporary:
        staging = Path(temporary)
        names = set()
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if not members or len(members) > 100 or sum(m.file_size for m in members) > 512 * 1024**2:
                raise ValueError("Import exceeds the 100-file / 512-MiB budget")
            seen = set()
            for member in members:
                parts = member.filename.split("/")
                if (
                    len(parts) != 2
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", parts[0])
                    or parts[1] not in FILES
                    or member.filename in seen
                ):
                    raise ValueError("ZIP must contain unique run-name/artifact files only")
                seen.add(member.filename)
                run, filename = parts
                target = staging / run / filename
                target.parent.mkdir(exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                names.add(run)
        # Validate every run before making any run visible.
        for name in sorted(names):
            for record in validate_run(staging / name):
                recorded_name = record.get("dataset_name")
                if recorded_name and recorded_name != dataset_name:
                    raise ValueError("Result dataset name does not match the selected location")
        for name in sorted(names):
            publish_run(staging / name, root, key, name, rebind=True)
        return sorted(names)


def main():
    """Administrative import into an explicitly selected deployment and registered location."""
    import argparse
    import os
    import pwd

    from lerobot.data_platform.control_plane import ControlPlaneStore
    from lerobot.data_platform.deployment import Deployment, load_environment
    from lerobot.data_platform.operation_log import append_operation_event

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--env", choices=("dev", "prod"), required=True)
    parser.add_argument("--location-id", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    deployment = Deployment(args.env)
    load_environment(deployment)
    # Open the administrator's package before switching to the service account.
    with args.archive.open("rb") as archive:
        if os.geteuid() == 0:
            account = pwd.getpwnam(deployment.user)
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        store = ControlPlaneStore(os.environ["DATA_PLATFORM_DATABASE_URL"])
        location = store.get_location(args.location_id)
        output = os.environ.get("DATA_PLATFORM_OUTPUT_DIR")
        if not output:
            output = str(Path(os.environ["DATA_PLATFORM_ROOT"]) / "vis" / "_console")
        static = Path(output) / "static"
        root = Path(
            os.environ.get("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT") or static / "lifecycle/temporal_captions"
        )
        runs = import_archive(archive, root, location["dataset_key"], Path(location["root"]).name)
        append_operation_event(
            static,
            "caption.import",
            phase="complete",
            status="success",
            source="cli",
            dataset_keys=[location["dataset_key"]],
            details={"runs": runs, "location_id": args.location_id},
        )
        print(json.dumps({"runs": runs, "review_url": f"/remote/{args.location_id}/temporal-caption"}))


if __name__ == "__main__":
    from lerobot.data_platform.deployment import safe_cli

    safe_cli(main)
