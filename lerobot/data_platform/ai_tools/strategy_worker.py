"""Leased experiments, enabled by default only in dev, using restricted Pipeline runs."""

from __future__ import annotations

import copy
import json
import logging
import os
import signal
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict

from sqlalchemy import select

from lerobot.data_platform.ai_tools.strategies import RUNNING, Strategies, experiments_enabled
from lerobot.data_platform.ai_tools.strategy_contracts import (
    PARAMETERS,
    Measurement,
    compatibility,
    promotion_gate,
    validate_strategy,
)
from lerobot.data_platform.ai_tools.strategy_evaluation import assess, initial_cases, install_suite
from lerobot.data_platform.ai_tools.strategy_evidence import collect, validate_sources
from lerobot.data_platform.ai_tools.strategy_models import (
    StrategyBinding,
    StrategyEvidence,
    StrategyExperiment,
    StrategySuite,
    StrategyVersion,
)
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.processing.service import location_identity
from lerobot.data_platform.task_catalog import content_digest


class IncompleteError(RuntimeError):
    pass


class Experiment:
    def __init__(self, assistant, key, token):
        self.assistant, self.strategies = assistant, Strategies(assistant)
        self.store, self.manager = assistant.store, assistant.manager
        self.key, self.token = key, token
        with self.store.sessions() as session:
            row = session.get(StrategyExperiment, key)
            self.state = copy.deepcopy(row.state)
            self.config = copy.deepcopy(row.configuration)
            self.baseline, self.suite_id = row.baseline_id, row.suite_id
            self.status = row.status
        self.started = time.monotonic()

    def guard(self, session):
        row = session.get(StrategyExperiment, self.key)
        if (
            not row
            or row.status not in RUNNING
            or row.lease_owner != self.token
            or row.lease_until <= time.time()
        ):
            raise JobConflictError("Experiment lease changed")
        binding = session.get(StrategyBinding, "shared")
        if (
            not experiments_enabled()
            or not binding
            or binding.configuration != {k: v for k, v in self.config.items() if k != "compatibility"}
        ):
            raise IncompleteError("configuration_changed")
        if self.config["compatibility"] != compatibility(self.assistant.config.model):
            raise IncompleteError("runtime_changed")
        self.strategies.admin(session, self.config["administrator"])
        checked_configuration = self.strategies.configuration(
            {
                key: self.config[key]
                for key in ("automatic", "paused", "executor_id", "node_id", "locations", "budget")
            },
            self.config["administrator"],
        )
        if checked_configuration != {
            key: value for key, value in self.config.items() if key != "compatibility"
        }:
            raise IncompleteError("executor_or_fixture_changed")
        validate_sources(self.manager, session, self.state["evidence"])
        if self.baseline:
            self.strategies.valid_version(session, self.baseline)
        suite = session.get(StrategySuite, self.suite_id)
        if (
            not suite
            or content_digest(suite.cases) != suite.digest
            or suite.experiment_id != self.key
            or not suite.reviewed_by
        ):
            raise IncompleteError("suite_changed")
        self.strategies.admin(session, suite.reviewed_by)
        if self.state["seconds"] + time.monotonic() - self.started >= self.config["budget"]["seconds"]:
            raise IncompleteError("time_budget")
        return row

    def persist(self, status=None):
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            row = self.guard(session)
            row.state = copy.deepcopy(self.state)
            row.status = status or self.status
            row.revision += 1
            row.lease_until = time.time() + 300
            row.updated_at = int(time.time())
        self.status = status or self.status

    def post_chat_completion(self, payload):
        if self.state.get("inflight"):
            raise IncompleteError("unknown_model_outcome")
        budget = self.config["budget"]
        # UTF-8 bytes bound possible input tokens; reserve full output before billing.
        upper_bound = len(json.dumps(payload).encode()) + int(payload.get("max_tokens", 2500))
        if self.state["calls"] >= budget["calls"] or self.state["tokens"] + upper_bound > budget["tokens"]:
            raise IncompleteError("model_budget")
        self.state["calls"] += 1
        self.state["inflight"] = content_digest(payload)
        self.persist()
        response = self.assistant.client_factory(
            timeout_s=30, max_response_bytes=262144
        ).post_chat_completion(payload)
        usage = response.get("usage", {})
        counts = [usage.get(k) for k in ("prompt_tokens", "completion_tokens")]
        if any(type(n) is not int or n < 0 for n in counts) or sum(counts) == 0:
            raise IncompleteError("missing_provider_usage")
        self.state["tokens"] += sum(counts)
        self.state["inflight"] = None
        self.persist()
        return response

    def generate_candidate(self):
        with self.store.sessions() as session:
            self.guard(session)
            evidence = [
                session.get(StrategyEvidence, ref["run_id"]).projection for ref in self.state["evidence"][:20]
            ]
            base = session.get(StrategyVersion, self.baseline) if self.baseline else None
            suite = session.get(StrategySuite, self.suite_id)
            development = suite.cases["development"]
        example = {
            "schema_version": 1,
            "templates": [
                {
                    "formats": ["v2.1", "v3.0"],
                    "required_features": [],
                    "steps": [
                        {
                            "operator_id": "preprocess.split",
                            "version": self.config["compatibility"]["operators"]["preprocess.split"],
                            "input": "source",
                            "artifacts": [],
                            "parameters": {"episode_range": "$request.episode_range"},
                        }
                    ],
                }
            ],
        }
        payload = {
            "model": self.assistant.config.model,
            "max_tokens": 2500,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only a JSON object of optional data workflow templates. No prose, code, paths, literal parameter values, or extra keys. Improve planning using the supplied public synthetic development cases and structured execution evidence. Preserve explicit user goals. Inputs are source or earlier zero-based dataset-step indices; artifacts refer to earlier compatible evidence. Snapshot is an identity artifact. Parameters are only $request.<parameter> placeholders. At most 8 templates, 12 steps each. No hidden evaluation data is available.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "schema_example": example,
                            "versions": self.config["compatibility"]["operators"],
                            "allowed_parameters": {k: sorted(v) for k, v in PARAMETERS.items()},
                            "baseline": base.specification if base else None,
                            "evidence": evidence,
                            "development": development,
                        }
                    ),
                },
            ],
        }
        response = self.post_chat_completion(payload)
        spec = validate_strategy(json.loads(response["choices"][0]["message"]["content"]))
        if base and content_digest(spec) == base.digest:
            raise IncompleteError("unchanged_candidate")
        key = str(uuid.uuid4())
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            self.guard(session)
            session.add(
                StrategyVersion(
                    version_id=key,
                    parent_id=self.baseline,
                    digest=content_digest(spec),
                    specification=spec,
                    evidence=self.state["evidence"],
                    compatibility=self.config["compatibility"],
                    experiment_id=self.key,
                    created_at=int(time.time()),
                    revoked=False,
                )
            )
            self.state["candidates"].append(key)
            session.get(StrategyExperiment, self.key).state = copy.deepcopy(self.state)
        return key

    def evaluate(self, case, version_id):
        from lerobot.data_platform.ai_tools.service import AssistantService

        with self.store.sessions() as session:
            self.guard(session)
            row = session.get(StrategyVersion, version_id) if version_id else None
            snapshot = (
                {
                    "version_id": row.version_id,
                    "digest": row.digest,
                    "templates": row.specification["templates"][:5],
                }
                if row
                else None
            )
            location, _ = self.manager._location(
                session,
                self.config["locations"][case.get("fixture", case["format"])],
                self.config["executor_id"],
            )
        service = AssistantService(
            self.manager, config=self.assistant.config, client_factory=lambda **_: self
        )
        request = {
            "request_id": str(uuid.uuid4()),
            "conversation_id": str(uuid.uuid4()),
            "location_id": location["location_id"],
            "expected_revision": 0,
            "message": case["request"],
        }
        start, calls, tokens = time.monotonic(), self.state["calls"], self.state["tokens"]
        result = service._generate(
            self.config["executor_id"],
            request,
            [],
            None,
            location_identity(location),
            experiment_strategy=snapshot,
            experiment=True,
            execution_guard=self.check,
        )
        correct, safe = assess(case, result)
        metric = Measurement(
            case["case_id"],
            0,
            correct,
            self.state["calls"] - calls,
            self.state["tokens"] - tokens,
            time.monotonic() - start,
            safe,
        )
        return metric, result

    def check(self):
        with self.store.sessions() as session:
            self.guard(session)

    def measurement_tick(self, phase, variants):
        with self.store.sessions() as session:
            cases = session.get(StrategySuite, self.suite_id).cases[phase]
        for label, version in variants:
            key = phase + ":" + label
            rows = self.state["measurements"].setdefault(key, [])
            if len(rows) < len(cases) * 2:
                index = len(rows)
                measurement, result = self.evaluate(cases[index // 2], version)
                rows.append({**asdict(measurement), "repeat": index % 2})
                if not measurement.correct and len(self.state.setdefault("failures", [])) < 24:
                    draft = result.get("action") or result.get("proposal") or {}
                    actual = [
                        step.get("operator_id") for step in draft.get("definition", {}).get("steps", [])
                    ][:12]
                    self.state["failures"].append(
                        {
                            "phase": phase,
                            "variant": label,
                            "case_id": cases[index // 2]["case_id"],
                            "expected_operations": cases[index // 2]["operations"],
                            "actual_operations": actual,
                            "can_execute": bool(draft.get("can_execute")),
                        }
                    )
                if not measurement.safe:
                    self.state["unsafe_version"] = version
                    raise IncompleteError("safety_failure")
                return False
        return True

    def execution_tick(self):
        from lerobot.data_platform.ai_tools.followups import _acknowledged
        from lerobot.data_platform.ai_tools.strategy_fixtures import validate_fixture, verify_output
        from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun

        # Six real execution scenarios, including v3 shared-shard subset preservation.
        # Execution contracts stay fixed when an administrator rotates the evaluation suite.
        cases = initial_cases()["development"]
        selected = [cases[n] for n in (0, 1, 2, 3, 5, 9)]
        selected[2] = {**selected[2], "fixture": "v2.1-video"}
        runs = self.state["runs"]
        if len(runs) == 12 and all(r.get("verified") for r in runs):
            return True
        if not runs or runs[-1].get("verified"):
            if len(runs) >= min(12, self.config["budget"]["pipelines"]):
                raise IncompleteError("pipeline_budget")
            index = len(runs)
            case = selected[index // 2]
            version = self.baseline if index % 2 == 0 else self.state["winner"]
            metric, result = self.evaluate(case, version)
            if not metric.correct or not metric.safe:
                raise IncompleteError("execution_plan_failed")
            proposal = result.get("action") or result.get("proposal")
            entry = {
                "case": case,
                "definition": proposal["definition"],
                "version_id": version,
                "delivery": "strategy:" + self.key + ":" + str(index),
                "run_id": None,
                "verified": False,
            }
            runs.append(entry)
            self.persist()
        entry = runs[-1]
        case = entry["case"]
        fixture = case.get("fixture", case["format"])
        owner, location_id = self.config["executor_id"], self.config["locations"][fixture]
        with self.store.sessions() as session:
            source, _ = self.manager._location(session, location_id, owner)
        if validate_fixture(source["root"], fixture) != self.config["fixtures"][fixture]:
            raise IncompleteError("source_changed")
        if not entry["run_id"]:
            saved = self.manager.save(owner, entry["definition"], delivery=entry["delivery"])
            run = self.manager.start(
                owner,
                saved["pipeline_id"],
                saved["revision"],
                location_id,
                entry["delivery"],
                expected_identity=location_identity(source),
                admission_guard=self.guard,
            )
            entry["run_id"] = run["run_id"]
            self.persist()
        # Coordinator/Agent perform work; this worker never executes a dataset transform itself.
        run = self.manager.run(owner, entry["run_id"])
        if run["status"] in {"queued", "running"}:
            now = time.time()
            if run["status"] == "running" and entry.get("last_checked"):
                self.state["seconds"] += max(0, now - entry["last_checked"])
            entry["last_checked"] = now
            return False
        if run["status"] != "done":
            raise IncompleteError("test_pipeline_not_completed")
        with self.store.sessions() as session:
            row = session.get(PipelineRun, entry["run_id"])
            steps = session.scalars(
                select(PipelineStepRun)
                .where(PipelineStepRun.run_id == row.run_id)
                .order_by(PipelineStepRun.position)
            ).all()
            if not _acknowledged(session, row, steps):
                return False
            expected_episodes, expected_format = [0, 1], case["format"]
            checks = []
            from lerobot.data_platform.operators import get_operator

            for step, definition in zip(steps, entry["definition"]["steps"], strict=True):
                op = definition["operator_id"]
                if op == "preprocess.split":
                    start, end = map(int, definition["parameters"]["episode_range"].split(":"))
                    expected_episodes = expected_episodes[start:end]
                if op == "preprocess.convert_v3":
                    expected_format = "v3.0"
                if get_operator(op).output_type == "dataset":
                    if not step.output_location_id:
                        raise IncompleteError("output_not_registered")
                    output, _ = self.manager._location(session, step.output_location_id, owner, write=False)
                    try:
                        check = verify_output(
                            output["root"],
                            source["root"],
                            expected_episodes=expected_episodes,
                            expected_format=expected_format,
                        )
                    except (KeyError, ValueError) as exc:
                        self.state["unsafe_version"] = entry["version_id"]
                        raise IncompleteError("data_integrity_failure") from exc
                    checks.append(check)
        try:
            validate_fixture(source["root"], fixture)
        except ValueError as exc:
            self.state["unsafe_version"] = entry["version_id"]
            raise IncompleteError("source_integrity_failure") from exc
        entry.update(verified=True, checks=checks, acknowledged=True)
        return False

    def step(self):
        # Recheck executor grants, immutable fixtures and authorization after every lease acquisition.
        current_config = self.strategies.configuration(
            {
                k: self.config[k]
                for k in ("automatic", "paused", "executor_id", "node_id", "locations", "budget")
            },
            self.config["administrator"],
        )
        if current_config != {k: v for k, v in self.config.items() if k != "compatibility"}:
            raise IncompleteError("fixture_or_authorization_changed")
        if self.state.get("inflight"):
            raise IncompleteError("unknown_model_outcome")
        if self.status in {"queued", "generating"}:
            if len(self.state["candidates"]) >= 2:
                return "development"
            self.generate_candidate()
            return "development" if len(self.state["candidates"]) >= 2 else "generating"
        if self.status == "development":
            variants = [("baseline", self.baseline)] + [(key, key) for key in self.state["candidates"]]
            if self.measurement_tick("development", variants):
                winner = max(
                    self.state["candidates"],
                    key=lambda key: (
                        sum(m["correct"] for m in self.state["measurements"]["development:" + key]),
                        -sum(m["calls"] for m in self.state["measurements"]["development:" + key]),
                        -sum(m["tokens"] for m in self.state["measurements"]["development:" + key]),
                    ),
                )
                self.state["winner"] = winner
                return "holdout"
        elif self.status == "holdout":
            winner = self.state["winner"]
            if self.measurement_tick("holdout", [("baseline", self.baseline), (winner, winner)]):
                self.state["gate"] = promotion_gate(
                    [Measurement(**m) for m in self.state["measurements"]["holdout:baseline"]],
                    [Measurement(**m) for m in self.state["measurements"]["holdout:" + winner]],
                )
                return "execution" if self.state["gate"]["eligible"] else "rejected"
        elif self.status == "execution" and self.execution_tick():
            return "passed"
        return self.status


def tick(assistant):
    if not experiments_enabled():
        return False
    store, strategies = assistant.store, Strategies(assistant)
    install_suite(store)
    with store.sessions() as session:
        binding = session.get(StrategyBinding, "shared")
        config = binding.configuration if binding else {}
        revision = binding.revision if binding else 0
    if not config or config["paused"]:
        return False
    with store.sessions.begin() as session:
        scheduler_lock(session)
        binding = session.get(StrategyBinding, "shared")
        collect_now = time.time() - (binding.last_collection_at or 0) >= 60
        if collect_now:
            binding.last_collection_at = int(time.time())
    if collect_now:
        collect(assistant.manager, config["administrator"], excluded_owner=config["executor_id"])
    token = str(uuid.uuid4())
    with store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.scalar(
            select(StrategyExperiment)
            .where(StrategyExperiment.status.in_(RUNNING), StrategyExperiment.lease_until <= time.time())
            .order_by(StrategyExperiment.created_at)
            .limit(1)
        )
        if row is not None:
            row.lease_owner, row.lease_until = token, time.time() + 300
            key = row.experiment_id
        else:
            key = None
    if key is None:
        if config["automatic"] and os.getenv("DATA_PLATFORM_ENV") == "dev":
            with store.sessions() as session:
                suite = session.scalar(
                    select(StrategySuite.suite_id)
                    .where(StrategySuite.reviewed_by.is_not(None), StrategySuite.experiment_id.is_(None))
                    .limit(1)
                )
            if suite:
                with suppress(JobConflictError):
                    strategies.start(config["administrator"], revision, suite, automatic=True)
        return False
    worker = Experiment(assistant, key, token)
    status, reason = worker.status, None
    try:
        status = worker.step()
    except IncompleteError as exc:
        status, reason = "incomplete", str(exc)
    except (PermissionError, KeyError, ValueError, TypeError, JobConflictError):
        status, reason = "incomplete", "validation_or_authorization_failed"
    except Exception:
        # No provider bodies, private parameters or database exception text in logs.
        status, reason = "incomplete", "external_failure"
    with store.sessions.begin() as session:
        scheduler_lock(session)
        row = session.get(StrategyExperiment, key)
        if row.lease_owner != token or row.lease_until <= time.time() or row.status not in RUNNING:
            return True
        worker.state["seconds"] += time.monotonic() - worker.started
        if reason:
            worker.state["reason"] = reason
        row.state, row.status = worker.state, status
        row.lease_owner, row.lease_until = "", 0
        row.revision += 1
        row.updated_at = int(time.time())
        if worker.state.get("unsafe_version"):
            unsafe = session.get(StrategyVersion, worker.state["unsafe_version"])
            if unsafe:
                unsafe.revoked = True
                binding = session.get(StrategyBinding, "shared")
                if binding.active_id == unsafe.version_id:
                    binding.active_id = None
                    binding.revision += 1
                assistant.manager._audit(
                    session,
                    config["administrator"],
                    "strategy.invalidated",
                    version_id=unsafe.version_id,
                    reason=reason,
                )
        if status not in RUNNING:
            assistant.manager._audit(
                session,
                config["administrator"],
                "strategy.experiment.finished",
                experiment_id=key,
                status=status,
                reason=reason,
            )
    return True


def main():
    from lerobot.data_platform.ai_tools.service import AssistantService
    from lerobot.data_platform.processing.coordinator import configured_manager

    service = AssistantService(configured_manager())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            tick(service)
        except Exception:
            logging.warning("Strategy worker unavailable; existing requests and jobs are unaffected")
        stop.wait(5)


if __name__ == "__main__":
    main()
