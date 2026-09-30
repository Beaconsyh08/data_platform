"""Inspect actual source/cache values without full data loading or source mutation."""

import hashlib
import json
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from flask import Flask

from lerobot.data_platform.precompute.inspection import MAX_RESPONSE_BYTES, DatasetInspection
from lerobot.data_platform.routes.inspect import register_inspect_routes
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_lifecycle import _make_dataset


def _local(tmp_path):
    root = tmp_path / "dataset"
    _make_dataset(root)
    static = tmp_path / "cache/static"
    (static / "csv").mkdir(parents=True)
    return DatasetInspection.local(root, static)


def _ctx(inspection, store=None, remote_cache_root=None):
    return SimpleNamespace(
        datasets_index={("local", "test"): {"root": str(inspection.root)}},
        static_dir_for_key=lambda key: inspection.static_dir,
        repo_id_from_key=lambda key: "/".join(key),
        control_plane_store=store,
        remote_cache_root=remote_cache_root,
    )


def test_raw_records_and_csv_values_preserved_with_paging(tmp_path):
    inspection = _local(tmp_path)
    csv_path = inspection.static_dir / "csv/episode_000001_ds5.csv"
    csv_path.write_text("timestamp,gripper,gripper,subtask_state\n0.000,0.123,4,2\n0.500,0.999,5,3\n")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()
    }
    schema = inspection.schema("parquet")
    assert schema["episode_index"] == 0
    assert next(c for c in schema["columns"] if c["name"] == "action")["shape"] == [2]
    data = inspection.rows("parquet", 1, 1, 1, ["frame_index", "action"])
    assert data["rows"] == [[1, [1.0, 1.0]]]
    assert data["has_more"] and data["next_offset"] == 2
    csv_schema = inspection.schema("csv")
    assert csv_schema["episode_index"] == 1  # first episode with available cache
    assert [c["name"] for c in csv_schema["columns"]] == ["timestamp", "gripper", "gripper", "subtask_state"]
    data = inspection.rows("csv", 1, 0, 1, ["1", "2", "3"])
    assert data["rows"] == [["0.123", "4", "2"]]
    assert data["provenance"]["downsample"] == 5
    assert data["provenance"]["source_frames"] == 3
    assert data["cache_rows"] is None
    end = inspection.rows("csv", 1, 1, 50, ["0"])
    assert end["cache_rows"] == 2 and not end["has_more"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    assert set(before) == {path for path in tmp_path.rglob("*") if path.is_file()}


def test_v3_shared_shard_filters_before_offset_and_excludes_binary(tmp_path, monkeypatch):
    root = tmp_path / "v3"
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "features": {"camera": {"dtype": "image", "shape": [3, 4, 4]}},
            }
        )
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": i,
                    "length": 3 if i == 3 else 2,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                }
                for i in (3, 9)
            ]
        ),
        root / "meta/episodes/chunk-000/file-000.parquet",
    )
    path = root / "data/chunk-000/file-000.parquet"
    pq.write_table(
        pa.table(
            {
                "episode_index": [3, 9, 3, 9, 3],
                "frame_index": [0, 0, 1, 1, 2],
                "camera": [b"large embedded image"] * 5,
            }
        ),
        path,
    )
    inspection = DatasetInspection.local(root, None)
    real_file = pq.ParquetFile
    projected = []

    class SpyFile:
        def __init__(self, path):
            self.file = real_file(path)

        def iter_batches(self, **kwargs):
            projected.append(kwargs["columns"])
            yield from self.file.iter_batches(**kwargs)

    monkeypatch.setattr(pq, "ParquetFile", SpyFile)
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: pytest.fail("Inspect must not load a whole table"))
    result = inspection.rows("parquet", 3, 1, 1, ["episode_index", "frame_index", "camera"])
    assert result["rows"][0][:2] == [3, 1]
    assert result["rows"][0][2]["omitted"]
    assert projected == [["episode_index", "frame_index"]]
    assert result["has_more"]


def test_response_cap_large_values_and_safe_json(tmp_path):
    inspection = _local(tmp_path)
    path = inspection.static_dir / "csv/episode_000000_ds1.csv"
    path.write_text("text\n" + ("x" * 100000 + "\n") * 200)
    result = inspection.rows("csv", 0, 0, 200, ["0"])
    assert len(json.dumps(result).encode()) < MAX_RESPONSE_BYTES
    assert result["rows"][0][0]["truncated"]
    assert result["rows"][0][0]["length"] == 100000
    pq.write_table(
        pa.table({"episode_index": [0], "action": [[float("nan"), float("inf")]]}),
        inspection.root / "data/chunk-000/episode_000000.parquet",
    )
    result = inspection.rows("parquet", 0, 0, 50, ["action"])
    json.dumps(result, allow_nan=False)
    assert result["rows"][0][0] == [{"non_finite": "nan"}, {"non_finite": "inf"}]


def test_csv_replacement_cannot_mix_old_headers_with_new_values(tmp_path, monkeypatch):
    inspection = _local(tmp_path)
    path = inspection.static_dir / "csv/episode_000000_ds1.csv"
    path.write_text("state\n1\n")
    original = inspection.columns

    def replaced_columns(*args):
        columns = original(*args)
        path.write_text("action\n2\n")
        return columns

    monkeypatch.setattr(inspection, "columns", replaced_columns)
    with pytest.raises(ValueError, match="CSV schema changed"):
        inspection.rows("csv", 0, 0, 50, ["0"])


def test_window_stops_batches_after_lookahead(tmp_path, monkeypatch):
    inspection = _local(tmp_path)
    pq.write_table(
        pa.table({"episode_index": [0] * 10000, "frame_index": list(range(10000))}),
        inspection.root / "data/chunk-000/episode_000000.parquet",
    )
    real_file = pq.ParquetFile
    consumed = []

    class SpyFile:
        def __init__(self, path):
            self.file = real_file(path)

        def iter_batches(self, **kwargs):
            for batch in self.file.iter_batches(**kwargs):
                consumed.append(batch.num_rows)
                yield batch

    monkeypatch.setattr(pq, "ParquetFile", SpyFile)
    result = inspection.rows("parquet", 0, 0, 50, ["frame_index"])
    assert len(result["rows"]) == 50 and consumed == [256]


def test_route_validation_missing_data_and_path_escape(tmp_path):
    inspection = _local(tmp_path)
    app = Flask(__name__)
    register_inspect_routes(app, _ctx(inspection))
    client = app.test_client()
    base = "/api/datasets/local/test/inspect"
    assert client.get(base + "/schema").status_code == 200
    for query in ("limit=201", "offset=-1", "offset=no", "columns=missing", "source=other", "limit=0"):
        assert client.get(base + "/rows?episode_index=0&" + query).status_code == 400
    assert client.get(base + "/rows").status_code == 400
    assert client.get(base + "/rows?episode_index=88").status_code == 404
    assert client.get(base + "/schema?source=csv").status_code == 404
    outside = tmp_path / "outside.csv"
    outside.write_text("secret\nvalue\n")
    (inspection.static_dir / "csv/episode_000000_ds1.csv").symlink_to(outside)
    assert client.get(base + "/schema?source=csv").status_code == 403


def test_remote_viewer_scope_isolation_and_stale_offline_cache(tmp_path):
    inspection = _local(tmp_path)
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    register_inspect_routes(app, _ctx(inspection, store, tmp_path / "remote-cache"))
    client = app.test_client()
    _bootstrap(client)
    _, node = store.enroll_node(
        name="server-b",
        hostname="offline.invalid",
        allowed_roots=["/does-not-exist"],
        writable_roots=["/output"],
        capabilities={},
        enrollment_token="test",
        expected_token="test",
    )
    locations = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "remote/a",
                "root": "/does-not-exist/a",
                "metadata": {
                    "episodes": [{"episode_index": 3, "length": 100}],
                    "viewer_ready": False,
                },
            },
            {
                "dataset_key": "remote/b",
                "root": "/does-not-exist/b",
                "metadata": {
                    "episodes": [{"episode_index": 3, "length": 100}],
                },
            },
        ],
    )
    base = "/api/control/locations/" + locations[0]["location_id"] + "/inspect"
    assert client.get(base + "/schema").status_code == 404
    for index, location in enumerate(locations):
        static = tmp_path / "remote-cache" / location["location_id"] / "static/csv"
        static.mkdir(parents=True)
        (static / "episode_000003_ds5.csv").write_text(f"action,action\n{index},0.123\n")
    assert client.get(base + "/schema?source=parquet").status_code == 400
    result = client.get(base + "/rows?episode_index=3&columns=0&columns=1").get_json()
    assert result["rows"] == [["0", "0.123"]]
    assert result["provenance"]["cache_status"] == "stale"
    other_cache = tmp_path / "remote-cache" / locations[1]["location_id"]
    store.mark_viewer_ready(
        locations[0]["location_id"], viewer_url="/remote/a/episode_3", cache_root=str(other_cache)
    )
    assert client.get(base + "/schema").status_code == 403
    store.mark_viewer_stale(locations[0]["location_id"])
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "inspect-viewer",
            "display_name": "Inspect Viewer",
            "password": "strong-viewer-password",
        },
    ).get_json()["user"]
    store.update_user(registered["user_id"], role="viewer", active=True)
    assert (
        client.put(
            f"/api/auth/users/{registered['user_id']}/data-scopes",
            json={
                "location_ids": [locations[0]["location_id"]],
            },
        ).status_code
        == 200
    )
    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": "inspect-viewer",
                "password": "strong-viewer-password",
            },
        ).status_code
        == 200
    )
    assert client.get(base + "/rows?episode_index=3").status_code == 200
    assert (
        client.get("/api/control/locations/" + locations[1]["location_id"] + "/inspect/schema").status_code
        == 403
    )
    assert client.get("/api/datasets/local/test/inspect/schema").status_code == 403
    assert store.list_jobs() == []
