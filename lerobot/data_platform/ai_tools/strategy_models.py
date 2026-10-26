"""Additive strategy control records; private conversations are not shared evidence."""

from sqlalchemy import JSON, BigInteger, Boolean, Double, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.management_storage import ManagementBase


class StrategyEvidence(ManagementBase):
    __tablename__ = "dp_strategy_evidence"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    administrator: Mapped[str] = mapped_column(String(36))
    source_digest: Mapped[str] = mapped_column(String(64))
    projection: Mapped[dict] = mapped_column(JSON)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(BigInteger)


class StrategyVersion(ManagementBase):
    __tablename__ = "dp_strategy_versions"
    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    digest: Mapped[str] = mapped_column(String(64))
    specification: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON)
    compatibility: Mapped[dict] = mapped_column(JSON)
    experiment_id: Mapped[str] = mapped_column(String(36), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger)


class StrategySuite(ManagementBase):
    __tablename__ = "dp_strategy_suites"
    suite_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    cases: Mapped[dict] = mapped_column(JSON)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # A holdout is consumed once, even when the candidate fails.
    experiment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)


class StrategyExperiment(ManagementBase):
    __tablename__ = "dp_strategy_experiments"
    experiment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    configuration: Mapped[dict] = mapped_column(JSON)
    baseline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    suite_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_owner: Mapped[str] = mapped_column(String(36), default="")
    lease_until: Mapped[float] = mapped_column(Double, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class StrategyBinding(ManagementBase):
    __tablename__ = "dp_strategy_bindings"
    binding_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    active_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    last_collection_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_automatic_at: Mapped[int] = mapped_column(BigInteger, default=0)
    evidence_cursor: Mapped[int] = mapped_column(BigInteger, default=0)
