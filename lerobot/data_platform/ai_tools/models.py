"""Bounded conversations and idempotent model requests in the control database."""

from sqlalchemy import JSON, BigInteger, Integer, String
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
