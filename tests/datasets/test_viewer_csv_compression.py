import gzip

from flask import Flask

from lerobot.data_platform.viewer import _find_any_cached_csv, _serve_csv_stripped


def test_existing_viewer_finds_and_serves_compressed_csv(tmp_path):
    path = tmp_path / "episode_000000_ds1.csv.gz"
    content = "timestamp,stage,subtask_state\n0,0,0\n1,1,1\n"
    path.write_bytes(gzip.compress(content.encode()))
    assert _find_any_cached_csv(tmp_path, 0) == path
    with Flask(__name__).test_request_context():
        response = _serve_csv_stripped(path)
        assert response.get_data(as_text=True) == "timestamp,stage\r\n0,0\r\n1,1\r\n"


def test_compression_keeps_csv_bytes_and_media_unchanged(tmp_path):
    from lerobot.data_platform.cache_files import compress_viewer_csvs, read_cache_bytes

    directory = tmp_path / "csv"
    directory.mkdir()
    path = directory / "episode_000000_ds1.csv"
    content = b"timestamp,state\r\n0,1\r\n"
    path.write_bytes(content)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"original-video")
    before = video.stat()
    compress_viewer_csvs(tmp_path)
    assert not path.exists()
    assert read_cache_bytes(path) == content
    assert video.read_bytes() == b"original-video"
    assert video.stat() == before
    compress_viewer_csvs(tmp_path)


def test_failed_compression_preserves_original(tmp_path, monkeypatch):
    from lerobot.data_platform import cache_files

    path = tmp_path / "episode_000000_ds1.csv"
    path.write_bytes(b"timestamp,state\n0,1\n")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cache_files.os, "replace", fail)
    import pytest

    with pytest.raises(OSError):
        cache_files.compress_file(path)
    assert path.read_bytes() == b"timestamp,state\n0,1\n"
    assert list(tmp_path.iterdir()) == [path]


def test_compressed_cache_symlink_is_rejected_by_results(tmp_path):
    import pytest

    from lerobot.data_platform.dataset_results import result_path

    (tmp_path / "csv").mkdir()
    other = tmp_path / "other"
    other.write_bytes(b"secret")
    (tmp_path / "csv/episode_000000_ds1.csv.gz").symlink_to(other)
    with pytest.raises(ValueError, match="symbolic"):
        result_path(tmp_path, "csv/episode_000000_ds1.csv")


def test_compression_and_stage_write_preserve_file_acl(tmp_path):
    import os
    import shutil
    import subprocess

    import pytest

    from lerobot.data_platform.cache_files import compress_file, open_cached_csv

    if not shutil.which("setfacl"):
        pytest.skip("POSIX ACL tools are required")
    path = tmp_path / "episode_000000_ds1.csv"
    path.write_bytes(b"timestamp,stage\n0,0\n")
    uid = os.getuid() + 10000
    subprocess.run(["setfacl", "-m", f"u:{uid}:r", str(path)], check=True)
    before = os.getxattr(path, "system.posix_acl_access")
    zipped = compress_file(path)
    assert os.getxattr(zipped, "system.posix_acl_access") == before
    with open_cached_csv(zipped, "w") as stream:
        stream.write("timestamp,stage\n0,1\n")
    assert os.getxattr(zipped, "system.posix_acl_access") == before


def test_compression_resumes_after_publication_without_losing_original(tmp_path):
    import pytest

    from lerobot.data_platform.cache_files import compress_file

    path = tmp_path / "episode_000000_ds1.csv"
    content = b"timestamp,stage\n0,0\n"
    path.write_bytes(content)
    zipped = path.with_name(path.name + ".gz")
    zipped.write_bytes(gzip.compress(b"different"))
    with pytest.raises(FileExistsError):
        compress_file(path)
    assert path.read_bytes() == content
    zipped.write_bytes(gzip.compress(content))
    assert compress_file(path) == zipped
    assert not path.exists()
    assert gzip.decompress(zipped.read_bytes()) == content


def test_result_snapshot_limits_decoded_csv_bytes(tmp_path):
    import pytest

    from lerobot.data_platform.dataset_results import ResultSnapshot

    (tmp_path / "static/csv").mkdir(parents=True)
    (tmp_path / ".edited-csv.json").write_text('["csv/episode_000000_ds1.csv"]')
    (tmp_path / "static/csv/episode_000000_ds1.csv.gz").write_bytes(gzip.compress(b"x" * 10000))
    with pytest.raises(ValueError, match="size limit"):
        ResultSnapshot.read(tmp_path / "static", max_bytes=100)
