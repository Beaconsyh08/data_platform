"""Viewer cache publication must preserve old data and survive interrupted finalization."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform.execution import ExecutionSupervisor, atomic_json, publish_viewer_cache


def cache_fixture(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    cache = tmp_path / "vis" / ".dp-job-attempt" / "cache"
    target = tmp_path / "vis" / "local_vis_dataset"
    atomic_json(cache / "static/viewer_manifest.json", {"root": str(source), "output_dir": str(cache)})
    (cache / "static/video.mp4").write_bytes(b"new-video")
    return source, cache, target


def test_supervisor_publishes_viewer_paths_and_replays(tmp_path):
    source, cache, target = cache_fixture(tmp_path)
    work = tmp_path / "work"
    atomic_json(
        work / "uploads/manifest.json",
        {"path": str(cache / "static/viewer_manifest.json"), "relative_path": "viewer_manifest.json"},
    )
    supervisor = ExecutionSupervisor(
        SimpleNamespace(state_path=tmp_path / "agent.json", writable_roots=[tmp_path])
    )
    marker = {
        "job": {"operation": "viewer.prepare", "location": {"root": str(source), "output_dir": str(target)}},
        "work": str(work),
        "staging": str(cache.parent),
        "final": None,
    }
    payload = {"status": "done", "result": {"output_dir": str(cache), "static_dir": str(cache / "static")}}
    result = supervisor.publish(marker, payload)
    assert result["result"] == {"output_dir": str(target), "static_dir": str(target / "static")}
    assert not cache.exists()
    assert (target / "static/video.mp4").read_bytes() == b"new-video"
    assert json.loads((work / "uploads/manifest.json").read_text())["path"] == str(
        target / "static/viewer_manifest.json"
    )
    assert json.loads((target / "static/viewer_manifest.json").read_text())["output_dir"] == str(target)
    # A crash before published.json is persisted replays the original worker result.
    assert supervisor.publish(marker, payload) == result


def test_publication_preserves_existing_cache(tmp_path):
    source, cache, target = cache_fixture(tmp_path)
    atomic_json(target / "static/viewer_manifest.json", {"root": str(source)})
    (target / "static/review.json").write_text('"old annotations"')
    publish_viewer_cache(cache, target, source)
    assert (cache.parent / "previous-cache/static/review.json").read_text() == '"old annotations"'
    assert (target / "static/video.mp4").is_file()


def test_publication_restores_backup_after_rename_failure(tmp_path, monkeypatch):
    from lerobot.data_platform import execution

    source, cache, target = cache_fixture(tmp_path)
    atomic_json(target / "static/viewer_manifest.json", {"root": str(source)})
    original = (target / "static/viewer_manifest.json").read_bytes()
    replace = execution.os.replace

    def fail_cache_move(old, new):
        if Path(old) == cache:
            raise OSError("injected publication failure")
        return replace(old, new)

    monkeypatch.setattr(execution.os, "replace", fail_cache_move)
    with pytest.raises(OSError, match="injected"):
        publish_viewer_cache(cache, target, source)
    assert (target / "static/viewer_manifest.json").read_bytes() == original
    assert cache.is_dir()
    monkeypatch.setattr(execution.os, "replace", replace)
    publish_viewer_cache(cache, target, source)
    assert (target / "static/video.mp4").is_file()


def test_publication_resumes_after_old_cache_was_backed_up(tmp_path):
    source, cache, target = cache_fixture(tmp_path)
    atomic_json(cache.parent / "previous-cache/static/viewer_manifest.json", {"root": str(source)})
    publish_viewer_cache(cache, target, source)
    assert (target / "static/video.mp4").is_file()
    assert (cache.parent / "previous-cache/static/viewer_manifest.json").is_file()


@pytest.mark.parametrize("invalid", ["missing", "wrong_source", "unowned_target", "symlink"])
def test_publication_rejects_unsafe_or_incomplete_cache(tmp_path, invalid):
    source, cache, target = cache_fixture(tmp_path)
    if invalid == "missing":
        (cache / "static/viewer_manifest.json").unlink()
    elif invalid == "wrong_source":
        atomic_json(cache / "static/viewer_manifest.json", {"root": str(tmp_path / "other")})
    elif invalid == "unowned_target":
        cache.rename(target)
    else:
        target.symlink_to(source, target_is_directory=True)
    with pytest.raises((ValueError, FileNotFoundError)):
        publish_viewer_cache(cache, target, source)
