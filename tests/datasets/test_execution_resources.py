"""Resource ceilings and incremental Viewer scheduling regressions."""

import os
import threading

import pytest

from lerobot.data_platform import execution_resources as resources
from lerobot.data_platform.execution import StopRequestedError
from lerobot.data_platform.execution_pools import ThreadPoolExecutor, map_completed
from lerobot.data_platform.precompute.viewer_work import ViewerWork, run_viewer_work


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in os.environ:
        if (
            key.startswith(("DATA_PLATFORM_COMPUTE_", "DATA_PLATFORM_EXECUTION_"))
            or key == "DATA_PLATFORM_VIDEO_THREADS"
        ):
            monkeypatch.delenv(key)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 224)
    monkeypatch.setattr(resources, "inherited_limits", lambda: (224, 2 * 1024**4))


def test_default_and_absolute_resources(monkeypatch):
    default = resources.ExecutionResources.resolve()
    assert default.max_workers == 4
    assert default.cpu_limit == 112
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MAX_WORKERS", "64")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_CPU_LIMIT", "32")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MEMORY_LIMIT_GIB", "64")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_NATIVE_THREADS", "1")
    monkeypatch.setenv("DATA_PLATFORM_VIDEO_THREADS", "2")
    budget = resources.ExecutionResources.resolve()
    assert (budget.max_workers, budget.cpu_limit, budget.memory_limit) == (32, 32, 64 * 1024**3)
    assert budget.workers(1) == 1
    assert budget.workers(0) == 32
    assert budget.workers(64) == 32
    assert budget.environment()["OMP_NUM_THREADS"] == "1"
    assert budget.video_threads == 2


def test_parent_limits_win(monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MAX_WORKERS", "64")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_CPU_LIMIT", "64")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MEMORY_LIMIT_GIB", "128")
    monkeypatch.setattr(resources, "inherited_limits", lambda: (2, 4 * 1024**3))
    budget = resources.ExecutionResources.resolve()
    assert (budget.max_workers, budget.cpu_limit, budget.memory_limit) == (2, 2, 4 * 1024**3)


@pytest.mark.parametrize("slots", [2, 4])
def test_fixed_execution_slots(monkeypatch, slots):
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MAX_WORKERS", "16")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_CPU_LIMIT", "32")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MEMORY_LIMIT_GIB", "64")
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_NATIVE_THREADS", "1")
    monkeypatch.setenv("DATA_PLATFORM_VIDEO_THREADS", "1")
    parent = resources.ExecutionResources.resolve()
    slot = parent.for_slots(slots)
    assert (slot.cpu_limit, slot.memory_limit, slot.max_workers) == (
        32 / slots,
        60 * 1024**3 // slots,
        32 // slots,
    )
    assert (slot.native_threads, slot.video_threads) == (1, 1)
    assert parent == parent.for_slots(1)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad", "1.5"])
def test_invalid_worker_config_fails(value, monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_COMPUTE_MAX_WORKERS", value)
    with pytest.raises(ValueError, match="MAX_WORKERS"):
        resources.ExecutionResources.resolve()


def test_read_ancestor_limits_and_affinity(tmp_path, monkeypatch):
    # Restore the real function hidden by the default fixture.
    import importlib

    importlib.reload(resources)
    parent = tmp_path / "service"
    child = parent / "supervisor"
    child.mkdir(parents=True)
    (parent / "cpu.max").write_text("150000 100000")
    (parent / "memory.max").write_text(str(4 * 1024**3))
    (parent / "cpuset.cpus.effective").write_text("0-7,10-11")
    monkeypatch.setattr(resources, "cgroup_directory", lambda: child)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    assert resources.inherited_limits() == (1.5, 4 * 1024**3)
    (parent / "cpu.max").write_text("max 100000")
    assert resources.inherited_limits()[0] == 4
    monkeypatch.setattr(resources, "cgroup_directory", lambda: None)
    assert resources.inherited_limits()[0] == 4


def test_completed_results_arrive_before_all_work_is_submitted():
    supplied = []

    def items():
        for index in range(20):
            supplied.append(index)
            yield index

    with ThreadPoolExecutor(max_workers=2) as executor:
        iterator = map_completed(executor, lambda n: n, items())
        next(iterator)
        assert len(supplied) == 4
        assert len(list(iterator)) == 19


def test_single_episode_cameras_parallel_and_progress(tmp_path):
    gate = threading.Barrier(3)

    def work():
        gate.wait(timeout=5)
        return "ok"

    items = [ViewerWork(0, tmp_path / str(i), work) for i in range(3)]
    events, results = [], []
    run_viewer_work(items, 3, events.append, lambda item, result: results.append(result))
    assert results == ["ok"] * 3
    assert [e["files_current"] for e in events] == [0, 1, 2, 3]
    assert [e["current"] for e in events] == [0, 0, 0, 1]


def test_supervised_worker_limit_and_serial_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PLATFORM_EXECUTION_STOP_FILE", str(tmp_path / "stop"))
    monkeypatch.setenv("DATA_PLATFORM_EXECUTION_WORKERS", "2")
    with ThreadPoolExecutor(max_workers=64) as executor:
        assert executor._max_workers == 2
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor._max_workers == 1


def test_failure_stops_incremental_submission(tmp_path):
    started = []

    def fail():
        started.append(1)
        raise ValueError("failed camera")

    items = [ViewerWork(0, tmp_path / str(i), fail) for i in range(100)]
    with pytest.raises(ValueError, match="failed camera"):
        run_viewer_work(items, 1)
    assert len(started) <= 2


def test_cancel_at_compute_boundary(tmp_path, monkeypatch):
    stop = tmp_path / "stop"
    monkeypatch.setenv("DATA_PLATFORM_EXECUTION_STOP_FILE", str(stop))

    def work():
        stop.touch()

    with pytest.raises(StopRequestedError):
        run_viewer_work([ViewerWork(0, tmp_path / "video", work)], 1)


def test_duplicate_outputs_rejected_before_work(tmp_path):
    item = ViewerWork(0, tmp_path / "same", lambda: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="Duplicate"):
        run_viewer_work([item, item], 2)


@pytest.mark.parametrize(
    "configured, expected", [(None, 1), ("1", 1), ("2", 2), ("4", 4), ("8", 8), ("99", 8)]
)
def test_agent_slot_configuration(monkeypatch, configured, expected):
    from lerobot.data_platform.job_concurrency import agent_slots

    monkeypatch.delenv("DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS", raising=False)
    if configured is not None:
        monkeypatch.setenv("DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS", configured)
    assert agent_slots() == expected
