"""Bounded compute submission inside supervised jobs; ordinary CLI pools keep their behavior."""

import os
import threading
from concurrent.futures import ProcessPoolExecutor as _ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path


def check_stop():
    value = os.environ.get("DATA_PLATFORM_EXECUTION_STOP_FILE")
    if value and Path(value).exists():
        from lerobot.data_platform.execution import StopRequestedError

        raise StopRequestedError("Stop requested at a compute boundary")


class _BoundedSubmission:
    def __init__(self, max_workers=None, **kwargs):
        self._supervised = bool(os.environ.get("DATA_PLATFORM_EXECUTION_STOP_FILE"))
        if self._supervised:
            max_workers = min(max_workers or 4, int(os.environ.get("DATA_PLATFORM_EXECUTION_WORKERS", "4")))
        super().__init__(max_workers=max_workers, **kwargs)
        self._pending_slots = threading.BoundedSemaphore(max(1, self._max_workers * 2))

    def submit(self, fn, /, *args, **kwargs):
        if not self._supervised:
            return super().submit(fn, *args, **kwargs)
        while not self._pending_slots.acquire(timeout=0.2):
            check_stop()
        try:
            check_stop()
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._pending_slots.release()
            raise
        future.add_done_callback(lambda _: self._pending_slots.release())
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        stopped = self._supervised and Path(os.environ["DATA_PLATFORM_EXECUTION_STOP_FILE"]).exists()
        return super().shutdown(wait=wait, cancel_futures=cancel_futures or stopped)


class ProcessPoolExecutor(_BoundedSubmission, _ProcessPoolExecutor):
    def __init__(self, max_workers=None, **kwargs):
        if os.environ.get("DATA_PLATFORM_EXECUTION_NATIVE_THREADS") and "initializer" not in kwargs:
            from lerobot.data_platform.execution_resources import configure_native_threads

            kwargs["initializer"] = configure_native_threads
        super().__init__(max_workers=max_workers, **kwargs)


class ThreadPoolExecutor(_BoundedSubmission, _ThreadPoolExecutor):
    pass


def effective_workers(requested: int) -> int:
    count = max(1, int(requested))
    if os.environ.get("DATA_PLATFORM_EXECUTION_STOP_FILE"):
        count = min(count, int(os.environ.get("DATA_PLATFORM_EXECUTION_WORKERS", "4")))
    return count


def map_completed(executor, function, items):
    """Yield completed results before refilling a bounded submission window."""
    from concurrent.futures import FIRST_COMPLETED, wait

    iterator = iter(items)
    pending = set()
    exhausted = False
    try:
        while pending or not exhausted:
            check_stop()
            while not exhausted and len(pending) < executor._max_workers * 2:
                check_stop()
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(executor.submit(function, item))
            if not pending:
                break
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                check_stop()
                yield future.result()
    finally:
        for future in pending:
            future.cancel()
