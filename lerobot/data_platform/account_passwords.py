"""Account password recovery, transactional session revocation, and schema migration."""

import secrets
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from lerobot.data_platform.control_plane import (
    Base,
    ControlPlaneSession,
    ControlPlaneUser,
    DevRoleSession,
    _password_digest,
    _password_matches,
    _token_digest,
    _utcnow,
    _validate_password,
)


class PasswordReset(Base):
    __tablename__ = "dp_password_resets"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("dp_users.user_id"), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class PasswordRateLimit(Base):
    __tablename__ = "dp_password_rate_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


def migrate_accounts(engine):
    """Remove the obsolete display name only during explicit schema initialization."""
    with engine.begin() as conn:
        if "display_name" in {column["name"] for column in inspect(conn).get_columns("dp_users")}:
            conn.execute(text("ALTER TABLE dp_users DROP COLUMN display_name"))


def allow_attempt(store, key: str, *, limit: int = 10) -> bool:
    now = _utcnow()
    # Shared database counters cover all web workers; only hashed keys are persisted.
    bucket = int(now.timestamp()) // 300
    digest = _token_digest(f"{key}:{bucket}")
    with store.sessions.begin() as session:
        session.execute(delete(PasswordRateLimit).where(PasswordRateLimit.expires_at < now))
        try:
            with session.begin_nested():
                session.add(PasswordRateLimit(key=digest, count=0, expires_at=now + timedelta(minutes=10)))
                session.flush()
        except IntegrityError:
            pass
        result = session.execute(
            update(PasswordRateLimit)
            .where(PasswordRateLimit.key == digest, PasswordRateLimit.count < limit)
            .values(count=PasswordRateLimit.count + 1)
        )
        return result.rowcount == 1


def _lock_user(session, user_id):
    result = session.execute(
        update(ControlPlaneUser)
        .where(ControlPlaneUser.user_id == user_id)
        .values(updated_at=ControlPlaneUser.updated_at)
    )
    if result.rowcount != 1:
        raise KeyError(user_id)
    return session.get(ControlPlaneUser, user_id, populate_existing=True)


def _audit(session, operation, user_id, actor):
    from lerobot.data_platform.management_storage import enqueue_event
    from lerobot.data_platform.operation_log import build_operation_event

    enqueue_event(
        session,
        build_operation_event(
            operation,
            status="success",
            source="control-plane",
            actor={key: actor[key] for key in ("user_id", "username", "role")}
            if actor
            else {"kind": "password-reset"},
            details={"target_user_id": user_id},
        ),
    )


def issue_reset(store, user_id, actor):
    token = secrets.token_urlsafe(32)
    with store.sessions.begin() as session:
        _lock_user(session, user_id)
        session.execute(delete(PasswordReset).where(PasswordReset.user_id == user_id))
        session.add(
            PasswordReset(
                user_id=user_id,
                token_digest=_token_digest(token),
                expires_at=_utcnow() + timedelta(minutes=15),
            )
        )
        _audit(session, "account.password_reset_issued", user_id, actor)
    return token


def _replace_password(session, user, password):
    user.password_digest = _password_digest(password)
    user.updated_at = _utcnow()
    ids = select(ControlPlaneSession.session_id).where(ControlPlaneSession.user_id == user.user_id)
    session.execute(delete(DevRoleSession).where(DevRoleSession.session_id.in_(ids)))
    session.execute(delete(ControlPlaneSession).where(ControlPlaneSession.user_id == user.user_id))
    session.execute(delete(PasswordReset).where(PasswordReset.user_id == user.user_id))


def change_password(store, user_id, current_password, new_password, actor):
    new_password = _validate_password(new_password)
    with store.sessions.begin() as session:
        user = _lock_user(session, user_id)
        if not user.active or not _password_matches(user.password_digest, current_password):
            raise PermissionError("Current password is incorrect")
        _replace_password(session, user, new_password)
        _audit(session, "account.password_changed", user_id, actor)


def redeem_reset(store, token, new_password):
    new_password = _validate_password(new_password)
    digest = _token_digest(token)
    with store.sessions.begin() as session:
        user_id = session.scalar(select(PasswordReset.user_id).where(PasswordReset.token_digest == digest))
        if user_id is None:
            raise ValueError("Reset link is invalid or expired")
        user = _lock_user(session, user_id)
        # Locking reads also see the latest committed token under MySQL REPEATABLE READ.
        reset = session.scalar(
            select(PasswordReset)
            .where(PasswordReset.user_id == user_id, PasswordReset.token_digest == digest)
            .with_for_update()
        )
        if reset is None or reset.expires_at <= _utcnow():
            raise ValueError("Reset link is invalid or expired")
        _replace_password(session, user, new_password)
        _audit(session, "account.password_reset_completed", user_id, None)
