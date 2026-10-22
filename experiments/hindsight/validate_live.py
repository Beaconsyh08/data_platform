"""Real Hindsight/Qwen with isolated synthetic completion fixtures and actual asynchronous Runner.

No production database is opened. Fixture acknowledgements simulate completed jobs; model
probes never confirm actions. Credentials must be provided through the environment.
"""

import argparse
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from lerobot.data_platform.ai_tools import experience, experience_worker, hindsight  # noqa: E402
from lerobot.data_platform.ai_tools.models import AssistantExperience  # noqa: E402
from lerobot.data_platform.ai_tools.runner import AssistantRunner  # noqa: E402
from lerobot.data_platform.ai_tools.service import AssistantService  # noqa: E402
from lerobot.data_platform.qwen import QwenClient  # noqa: E402
from tests.datasets.test_ai_assistant import FakeQwen  # noqa: E402
from tests.datasets.test_ai_experience import history  # noqa: E402
from tests.datasets.test_ai_memory_followups import save  # noqa: E402
from tests.datasets.test_processing import setup  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20, choices=range(1, 21))
    parser.add_argument(
        "--outage-marker", action="store_true", help="Wait for the isolated test harness to stop its API"
    )
    args = parser.parse_args()
    work = args.output_dir.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    setup_value = setup.__wrapped__(work)
    store, manager, actor, _, location = setup_value
    service = AssistantService(manager)
    fake = FakeQwen()
    service.client_factory = lambda **_: fake
    body = {
        "request_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "location_id": location["location_id"],
        "expected_revision": 0,
        "message": "Prepare Viewer",
    }
    with pytest.MonkeyPatch.context() as patch:
        history.__wrapped__((service, fake, actor["user_id"], body), setup_value, work, patch)
        service.client_factory = QwenClient
        service.config = replace(service.config, backend="langgraph")
        started = time.monotonic()

        class InspectedBackend(hindsight.Backend):
            def request(self, method, route="", body=None, **kwargs):
                result = super().request(method, route, body, **kwargs)
                if route.startswith("/memories/list"):
                    (work / "synthetic-units.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2)
                    )
                return result

        experience_worker.tick(service, backend_factory=InspectedBackend)
        with store.sessions() as session:
            row = session.scalar(select(AssistantExperience))
            report = {
                "background_seconds": time.monotonic() - started,
                "background_status": row.status,
                "remote_bank_cleaned": not row.bank_id,
                "observations": row.observations,
                "probes": [],
            }

        def checkpoint():
            (work / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        checkpoint()
        if report["background_status"] != "ready" or not report["observations"]:
            raise RuntimeError("Background did not produce a validated experience")
        timings = []
        for _ in range(50):
            start = time.monotonic()
            assert experience.context(service, actor["user_id"], location["location_id"])
            timings.append(time.monotonic() - start)
        report["cache_median_seconds"] = statistics.median(timings)
        report["cache_max_seconds"] = max(timings)
        initial_jobs, initial_runs = len(store.list_jobs()), len(manager.runs(actor["user_id"]))
        for index in range(args.samples):
            prepare = index % 2 == 0
            message = (
                "给当前数据准备 Quality 检查，只用 S3，workers=2，其他参数用有效默认值。等我确认再执行。"
                if prepare
                else "简短总结已有 Viewer 历史任务的共同结果，引用来源 run_id；不要创建任务。"
            )
            value = {
                **body,
                "request_id": str(uuid.uuid4()),
                "conversation_id": str(uuid.uuid4()),
                "message": message,
            }
            start = time.monotonic()
            admitted = service.submit(actor["user_id"], value)
            assert admitted["status"] == "running"
            assert AssistantRunner(service).tick()
            result = service.turn(actor["user_id"], value["request_id"])
            action = result.get("action") or {}
            expected = (
                action.get("can_execute")
                and action.get("parameters", {}).get("checks") == ["s3"]
                and action.get("parameters", {}).get("workers") == 2
                if prepare
                else not action
            )
            report["probes"].append(
                {
                    "index": index,
                    "kind": "prepare" if prepare else "explain",
                    "elapsed_seconds": time.monotonic() - start,
                    "status": result["status"],
                    "expected_behavior": bool(expected),
                    "result": result,
                }
            )
            checkpoint()
            print(f"probe {index}: {result['status']}, expected={bool(expected)}", flush=True)
        if args.outage_marker:
            (work / "outage-ready").touch()
            stop_deadline = time.monotonic() + 60
            while not (work / "outage-stopped").exists():
                if time.monotonic() >= stop_deadline:
                    raise TimeoutError("Test harness did not stop its API")
                time.sleep(0.2)
            assert experience.context(service, actor["user_id"], location["location_id"])
        # Four distinct users exercise the real global concurrency without bypassing
        # the normal one-active-request-per-account admission rule.
        requests = []
        for index in range(4):
            if index:
                account = store.register_user(username=f"burst{index}", password="synthetic-burst-password")
                store.update_user(account["user_id"], active=True, role="admin")
            else:
                account = actor
            value = {
                **body,
                "request_id": str(uuid.uuid4()),
                "conversation_id": str(uuid.uuid4()),
                "message": "准备 Quality 检查，只用 S3，workers=2，其他参数用默认值，等我确认再执行。",
            }
            assert service.submit(account["user_id"], value)["status"] == "running"
            requests.append((account["user_id"], value["request_id"]))
        burst_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(pool.map(lambda _: AssistantRunner(service).tick(), range(4)))
        report["concurrent_seconds"] = time.monotonic() - burst_start
        report["concurrent_with_hindsight_stopped"] = args.outage_marker
        report["concurrent_probes"] = []
        for owner, identity in requests:
            result = service.turn(owner, identity)
            action = result.get("action") or {}
            report["concurrent_probes"].append(
                {
                    "status": result["status"],
                    "expected_behavior": bool(
                        action.get("can_execute")
                        and action.get("parameters", {}).get("checks") == ["s3"]
                        and action.get("parameters", {}).get("workers") == 2
                    ),
                    "result": result,
                }
            )
        checkpoint()
        start = time.monotonic()
        save(service, actor["user_id"], "A new approved rule supersedes earlier task settings")
        report["rule_edit_immediately_hides_old_experience"] = not experience.context(
            service, actor["user_id"], location["location_id"]
        )
        report["rule_edit_seconds"] = time.monotonic() - start
        report["additional_jobs"] = len(store.list_jobs()) - initial_jobs
        report["additional_runs"] = len(manager.runs(actor["user_id"])) - initial_runs
        report["completed"] = True
        checkpoint()
        assert report["rule_edit_immediately_hides_old_experience"]
        assert report["additional_jobs"] == report["additional_runs"] == 0
        assert all(p["status"] == "done" and p["expected_behavior"] for p in report["probes"])
        assert all(p["status"] == "done" and p["expected_behavior"] for p in report["concurrent_probes"])


if __name__ == "__main__":
    main()
