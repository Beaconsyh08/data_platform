import gzip
import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lerobot.data_platform.operation_log import (
    LOG_FILENAME,
    append_operation_event,
    audit_cli_main,
    read_operation_events,
    sanitize_for_log,
)


def test_sanitize_for_log_redacts_secrets_and_bounds_large_values():
    sanitized = sanitize_for_log(
        {
            "token": "secret-token",
            "nested": {"api-key": "secret-key", "value": "ok"},
            "long": "x" * 5000,
        }
    )

    assert sanitized["token"] == "<redacted>"
    assert sanitized["nested"] == {"api-key": "<redacted>", "value": "ok"}
    assert sanitized["long"].endswith("<truncated>")
    assert "secret" not in json.dumps(sanitized)


def test_append_and_read_operation_events_mirrors_and_deduplicates(tmp_path: Path):
    global_dir = tmp_path / "global"
    dataset_dir = tmp_path / "dataset"
    event = append_operation_event(
        [global_dir, dataset_dir],
        "preprocess_merge",
        status="success",
        dataset_keys=["local/source", "local/output"],
        dataset_roots=[tmp_path / "source", tmp_path / "output"],
        episode_ids=[3, 1, 3],
        details={"api_token": "must-not-leak", "total": 2},
    )

    assert (global_dir / LOG_FILENAME).is_file()
    assert (dataset_dir / LOG_FILENAME).is_file()
    events = read_operation_events(
        [global_dir, dataset_dir],
        dataset_key="local/source",
        operation="merge",
    )
    assert len(events) == 1
    assert events[0]["event_id"] == event["event_id"]
    assert events[0]["episode_ids"] == [1, 3]
    assert events[0]["details"]["api_token"] == "<redacted>"


def test_concurrent_appends_leave_valid_json_lines(tmp_path: Path):
    log_dir = tmp_path / "ledger"

    def write(index: int) -> None:
        append_operation_event(
            log_dir,
            "episode_update",
            status="success",
            episode_ids=[index],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    lines = (log_dir / LOG_FILENAME).read_text().splitlines()
    assert len(lines) == 40
    assert {json.loads(line)["episode_ids"][0] for line in lines} == set(range(40))


def test_operation_logs_rotate_and_retained_events_remain_queryable(tmp_path: Path, monkeypatch):
    from lerobot.data_platform import operation_log

    monkeypatch.setattr(operation_log, "LOG_MAX_BYTES", 1200, raising=False)
    monkeypatch.setattr(operation_log, "LOG_BACKUP_COUNT", 2, raising=False)
    events = [
        append_operation_event(tmp_path, "episode_update", status="success", episode_ids=[index])
        for index in range(12)
    ]
    assert (tmp_path / f"{LOG_FILENAME}.1.gz").is_file()
    assert (tmp_path / f"{LOG_FILENAME}.2.gz").is_file()
    assert not (tmp_path / f"{LOG_FILENAME}.3.gz").exists()
    retained = read_operation_events(tmp_path)
    assert 1 < len(retained) < len(events)
    assert events[-1]["event_id"] in {event["event_id"] for event in retained}
    assert events[0]["event_id"] not in {event["event_id"] for event in retained}
    for path in (
        tmp_path / LOG_FILENAME,
        tmp_path / f"{LOG_FILENAME}.1.gz",
        tmp_path / f"{LOG_FILENAME}.2.gz",
    ):
        assert path.stat().st_size <= 1200
        assert all(
            json.loads(line)["operation"] == "episode_update"
            for line in (
                gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
            ).splitlines()
        )


def test_catalog_sync_audit_keeps_identity_without_large_metadata():
    from lerobot.data_platform.operation_log import summarize_location_sync_request

    metadata = {"feature_" + str(index): "large-metadata-marker" * 100 for index in range(100)}
    location = {"dataset_key": "shared/source", "root": "/datasets/source", "metadata": metadata}
    compact = summarize_location_sync_request({"locations": [location], "token": "top-secret"})
    assert compact["location_count"] == 1
    assert compact["locations"][0]["dataset_key"] == location["dataset_key"]
    assert compact["locations"][0]["root"] == location["root"]
    assert compact["locations"][0]["metadata_summary"]["bytes"] > 100_000
    encoded = json.dumps(compact)
    assert len(encoded) < 3000
    assert "large-metadata-marker" not in encoded
    assert "top-secret" not in encoded
    reordered = {**location, "metadata": dict(reversed(list(metadata.items())))}
    digest = compact["locations"][0]["metadata_summary"]["sha256"]
    assert (
        summarize_location_sync_request({"locations": [reordered]})["locations"][0]["metadata_summary"][
            "sha256"
        ]
        == digest
    )
    assert summarize_location_sync_request({"locations": "invalid", "password": "secret"}) == {
        "locations": "invalid",
        "password": "<redacted>",
    }


def _append_rotating_process_events(directory: Path, offset: int):
    for index in range(offset, offset + 25):
        append_operation_event(directory, "episode_update", status="success", episode_ids=[index])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux workers use flock and fork")
def test_rotation_preserves_events_across_worker_processes(tmp_path: Path, monkeypatch):
    from lerobot.data_platform import operation_log

    monkeypatch.setattr(operation_log, "LOG_MAX_BYTES", 4096)
    monkeypatch.setattr(operation_log, "LOG_BACKUP_COUNT", 32)
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_append_rotating_process_events, args=(tmp_path, index * 25))
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    events = read_operation_events(tmp_path, limit=200)
    assert len(events) == 100
    assert {event["episode_ids"][0] for event in events} == set(range(100))
    assert (tmp_path / f"{LOG_FILENAME}.1.gz").is_file()


def test_audit_cli_main_records_failure_and_redacts_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output_dir = tmp_path / "viewer"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "data-platform",
            "--root",
            str(tmp_path / "dataset"),
            "--output-dir",
            str(output_dir),
            "--tag-vlm-token",
            "top-secret",
        ],
    )

    @audit_cli_main
    def fail() -> None:
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError, match="broken"):
        fail()

    events = read_operation_events(output_dir / "static")
    assert {event["status"] for event in events} == {"started", "failed"}
    serialized = json.dumps(events)
    assert "top-secret" not in serialized
    assert "<redacted>" in serialized


def test_web_mutation_failure_is_queryable_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from lerobot.data_platform import viewer

    captured = {}

    def capture_app(app, **_kwargs):
        captured["app"] = app

    monkeypatch.setattr(viewer.Flask, "run", capture_app)
    static_dir = tmp_path / "console" / "static"
    static_dir.mkdir(parents=True)
    viewer.run_server(
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
        template_folder=Path(viewer.__file__).resolve().parent / "templates",
    )

    client = captured["app"].test_client()
    response = client.post("/api/datasets/register", json={"token": "web-secret"})
    assert response.status_code == 400

    history = client.get("/api/operation_log").get_json()["operations"]
    event = next(item for item in history if item["operation"] == "api_register_dataset")
    assert event["status"] == "failed"
    assert event["details"]["parameters"]["token"] == "<redacted>"
    assert "web-secret" not in json.dumps(history)


def test_legacy_plain_archive_is_compressed_and_remains_queryable(tmp_path):
    event = append_operation_event(tmp_path, "old_operation", status="success")
    (tmp_path / LOG_FILENAME).rename(tmp_path / f"{LOG_FILENAME}.1")
    append_operation_event(tmp_path, "new_operation", status="success")
    assert not (tmp_path / f"{LOG_FILENAME}.1").exists()
    assert (tmp_path / f"{LOG_FILENAME}.1.gz").is_file()
    assert read_operation_events(tmp_path, operation="old_operation")[0]["event_id"] == event["event_id"]
