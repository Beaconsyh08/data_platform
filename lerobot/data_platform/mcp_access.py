"""Account-bound MCP credentials; plaintext credentials are returned only at creation."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import JSON, BigInteger, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.control_plane import ControlPlaneUser
from lerobot.data_platform.job_management import scheduler_lock
from lerobot.data_platform.management_storage import ManagementBase
from lerobot.data_platform.processing.service import PipelineManager


class McpCredential(ManagementBase):
    __tablename__ = "dp_mcp_credentials"
    credential_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(80))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    password_revision: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int] = mapped_column(BigInteger, default=0)


@dataclass(frozen=True)
class McpIdentity:
    owner: str
    credential_id: str
    scopes: frozenset[str]
    role: str


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _view(row):
    return {
        k: getattr(row, k)
        for k in ("credential_id", "name", "scopes", "created_at", "expires_at", "revoked_at")
    }


def credentials(store, owner, *, create=None, revoke=None):
    manager = PipelineManager(store)
    with store.sessions.begin() as session:
        scheduler_lock(session)
        actor = manager._actor(session, owner, write=False)
        if revoke is not None:
            row = session.get(McpCredential, revoke)
            if row is None or row.owner != owner:
                raise KeyError("Credential not found")
            row.revoked_at = int(time.time())
            manager._audit(session, owner, "mcp.credential_revoked", credential_id=row.credential_id)
            return {"revoked": True}
        if create is None:
            return {
                "credentials": [
                    _view(row)
                    for row in session.scalars(
                        select(McpCredential)
                        .where(McpCredential.owner == owner)
                        .order_by(McpCredential.created_at.desc())
                        .limit(100)
                    )
                ]
            }
        if not isinstance(create, dict) or set(create) - {"name", "days", "scopes"}:
            raise ValueError("Invalid credential request")
        name, days, scopes = create.get("name"), create.get("days", 30), create.get("scopes", ["read"])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError("A short credential name is required")
        if type(days) is not int or not 1 <= days <= 90:
            raise ValueError("Credential lifetime must be 1–90 days")
        if not isinstance(scopes, list) or not scopes or any(s not in ("read", "prepare") for s in scopes):
            raise ValueError("Allowed scopes are read and prepare")
        if "prepare" in scopes and actor.role == "viewer":
            raise PermissionError("Viewer accounts cannot prepare operations")
        now = int(time.time())
        active = session.scalar(
            select(func.count())
            .select_from(McpCredential)
            .where(
                McpCredential.owner == owner, McpCredential.revoked_at == 0, McpCredential.expires_at > now
            )
        )
        if active >= 20:
            raise ValueError("Revoke an existing credential before creating another")
        token = "dpmcp_" + secrets.token_urlsafe(32)
        row = McpCredential(
            credential_id=str(uuid.uuid4()),
            owner=owner,
            name=name.strip(),
            digest=_digest(token),
            password_revision=_digest(actor.password_digest),
            scopes=sorted(set(scopes) | {"read"}),
            created_at=now,
            expires_at=now + days * 86400,
            revoked_at=0,
        )
        session.add(row)
        manager._audit(
            session,
            owner,
            "mcp.credential_created",
            credential_id=row.credential_id,
            scopes=row.scopes,
            expires_at=row.expires_at,
        )
        return {**_view(row), "token": token}


def authenticate(store, token):
    if not isinstance(token, str) or not token.startswith("dpmcp_") or len(token) > 128:
        return None
    with store.sessions() as session:
        row = session.scalar(select(McpCredential).where(McpCredential.digest == _digest(token)))
        if row is None or row.revoked_at or row.expires_at <= time.time():
            return None
        actor = session.get(ControlPlaneUser, row.owner)
        if not actor or not actor.active or row.password_revision != _digest(actor.password_digest):
            return None
        return McpIdentity(row.owner, row.credential_id, frozenset(row.scopes), actor.role)
