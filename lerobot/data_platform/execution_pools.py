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
    pass


class ThreadPoolExecutor(_BoundedSubmission, _ThreadPoolExecutor):
    pass
