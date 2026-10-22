"""Bounded conversations and idempotent model requests in the control database."""

from sqlalchemy import JSON, BigInteger, Double, Integer, LargeBinary, String
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.management_storage import ManagementBase


class AssistantConversation(ManagementBase):
    __tablename__ = "dp_ai_conversations"
    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    location_id: Mapped[str] = mapped_column(String(36))
    input_identity: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    history: Mapped[list] = mapped_column(JSON, default=list)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, index=True)


class AssistantTurn(ManagementBase):
    __tablename__ = "dp_ai_turns"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    lease_until: Mapped[int] = mapped_column(BigInteger)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class AssistantPreferences(ManagementBase):
    """Explicit, account-private settings; never model-written instructions."""

    __tablename__ = "dp_ai_preferences"
    owner: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class AssistantExecution(ManagementBase):
    """One durable generation per admitted Turn; never an operation approval."""

    __tablename__ = "dp_ai_runtime_executions"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    conversation_revision: Mapped[int] = mapped_column(Integer)
    backend: Mapped[str] = mapped_column(String(16), default="langgraph")
    graph_version: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    model: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    queue_until: Mapped[float] = mapped_column(Double)
    deadline: Mapped[float | None] = mapped_column(Double, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_until: Mapped[float] = mapped_column(Double, default=0)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)


class AssistantModelCall(ManagementBase):
    __tablename__ = "dp_ai_runtime_calls"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    call_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)


class AssistantCheckpoint(ManagementBase):
    __tablename__ = "dp_ai_runtime_checkpoints"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB(), "mysql"))
    details: Mapped[dict] = mapped_column(JSON)


class AssistantCheckpointWrite(ManagementBase):
    __tablename__ = "dp_ai_runtime_writes"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB(), "mysql"))


class AssistantMemory(ManagementBase):
    """Explicit bounded notes, independent of the short conversation retention window."""

    __tablename__ = "dp_ai_memory"
    owner: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class AssistantFollowup(ManagementBase):
    """One durable observation per explicitly confirmed action, never an execution request."""

    __tablename__ = "dp_ai_followups"
    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    location_id: Mapped[str] = mapped_column(String(36))
    input_identity: Mapped[dict] = mapped_column(JSON)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    next_check_at: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, index=True)
