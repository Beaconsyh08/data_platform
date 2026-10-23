"""Admin-reviewed shared strategies; execution authority stays in PipelineManager."""

from __future__ import annotations

import os
import time
import uuid

from sqlalchemy import select, update

from lerobot.data_platform.ai_tools.strategy_contracts import (
    Measurement,
    compatibility,
    promotion_gate,
    validate_strategy,
)
from lerobot.data_platform.ai_tools.strategy_evidence import validate_sources
from lerobot.data_platform.ai_tools.strategy_models import (
    StrategyBinding,
    StrategyEvidence,
    StrategyExperiment,
    StrategySuite,
    StrategyVersion,
)
from lerobot.data_platform.control_plane import DataMutationScope
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.task_catalog import content_digest

DEFAULT_BUDGET = {"calls": 1000, "tokens": 1000000, "seconds": 14400, "pipelines": 12}
RUNNING = {"queued", "generating", "development", "holdout", "execution"}


def enabled():
    return os.getenv("DATA_PLATFORM_STRATEGIES_ENABLED", "0") == "1"


def experiments_enabled():
    """Non-dev acceptance is an explicit deployment configuration, never a model decision."""
    return enabled() and (
        os.getenv("DATA_PLATFORM_ENV") == "dev"
        or os.getenv("DATA_PLATFORM_STRATEGY_EXPERIMENTS_ENABLED", "0") == "1"
    )


class Strategies:
    def __init__(self, assistant):
        self.assistant = assistant
        self.manager, self.store = assistant.manager, assistant.store

    def admin(self, session, owner):
        if self.manager._actor(session, owner, write=False).role != "admin":
            raise PermissionError("An active administrator is required")

    @staticmethod
    def binding(session):
        row = session.get(StrategyBinding, "shared")
        if row is None:
            row = StrategyBinding(
                binding_id="shared", revision=0, configuration={}, last_automatic_at=0, evidence_cursor=0
            )
            session.add(row)
            session.flush()
        return row

    def edit(self, session, owner, expected):
        scheduler_lock(session)
        self.admin(session, owner)
        row = self.binding(session)
        if type(expected) is not int or expected != row.revision:
            raise JobConflictError("Strategies changed. Reload before editing.")
        row.revision += 1
        return row

    def configuration(self, value, owner):
        from lerobot.data_platform.ai_tools.strategy_fixtures import validate_fixture

        keys = {"automatic", "paused", "executor_id", "node_id", "locations", "budget"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Experiment configuration fields are required")
        if type(value["automatic"]) is not bool or type(value["paused"]) is not bool:
            raise ValueError("Experiment switches must be booleans")
        if not isinstance(value["budget"], dict) or set(value["budget"]) != set(DEFAULT_BUDGET):
            raise ValueError("Explicit experiment budgets are required")
        if any(
            type(n) is not int or not 1 <= n <= DEFAULT_BUDGET[k] * 10 for k, n in value["budget"].items()
        ):
            raise ValueError("Invalid experiment budget")
        if not isinstance(value["locations"], dict) or set(value["locations"]) != {
            "v2.1",
            "v3.0",
            "v2.1-video",
        }:
            raise ValueError("Three generated synthetic fixture locations are required")
        with self.store.sessions() as session:
            self.admin(session, owner)
            executor = self.manager._actor(session, value["executor_id"])
            if executor.role != "operator" or executor.user_id == owner:
                raise ValueError("Use a dedicated restricted operator account")
            scope = session.get(DataMutationScope, executor.user_id)
            accessible = self.store._accessible_location_ids(session, executor)
            if (
                not scope
                or scope.all_locations
                or accessible is None
                or not set(value["locations"].values()) <= accessible
            ):
                raise ValueError("The experiment operator must have explicit dataset grants")
            # Other grants are permitted only for outputs from this operator's own test runs.
            from lerobot.data_platform.processing.models import PipelineRun, PipelineStepRun

            recorded_runs = session.scalars(
                select(StrategyExperiment.state["runs"])
                .where(StrategyExperiment.configuration["executor_id"].as_string() == executor.user_id)
                .order_by(StrategyExperiment.created_at.desc())
                .limit(200)
            ).all()
            experiment_runs = {
                entry["run_id"]
                for runs in recorded_runs
                if isinstance(runs, list)
                for entry in runs
                if entry.get("run_id")
            }
            outputs = set(
                session.scalars(
                    select(PipelineStepRun.output_location_id)
                    .join(PipelineRun, PipelineStepRun.run_id == PipelineRun.run_id)
                    .where(PipelineRun.owner == executor.user_id, PipelineRun.run_id.in_(experiment_runs))
                )
            )
            if accessible - set(value["locations"].values()) - outputs:
                raise ValueError("Experiment operator can access non-test data")
            fixtures = {}
            for fmt, key in value["locations"].items():
                location, _ = self.manager._location(session, key, executor.user_id, node_id=value["node_id"])
                self.manager._location(session, key, owner, write=False)
                fixtures[fmt] = validate_fixture(location["root"], fmt)
        return {**value, "administrator": owner, "fixtures": fixtures}

    def configure(self, owner, expected, value):
        config = self.configuration(value, owner)
        with self.store.sessions.begin() as session:
            row = self.edit(session, owner, expected)
            row.configuration = config
            self.manager._audit(session, owner, "strategy.configured", revision=row.revision)
        return self.dashboard(owner)

    def review_suite(self, owner, expected, suite_id):
        with self.store.sessions.begin() as session:
            self.edit(session, owner, expected)
            suite = session.get(StrategySuite, suite_id)
            if not self.suite_available(session, suite):
                raise JobConflictError("Choose an unused evaluation suite")
            if content_digest(suite.cases) != suite.digest:
                raise JobConflictError("Evaluation suite changed")
            suite.reviewed_by = owner
            self.manager._audit(
                session, owner, "strategy.suite.reviewed", suite_id=suite_id, digest=suite.digest
            )
        return self.dashboard(owner)

    @staticmethod
    def suite_available(session, suite):
        if suite is None:
            return False
        if not suite.experiment_id:
            return True
        previous = session.get(StrategyExperiment, suite.experiment_id)
        return bool(
            previous
            and previous.status in {"incomplete", "cancelled"}
            and not previous.state.get("winner")
            and not any(key.startswith("holdout:") for key in previous.state.get("measurements", {}))
        )

    def start(self, owner, expected, suite_id, *, automatic=False):
        if not experiments_enabled():
            raise JobConflictError("Strategy experiments are disabled in this environment")
        if automatic and os.getenv("DATA_PLATFORM_ENV") != "dev":
            raise JobConflictError("Automatic experiments are restricted to development")
        with self.store.sessions.begin() as session:
            row = self.edit(session, owner, expected)
            config = row.configuration
            if not config or config["paused"]:
                raise JobConflictError("Configure and unpause experiments first")
            if session.scalar(
                select(StrategyExperiment.experiment_id)
                .where(StrategyExperiment.status.in_(RUNNING))
                .limit(1)
            ):
                raise JobConflictError("An experiment is already running")
            suite = session.get(StrategySuite, suite_id)
            if not self.suite_available(session, suite) or not suite.reviewed_by:
                raise JobConflictError("A reviewed, unused evaluation suite is required")
            self.admin(session, suite.reviewed_by)
            evidence = list(
                session.scalars(
                    select(StrategyEvidence)
                    .where(StrategyEvidence.valid.is_(True))
                    .order_by(StrategyEvidence.created_at.desc())
                    .limit(100)
                )
            )
            refs = [{"run_id": r.run_id, "digest": r.source_digest} for r in evidence]
            validate_sources(self.manager, session, refs)
            now = int(time.time())
            if automatic and (
                not config["automatic"]
                or now - row.last_automatic_at < 86400
                or sum(r.created_at > row.evidence_cursor for r in evidence) < 10
            ):
                raise JobConflictError("Automatic experiment threshold has not been reached")
            key = str(uuid.uuid4())
            suite.experiment_id = key
            session.add(
                StrategyExperiment(
                    experiment_id=key,
                    status="queued",
                    revision=0,
                    configuration={**config, "compatibility": compatibility(self.assistant.config.model)},
                    baseline_id=row.active_id,
                    suite_id=suite_id,
                    created_at=now,
                    updated_at=now,
                    state={
                        "evidence": refs,
                        "candidates": [],
                        "measurements": {},
                        "calls": 0,
                        "tokens": 0,
                        "seconds": 0,
                        "runs": [],
                        "inflight": None,
                    },
                )
            )
            if automatic:
                row.last_automatic_at = now
            row.evidence_cursor = now
            self.manager._audit(session, owner, "strategy.experiment.started", experiment_id=key)
        return {"experiment_id": key, **self.dashboard(owner)}

    def valid_version(self, session, key, *, check_enabled=True):
        if check_enabled and not enabled():
            raise JobConflictError("Shared strategies are disabled")
        row = session.get(StrategyVersion, key)
        if not row or row.revoked:
            raise JobConflictError("Strategy was disabled or removed")
        spec = validate_strategy(row.specification)
        if content_digest(spec) != row.digest or row.compatibility != compatibility(
            self.assistant.config.model
        ):
            raise JobConflictError("Strategy is incompatible with this runtime")
        validate_sources(self.manager, session, row.evidence)
        return row

    def activate(self, owner, expected, version_id):
        with self.store.sessions.begin() as session:
            binding = self.edit(session, owner, expected)
            row = self.valid_version(session, version_id)
            experiment = session.get(StrategyExperiment, row.experiment_id)
            state = experiment.state if experiment else {}
            if not experiment or experiment.status != "passed" or state.get("winner") != version_id:
                raise JobConflictError("Strategy has not passed its experiment")
            metrics = state.get("measurements", {})
            gate = promotion_gate(
                [Measurement(**m) for m in metrics.get("holdout:baseline", [])],
                [Measurement(**m) for m in metrics.get("holdout:" + version_id, [])],
            )
            if (
                not gate["eligible"]
                or len(state.get("runs", [])) != 12
                or not all(r.get("verified") for r in state["runs"])
            ):
                raise JobConflictError("Complete evaluation and output verification are required")
            binding.active_id = version_id
            self.manager._audit(
                session, owner, "strategy.activated", version_id=version_id, revision=binding.revision
            )
        return self.dashboard(owner)

    def disable(self, owner, expected, version_id):
        with self.store.sessions.begin() as session:
            binding = self.edit(session, owner, expected)
            row = session.get(StrategyVersion, version_id)
            if row is None:
                raise KeyError("Strategy not found")
            row.revoked = True
            if binding.active_id == version_id:
                binding.active_id = None
            self.manager._audit(session, owner, "strategy.disabled", version_id=version_id)
        return self.dashboard(owner)

    def cancel(self, owner, expected, experiment_id):
        with self.store.sessions.begin() as session:
            self.edit(session, owner, expected)
            experiment = session.get(StrategyExperiment, experiment_id)
            if experiment is None:
                raise KeyError("Experiment not found")
            if experiment.status in RUNNING:
                experiment.status = "cancelled"
                experiment.revision += 1
            self.manager._audit(session, owner, "strategy.experiment.cancelled", experiment_id=experiment_id)
        # Existing test runs retain their own ordinary cancellation and finalization controls.
        return self.dashboard(owner)

    def dashboard(self, owner):
        with self.store.sessions() as session:
            self.admin(session, owner)
            binding = session.get(StrategyBinding, "shared")
            versions = session.scalars(
                select(StrategyVersion).order_by(StrategyVersion.created_at.desc()).limit(50)
            ).all()
            experiments = session.scalars(
                select(StrategyExperiment).order_by(StrategyExperiment.created_at.desc()).limit(20)
            ).all()
            suites = session.scalars(
                select(StrategySuite).order_by(StrategySuite.created_at.desc()).limit(20)
            ).all()
            return {
                "enabled": enabled(),
                "experiments_enabled": experiments_enabled(),
                "revision": binding.revision if binding else 0,
                "active_id": binding.active_id if binding else None,
                "configuration": binding.configuration if binding else {},
                "versions": [
                    {
                        "version_id": v.version_id,
                        "parent_id": v.parent_id,
                        "digest": v.digest,
                        "specification": v.specification,
                        "revoked": v.revoked,
                        "experiment_id": v.experiment_id,
                    }
                    for v in versions
                ],
                "experiments": [
                    {"experiment_id": e.experiment_id, "status": e.status, "state": e.state}
                    for e in experiments
                ],
                "suites": [
                    {
                        "suite_id": s.suite_id,
                        "digest": s.digest,
                        "reviewed": bool(s.reviewed_by),
                        "consumed": not self.suite_available(session, s),
                        "cases": s.cases
                        if self.suite_available(session, s)
                        else {"development": s.cases["development"]},
                    }
                    for s in suites
                ],
            }

    def select(self, owner, location_id):
        if not enabled():
            return None
        with self.store.sessions.begin() as session:
            binding = session.get(StrategyBinding, "shared")
            if not binding or not binding.active_id:
                return None
            selected_id = binding.active_id
            try:
                row = self.valid_version(session, selected_id)
            except (PermissionError, JobConflictError, ValueError):
                row = session.get(StrategyVersion, selected_id)
                if row:
                    row.revoked = True
                # A concurrent administrator may already have selected another version.
                session.execute(
                    update(StrategyBinding)
                    .where(StrategyBinding.binding_id == "shared", StrategyBinding.active_id == selected_id)
                    .values(active_id=None, revision=StrategyBinding.revision + 1)
                    .execution_options(synchronize_session=False)
                )
                self.manager._audit(
                    session, owner, "strategy.invalidated", reason="evidence_or_runtime_changed"
                )
                return None
            location, node = self.manager._location(session, location_id, owner, write=False)
            if self.manager._actor(session, owner, write=False).role == "viewer":
                return None
            from lerobot.data_platform.operators import get_operator
            from lerobot.data_platform.processing.planner import availability

            info = self.manager._info(location)
            templates = [
                t
                for t in row.specification["templates"]
                if info.get("codebase_version") in t["formats"]
                and set(t["required_features"]) <= set(info.get("features", {}))
                and all(
                    availability(get_operator(s["operator_id"], s["version"]), node, info) is None
                    for s in t["steps"]
                )
            ]
            return (
                {"version_id": row.version_id, "digest": row.digest, "templates": templates[:5]}
                if templates
                else None
            )

    def check(self, session, snapshot):
        if snapshot:
            row = self.valid_version(session, snapshot["version_id"])
            if row.digest != snapshot["digest"]:
                raise JobConflictError("Strategy snapshot changed")


def public_summary(snapshot):
    return {k: snapshot[k] for k in ("version_id", "digest")} if snapshot else None
