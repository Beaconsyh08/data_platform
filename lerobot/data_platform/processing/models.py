"""Additive control-plane persistence for immutable plans and restartable runs."""

from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.management_storage import ManagementBase


class Pipeline(ManagementBase):
    __tablename__ = "dp_pipelines"
    pipeline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(Float)


class PipelineVersion(ManagementBase):
    __tablename__ = "dp_pipeline_versions"
    __table_args__ = (UniqueConstraint("pipeline_id", "revision"),)
    version_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class PipelineRun(ManagementBase):
    __tablename__ = "dp_pipeline_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    version_id: Mapped[str] = mapped_column(String(80))
    node_id: Mapped[str] = mapped_column(String(36))
    location_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    input_identity: Mapped[dict] = mapped_column(JSON)
    pending_action: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class PipelineStepRun(ManagementBase):
    __tablename__ = "dp_pipeline_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_id"),)
    row_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    step_id: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(24))
    input_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_identity: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
