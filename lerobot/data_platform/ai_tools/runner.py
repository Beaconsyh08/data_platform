"""Independent bounded assistant worker; never submits business operations."""

import argparse
import logging
import signal
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from lerobot.data_platform.ai_tools.runtime_repository import (
    BudgetExceededError,
    LeaseLostError,
    OutcomeUnknownError,
    ReplayChangedError,
    RuntimeRepository,
)
from lerobot.data_platform.ai_tools.service import AssistantService
from lerobot.data_platform.job_management import JobConflictError


class AssistantRunner:
    def __init__(self, service, *, worker=None):
        self.service = service
        self.repository = RuntimeRepository(service.store)
        self.worker = worker or str(uuid.uuid4())

    def tick(self):
        lease = self.repository.claim(self.worker)
        if lease is None:
            return False
        self.run(lease)
        return True

    def run(self, lease):
        from lerobot.data_platform.ai_tools.runtime import generate

        stop = threading.Event()

        def heartbeat():
            while not stop.wait(5):
                try:
                    self.repository.renew(lease)
                except Exception:
                    # No exception bodies: database exceptions may include private inputs.
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            owner, _, payload = self.repository.snapshot(lease)
            try:
                if not self.service.config.enabled:
                    raise PermissionError("Assistant disabled")
                result = generate(self.service, self.repository, lease)
            except LeaseLostError:
                return
            except OutcomeUnknownError:
                result = {
                    "error": "The model request outcome is unknown. It was not automatically resent. Start a new request."
                }
            except ReplayChangedError:
                result = {"error": "Authorized context changed during recovery. Start a new request."}
            except BudgetExceededError:
                result = {"error": "The assistant generation budget was exhausted. Start a new request."}
            except (PermissionError, JobConflictError, KeyError):
                result = {"error": "Dataset access or identity changed. Start a new conversation."}
            except Exception:
                result = {
                    "error": "Qwen could not complete this request. Check server configuration and retry."
                }
            # Result + history + execution terminal state + audit commit together.
            self.service._complete(owner, payload["value"], payload["history"], result, lease=lease)
        except LeaseLostError:
            return
        finally:
            stop.set()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    from lerobot.data_platform.processing.coordinator import configured_manager

    service = AssistantService(configured_manager())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def worker():
        runner = AssistantRunner(service)
        while not stop.is_set():
            try:
                if runner.tick():
                    continue
            except Exception:
                logging.warning("Assistant storage unavailable; execution will be retried after lease expiry")
            stop.wait(1)

    # SIGTERM stops admission and drains bounded in-flight generations before exit.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in [pool.submit(worker) for _ in range(args.workers)]:
            future.result()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.error("Assistant runner could not start; check environment, database and dependencies")
        raise SystemExit(1) from None
