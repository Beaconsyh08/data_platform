"""Persistent multi-user control plane for distributed Data Platform nodes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

USER_ROLES = {"admin", "data_manager", "operator", "viewer"}
SCOPED_SOURCE_OPERATIONS = {
    "mutation.value_edit",
    "mutation.delete_episodes",
    "mutation.repair_v3_video_timestamps",
}
JOB_STATES = {"queued", "running", "done", "error", "cancelled", "cancel_requested", "interrupted"}
_PASSWORD_ITERATIONS = 310_000
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def _token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _password_digest(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(_PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _password_matches(encoded: str, password: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_text = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        observed = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            base64.urlsafe_b64decode(salt_text),
            int(iterations_text),
        )
        expected = base64.urlsafe_b64decode(expected_text)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(observed, expected)


def _validate_password(password: str) -> str:
    value = str(password)
    if len(value) < 10:
        raise ValueError("password must contain at least 10 characters")
    if len(value) > 256:
        raise ValueError("password must contain at most 256 characters")
    if not value.strip():
        raise ValueError("password cannot contain only whitespace")
    return value


def _normalize_username(username: str) -> str:
    value = str(username or "").strip().lower()
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError(
            "username must contain 3-64 lowercase letters, numbers, dots, underscores, or hyphens"
        )
    return value


class Base(DeclarativeBase):
    pass


class ControlPlaneUser(Base):
    __tablename__ = "dp_users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_digest: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DataMutationGrant(Base):
    """Explicit source-write grants tied to one registered dataset location."""

    __tablename__ = "dp_data_mutation_grants"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_dataset_locations.location_id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    root: Mapped[str] = mapped_column(Text, nullable=False)


class ControlPlaneSession(Base):
    __tablename__ = "dp_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_users.user_id", ondelete="CASCADE"), index=True
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class DevTestUser(Base):
    __tablename__ = "dp_dev_test_users"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)


class DevRoleSession(Base):
    __tablename__ = "dp_dev_role_sessions"
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)


class ControlPlaneNode(Base):
    __tablename__ = "dp_nodes"

    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="offline", index=True)
    allowed_roots: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    writable_roots: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class DatasetLocation(Base):
    __tablename__ = "dp_dataset_locations"
    __table_args__ = (UniqueConstraint("node_id", "root_digest", name="uq_dp_location_node_root_digest"),)

    location_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_nodes.node_id", ondelete="CASCADE"), index=True
    )
    dataset_key: Mapped[str] = mapped_column(String(255), index=True)
    root: Mapped[str] = mapped_column(Text)
    root_digest: Mapped[str] = mapped_column(String(64))
    output_dir: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="available", index=True)
    details: Mapped[dict] = mapped_column("metadata_json", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class EpisodeDeletionRequest(Base):
    __tablename__ = "dp_episode_deletion_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    location_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    dataset_key: Mapped[str] = mapped_column(String(255), nullable=False)
    episodes: Mapped[list] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RemoteJob(Base):
    __tablename__ = "dp_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_nodes.node_id", ondelete="CASCADE"), index=True
    )
    location_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_dataset_locations.location_id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("dp_users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation: Mapped[str] = mapped_column(String(128), index=True)
    options: Mapped[dict] = mapped_column("options_json", JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    result: Mapped[dict] = mapped_column("result_json", JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RemoteJobEvent(Base):
    __tablename__ = "dp_job_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dp_jobs.job_id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(32), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column("payload_json", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class ControlPlaneStore:
    """MySQL-compatible persistence for accounts, nodes, locations, and remote jobs."""

    def __init__(self, database_url: str, *, initialize_schema: bool | None = None):
        url = str(database_url or "").strip()
        if not url:
            raise ValueError("database_url is required")
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        from lerobot.data_platform.environment import verify_database_environment

        verify_database_environment(self.engine, "control")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        from lerobot.data_platform.environment import EnvironmentIdentity

        if initialize_schema is None:
            initialize_schema = EnvironmentIdentity.from_env() is None
        if initialize_schema:
            from lerobot.data_platform.account_passwords import migrate_accounts

            Base.metadata.create_all(self.engine)
            migrate_accounts(self.engine)
        from lerobot.data_platform.job_management import JobManager
        from lerobot.data_platform.management_storage import migrate_management

        if initialize_schema:
            migrate_management(self.engine)
        self.job_manager = JobManager(self)
        from sqlalchemy import event, inspect

        from lerobot.data_platform.management_storage import enqueue_event
        from lerobot.data_platform.operation_log import build_operation_event

        def audit_accounts(session, flush_context, instances):
            from flask import g, has_request_context

            for account in list(session.new) + list(session.dirty):
                if not isinstance(account, ControlPlaneUser):
                    continue
                changed = account in session.new or any(
                    inspect(account).attrs[key].history.has_changes()
                    for key in ("role", "active", "password_digest")
                )
                if not changed:
                    continue
                actor = getattr(g, "control_plane_user", None) if has_request_context() else None
                enqueue_event(
                    session,
                    build_operation_event(
                        "account.updated",
                        status="success",
                        actor={key: actor[key] for key in ("user_id", "username", "role")}
                        if actor
                        else {"kind": "system"},
                        source="control-plane",
                        details={
                            "target_user_id": account.user_id,
                            "username": account.username,
                            "role": account.role,
                            "active": account.active,
                        },
                    ),
                )

        event.listen(self.sessions, "before_flush", audit_accounts)

    @staticmethod
    def source_mutation_allowed(
        session, user: ControlPlaneUser | None, location: DatasetLocation | None
    ) -> bool:
        if user is None or not user.active or location is None or location.state != "available":
            return False
        if user.role == "admin":
            return True
        if user.role != "data_manager":
            return False
        grant = session.get(DataMutationGrant, (user.user_id, location.location_id))
        return bool(grant and grant.node_id == location.node_id and grant.root == location.root)

    def granted_mutation_location_ids(self, user_id: str) -> list[str]:
        """Return saved scopes for administration, including disabled accounts."""
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(DataMutationGrant.location_id)
                    .where(DataMutationGrant.user_id == user_id)
                    .order_by(DataMutationGrant.location_id)
                )
            )

    def mutation_location_ids(self, user_id: str) -> list[str]:
        with self.sessions() as session:
            user = session.get(ControlPlaneUser, user_id)
            if user is None or not user.active or user.role not in {"admin", "data_manager"}:
                return []
            query = select(DatasetLocation.location_id).where(DatasetLocation.state == "available")
            if user.role == "data_manager":
                query = query.join(
                    DataMutationGrant, DataMutationGrant.location_id == DatasetLocation.location_id
                ).where(
                    DataMutationGrant.user_id == user_id,
                    DataMutationGrant.node_id == DatasetLocation.node_id,
                    DataMutationGrant.root == DatasetLocation.root,
                )
            return list(session.scalars(query))

    def set_mutation_locations(self, user_id: str, location_ids: list[str], *, actor: dict) -> list[str]:
        from lerobot.data_platform.job_management import scheduler_lock
        from lerobot.data_platform.management_storage import enqueue_event
        from lerobot.data_platform.operation_log import build_operation_event

        if (
            not isinstance(location_ids, list)
            or len(location_ids) > 1000
            or any(not isinstance(value, str) or not value for value in location_ids)
        ):
            raise ValueError("location_ids must be an array of at most 1000 registered location IDs")
        with self.sessions.begin() as session:
            scheduler_lock(session)
            administrator = session.get(ControlPlaneUser, actor["user_id"])
            if administrator is None or not administrator.active or administrator.role != "admin":
                raise PermissionError("Administrator role required")
            user = session.get(ControlPlaneUser, user_id)
            if user is None:
                raise KeyError(user_id)
            if user.role != "data_manager":
                raise ValueError("Source mutation scopes can only be assigned to data_manager accounts")
            locations = [session.get(DatasetLocation, key) for key in sorted(set(location_ids))]
            if any(location is None or location.state != "available" for location in locations):
                raise ValueError("Every scope must reference an available registered dataset location")
            previous = list(
                session.scalars(
                    select(DataMutationGrant.location_id).where(DataMutationGrant.user_id == user_id)
                )
            )
            session.execute(delete(DataMutationGrant).where(DataMutationGrant.user_id == user_id))
            for location in locations:
                session.add(
                    DataMutationGrant(
                        user_id=user_id,
                        location_id=location.location_id,
                        node_id=location.node_id,
                        root=location.root,
                    )
                )
            selected = [location.location_id for location in locations]
            enqueue_event(
                session,
                build_operation_event(
                    "account.data_scopes.updated",
                    status="success",
                    actor=actor,
                    source="control-plane",
                    details={
                        "target_user_id": user_id,
                        "previous_location_ids": previous,
                        "location_ids": selected,
                    },
                ),
            )
        return selected

    def user_count(self) -> int:
        with self.sessions() as session:
            return len(session.scalars(select(ControlPlaneUser.user_id)).all())

    def bootstrap_admin(
        self,
        *,
        username: str,
        password: str,
        display_name: str | None = None,
        bootstrap_token: str,
        expected_token: str,
    ) -> dict:
        if not expected_token:
            raise RuntimeError("DATA_PLATFORM_BOOTSTRAP_TOKEN is not configured")
        if not hmac.compare_digest(str(bootstrap_token), str(expected_token)):
            raise PermissionError("invalid bootstrap token")
        with self.sessions.begin() as session:
            if session.scalar(select(ControlPlaneUser.user_id).limit(1)) is not None:
                raise ValueError("the first administrator has already been created")
            user = self._new_user(username, password, "admin")
            session.add(user)
        return self._user_dict(user)

    def register_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str | None = None,
        role: str = "viewer",
        active: bool = True,
    ) -> dict:
        if role not in USER_ROLES:
            raise ValueError(f"unsupported role: {role}")
        with self.sessions.begin() as session:
            if session.scalar(select(ControlPlaneUser.user_id).limit(1)) is None:
                raise ValueError("bootstrap the first administrator before registering users")
            user = self._new_user(username, password, role, active=active)
            if session.scalar(select(ControlPlaneUser).where(ControlPlaneUser.username == user.username)):
                raise ValueError("username is already registered")
            session.add(user)
        return self._user_dict(user)

    @staticmethod
    def _new_user(
        username: str,
        password: str,
        role: str,
        *,
        active: bool = True,
    ) -> ControlPlaneUser:
        now = _utcnow()
        normalized = _normalize_username(username)
        return ControlPlaneUser(
            user_id=str(uuid.uuid4()),
            username=normalized,
            password_digest=_password_digest(_validate_password(password)),
            role=role,
            active=bool(active),
            created_at=now,
            updated_at=now,
        )

    def authenticate_user(self, username: str, password: str, *, session_days: int = 7) -> tuple[str, dict]:
        normalized = _normalize_username(username)
        with self.sessions.begin() as session:
            # Serialize login with password replacement so old credentials cannot create a surviving session.
            session.execute(
                update(ControlPlaneUser)
                .where(ControlPlaneUser.username == normalized)
                .values(updated_at=ControlPlaneUser.updated_at)
            )
            user = session.scalar(select(ControlPlaneUser).where(ControlPlaneUser.username == normalized))
            if user is None or not _password_matches(user.password_digest, password):
                raise PermissionError("invalid username or password")
            if not user.active:
                raise PermissionError("account is awaiting administrator approval or has been disabled")
            token = secrets.token_urlsafe(32)
            now = _utcnow()
            session.add(
                ControlPlaneSession(
                    session_id=str(uuid.uuid4()),
                    user_id=user.user_id,
                    token_digest=_token_digest(token),
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(days=max(1, int(session_days))),
                )
            )
        return token, self._user_dict(user)

    def verify_session(self, token: str | None, *, original: bool = False) -> dict | None:
        if not token:
            return None
        now = _utcnow()
        with self.sessions.begin() as session:
            row = session.execute(
                select(ControlPlaneSession, ControlPlaneUser)
                .join(ControlPlaneUser, ControlPlaneUser.user_id == ControlPlaneSession.user_id)
                .where(ControlPlaneSession.token_digest == _token_digest(token))
            ).one_or_none()
            if row is None:
                return None
            stored_session, user = row
            if stored_session.expires_at <= now or not user.active:
                session.delete(stored_session)
                return None
            switched = session.get(DevRoleSession, stored_session.session_id)
            if switched is not None and user.role != "admin":
                session.delete(switched)
                session.delete(stored_session)
                return None
            if stored_session.last_seen_at <= now - timedelta(minutes=5):
                stored_session.last_seen_at = now
            from lerobot.data_platform.dev_roles import effective_user

            effective = effective_user(session, stored_session, user, original=original)
            if effective is not None:
                return effective
            return self._user_dict(user)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.sessions.begin() as session:
            stored = session.scalar(
                select(ControlPlaneSession).where(ControlPlaneSession.token_digest == _token_digest(token))
            )
            if stored is not None:
                role_session = session.get(DevRoleSession, stored.session_id)
                if role_session is not None:
                    session.delete(role_session)
                session.delete(stored)

    def list_users(self) -> list[dict]:
        with self.sessions() as session:
            users = session.scalars(select(ControlPlaneUser).order_by(ControlPlaneUser.username)).all()
            return [self._user_dict(user) for user in users]

    def update_user(self, user_id: str, *, role: str | None = None, active: bool | None = None) -> dict:
        if role is not None and role not in USER_ROLES:
            raise ValueError(f"unsupported role: {role}")
        with self.sessions.begin() as session:
            user = session.get(ControlPlaneUser, str(user_id))
            if user is None:
                raise KeyError(user_id)
            removes_active_admin = (
                user.active and user.role == "admin" and (role not in {None, "admin"} or active is False)
            )
            if removes_active_admin:
                active_admins = session.scalar(
                    select(func.count())
                    .select_from(ControlPlaneUser)
                    .where(ControlPlaneUser.role == "admin", ControlPlaneUser.active.is_(True))
                )
                if int(active_admins or 0) <= 1:
                    raise ValueError("the last active administrator cannot be demoted or deactivated")
            if role is not None:
                if user.role == "data_manager" and role != "data_manager":
                    session.execute(delete(DataMutationGrant).where(DataMutationGrant.user_id == user_id))
                user.role = role
            if active is not None:
                user.active = bool(active)
            user.updated_at = _utcnow()
        return self._user_dict(user)

    def enroll_node(
        self,
        *,
        name: str,
        hostname: str,
        allowed_roots: list[str],
        writable_roots: list[str],
        capabilities: dict,
        enrollment_token: str,
        expected_token: str,
    ) -> tuple[str, dict]:
        if not expected_token:
            raise RuntimeError("DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN is not configured")
        if not hmac.compare_digest(str(enrollment_token), str(expected_token)):
            raise PermissionError("invalid agent enrollment token")
        node_name = str(name or "").strip()
        if not node_name or len(node_name) > 128:
            raise ValueError("node name is required and must contain at most 128 characters")
        now = _utcnow()
        token = secrets.token_urlsafe(40)
        with self.sessions.begin() as session:
            node = session.scalar(select(ControlPlaneNode).where(ControlPlaneNode.name == node_name))
            if node is None:
                node = ControlPlaneNode(
                    node_id=str(uuid.uuid4()),
                    name=node_name,
                    hostname=str(hostname or node_name)[:255],
                    token_digest=_token_digest(token),
                    status="online",
                    allowed_roots=list(allowed_roots),
                    writable_roots=list(writable_roots),
                    capabilities=dict(capabilities),
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                session.add(node)
            else:
                node.hostname = str(hostname or node_name)[:255]
                node.token_digest = _token_digest(token)
                node.status = "online"
                node.allowed_roots = list(allowed_roots)
                node.writable_roots = list(writable_roots)
                node.capabilities = dict(capabilities)
                node.updated_at = now
                node.last_seen_at = now
        return token, self._node_dict(node)

    def authenticate_node(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.sessions() as session:
            node = session.scalar(
                select(ControlPlaneNode).where(ControlPlaneNode.token_digest == _token_digest(token))
            )
            return self._node_dict(node) if node is not None else None

    def heartbeat(self, node_id: str, *, capabilities: dict | None = None) -> dict:
        with self.sessions.begin() as session:
            node = session.get(ControlPlaneNode, str(node_id))
            if node is None:
                raise KeyError(node_id)
            now = _utcnow()
            node.status = "online"
            node.last_seen_at = now
            node.updated_at = now
            if capabilities is not None:
                node.capabilities = dict(capabilities)
            return self._node_dict(node)

    def configure_local_execution_roots(
        self, node_id: str, allowed_roots: list[str], writable_roots: list[str]
    ) -> None:
        """Keep the registered local executor consistent with its server-generated configuration."""
        with self.sessions.begin() as session:
            node = session.get(ControlPlaneNode, node_id)
            if (
                node is None
                or not node.name.startswith("local-")
                or not node.capabilities.get("local_requests")
            ):
                raise ValueError("Expected the registered local executor")
            node.allowed_roots = list(allowed_roots)
            node.writable_roots = list(writable_roots)

    def sync_locations(self, node_id: str, locations: list[dict]) -> list[dict]:
        now = _utcnow()
        synced = []
        with self.sessions.begin() as session:
            if session.get(ControlPlaneNode, str(node_id)) is None:
                raise KeyError(node_id)
            for payload in locations:
                root = str(payload.get("root") or "").strip()
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not root or not dataset_key:
                    raise ValueError("each dataset location requires root and dataset_key")
                root_digest = _token_digest(root)
                location = session.scalar(
                    select(DatasetLocation).where(
                        DatasetLocation.node_id == str(node_id),
                        DatasetLocation.root_digest == root_digest,
                    )
                )
                if location is None:
                    location = DatasetLocation(
                        location_id=str(uuid.uuid4()),
                        node_id=str(node_id),
                        dataset_key=dataset_key,
                        root=root,
                        root_digest=root_digest,
                        output_dir=str(payload.get("output_dir") or ""),
                        state="available",
                        details=dict(payload.get("metadata") or {}),
                        created_at=now,
                        updated_at=now,
                        last_seen_at=now,
                    )
                    session.add(location)
                else:
                    location.dataset_key = dataset_key
                    location.output_dir = str(payload.get("output_dir") or location.output_dir)
                    location.state = "available"
                    details = dict(payload.get("metadata") or {})
                    previous_details = dict(location.details or {})
                    for key in ("viewer_ready", "viewer_url", "cache_root"):
                        if key in previous_details:
                            details[key] = previous_details[key]
                    location.details = details
                    location.updated_at = now
                    location.last_seen_at = now
                synced.append(location)
        return [self._location_dict(location) for location in synced]

    def list_nodes(self) -> list[dict]:
        with self.sessions() as session:
            nodes = session.scalars(select(ControlPlaneNode).order_by(ControlPlaneNode.name)).all()
            return [self._node_dict(node) for node in nodes]

    def list_locations(self) -> list[dict]:
        with self.sessions() as session:
            # MySQL filesort may copy selected JSON into its sort buffer. Sort
            # only small identifiers here, then load full metadata separately.
            rows = session.execute(
                select(DatasetLocation.location_id, ControlPlaneNode.name, ControlPlaneNode.hostname)
                .join(ControlPlaneNode, ControlPlaneNode.node_id == DatasetLocation.node_id)
                .order_by(ControlPlaneNode.name, DatasetLocation.dataset_key, DatasetLocation.root)
            ).all()
            if not rows:
                return []
            locations = {
                location.location_id: self._location_dict(location)
                for location in session.scalars(select(DatasetLocation)).all()
            }
            return [
                {
                    **locations[location_id],
                    "node_name": node_name,
                    "node_hostname": node_hostname,
                }
                for location_id, node_name, node_hostname in rows
            ]

    def get_location(self, location_id: str) -> dict:
        with self.sessions() as session:
            location = session.get(DatasetLocation, str(location_id))
            if location is None:
                raise KeyError(location_id)
            return self._location_dict(location)

    def create_job(
        self,
        *,
        location_id: str,
        requested_by: str,
        operation: str,
        options: dict | None = None,
        idempotency_key: str | None = None,
        reuse_active: bool = False,
        job_id: str | None = None,
        _session=None,
    ) -> dict:
        now = _utcnow()
        with nullcontext(_session) if _session is not None else self.sessions.begin() as session:
            from lerobot.data_platform.job_management import JobConflictError, scheduler_lock

            scheduler_lock(session)
            from lerobot.data_platform.maintenance import MaintenanceError, is_maintenance

            if is_maintenance(session):
                raise MaintenanceError("Environment is under maintenance; new jobs are paused")
            location = session.get(DatasetLocation, str(location_id))
            if location is None:
                raise KeyError(location_id)
            submitter = session.get(ControlPlaneUser, requested_by)
            if (
                submitter is not None
                and submitter.role == "data_manager"
                and operation.startswith("mutation.")
                and (
                    operation not in SCOPED_SOURCE_OPERATIONS
                    or not self.source_mutation_allowed(session, submitter, location)
                )
            ):
                raise PermissionError("Source mutation permission is required for this dataset location")
            if idempotency_key:
                existing = session.scalar(
                    select(RemoteJob).where(RemoteJob.idempotency_key == str(idempotency_key))
                )
                if existing is not None:
                    if operation.startswith("mutation.") and (
                        existing.requested_by != requested_by
                        or existing.location_id != location_id
                        or existing.operation != operation
                    ):
                        raise JobConflictError("Idempotency key belongs to a different mutation request")
                    return self._job_dict(existing)
            if reuse_active:
                active_jobs = session.scalars(
                    select(RemoteJob)
                    .where(
                        RemoteJob.location_id == location.location_id,
                        RemoteJob.operation == str(operation),
                        RemoteJob.status.in_({"queued", "running"}),
                    )
                    .order_by(RemoteJob.created_at.desc())
                )
                for existing in active_jobs:
                    current_digest = ((existing.options or {}).get("task_config") or {}).get("digest")
                    requested_digest = ((options or {}).get("task_config") or {}).get("digest")
                    if current_digest == requested_digest:
                        return self._job_dict(existing)
            queued_count = session.scalar(
                select(func.count())
                .select_from(RemoteJob)
                .where(RemoteJob.requested_by == str(requested_by), RemoteJob.status == "queued")
            )
            if queued_count >= self.job_manager.queue_limit:
                raise JobConflictError("User queue limit reached")
            job = RemoteJob(
                job_id=job_id or str(uuid.uuid4()),
                node_id=location.node_id,
                location_id=location.location_id,
                requested_by=str(requested_by),
                operation=str(operation),
                options=dict(options or {}),
                status="queued",
                idempotency_key=str(idempotency_key) if idempotency_key else None,
                result={},
                error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            self.job_manager.on_create(session, job)
        return self._job_dict(job)

    def claim_job(self, node_id: str, *, lease_seconds: int = 60, worker_instance_id=None) -> dict | None:
        return self.job_manager.claim(str(node_id), lease_seconds, worker_instance_id)

    def heartbeat_job(
        self, job_id: str, *, node_id: str, lease_seconds: int = 60, attempt_id=None, credential=None
    ) -> bool:
        try:
            return self.job_manager.heartbeat(
                job_id, node_id, lease_seconds=lease_seconds, attempt_id=attempt_id, credential=credential
            )["renewed"]
        except (KeyError, ValueError):
            return False

    def add_job_event(
        self,
        job_id: str,
        *,
        node_id: str,
        message: str,
        level: str = "info",
        payload: dict | None = None,
        attempt_id=None,
        credential=None,
    ) -> dict:
        from lerobot.data_platform.job_management import scheduler_lock
        from lerobot.data_platform.management_storage import EventReceipt
        from lerobot.data_platform.operation_log import sanitize_for_log

        with self.sessions.begin() as session:
            scheduler_lock(session)
            job = session.get(RemoteJob, str(job_id))
            self.job_manager.validate(session, job, node_id, attempt_id, credential)
            delivery_id = (payload or {}).get("delivery_event_id")
            receipt_id = (
                hashlib.sha256(f"{job_id}:{attempt_id}:{delivery_id}".encode()).hexdigest()
                if delivery_id
                else None
            )
            previous = session.get(EventReceipt, receipt_id) if receipt_id else None
            if previous:
                return self._event_dict(session.get(RemoteJobEvent, previous.event_id))
            if job is None or job.node_id != str(node_id):
                raise KeyError(job_id)
            event = RemoteJobEvent(
                job_id=job.job_id,
                level=str(level or "info")[:32],
                message=sanitize_for_log(str(message)),
                payload=sanitize_for_log(dict(payload or {})),
                created_at=_utcnow(),
            )
            session.add(event)
            if payload and ("current" in payload or "total" in payload):
                job.result = {**(job.result or {}), "progress": sanitize_for_log(payload)}
            session.flush()
            if receipt_id:
                session.add(EventReceipt(receipt_id=receipt_id, event_id=event.event_id))
            self.job_manager.audit(
                session,
                job,
                "job.event",
                details={
                    "event_id": event.event_id,
                    "level": event.level,
                    "message": event.message,
                    "payload": event.payload,
                },
            )
        return self._event_dict(event)

    def complete_job(
        self,
        job_id: str,
        *,
        node_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        attempt_id=None,
        credential=None,
    ) -> dict:
        return self.job_manager.finish(
            job_id,
            node_id,
            status=status,
            result=result,
            error=error,
            attempt_id=attempt_id,
            credential=credential,
        )

    def list_jobs(self, *, limit: int = 200, actor: dict | None = None) -> list[dict]:
        self.job_manager.reap_expired()
        with self.sessions() as session:
            # Sort identifiers only: MySQL filesort can otherwise copy large JSON
            # options/results into the sort buffer for owner-filtered queries.
            query = select(RemoteJob.job_id)
            if actor is not None and actor.get("role") != "admin":
                query = query.where(RemoteJob.requested_by == actor.get("user_id", ""))
            job_ids = session.scalars(
                query.order_by(RemoteJob.created_at.desc(), RemoteJob.job_id.desc()).limit(
                    max(1, min(1000, limit))
                )
            ).all()
            if not job_ids:
                return []
            details = select(RemoteJob).where(RemoteJob.job_id.in_(job_ids))
            if actor is not None and actor.get("role") != "admin":
                details = details.where(RemoteJob.requested_by == actor.get("user_id", ""))
            jobs = {job.job_id: job for job in session.scalars(details).all()}
            return [self._job_dict(jobs[job_id]) for job_id in job_ids if job_id in jobs]

    def get_job(self, job_id: str) -> dict:
        with self.sessions() as session:
            job = session.get(RemoteJob, str(job_id))
            if job is None:
                raise KeyError(job_id)
            events = session.scalars(
                select(RemoteJobEvent)
                .where(RemoteJobEvent.job_id == job.job_id)
                .order_by(RemoteJobEvent.event_id.desc())
                .limit(200)
            ).all()
            events.reverse()
            payload = self._job_dict(job)
            payload["events"] = [self._event_dict(event) for event in events]
            return payload

    def mark_viewer_ready(self, location_id: str, *, viewer_url: str, cache_root: str) -> dict:
        with self.sessions.begin() as session:
            location = session.get(DatasetLocation, str(location_id))
            if location is None:
                raise KeyError(location_id)
            details = dict(location.details or {})
            details.update({"viewer_ready": True, "viewer_url": viewer_url, "cache_root": cache_root})
            location.details = details
            location.updated_at = _utcnow()
        return self._location_dict(location)

    def mark_viewer_stale(self, location_id: str) -> dict:
        """Invalidate derived viewer state after a source dataset mutation."""
        with self.sessions.begin() as session:
            location = session.get(DatasetLocation, str(location_id))
            if location is None:
                raise KeyError(location_id)
            details = dict(location.details or {})
            details.pop("viewer_url", None)
            details.pop("cache_root", None)
            details["viewer_ready"] = False
            location.details = details
            location.updated_at = _utcnow()
        return self._location_dict(location)

    @staticmethod
    def _user_dict(user: ControlPlaneUser) -> dict:
        return {
            "user_id": user.user_id,
            "username": user.username,
            "role": user.role,
            "active": bool(user.active),
            "created_at": _iso(user.created_at),
            "updated_at": _iso(user.updated_at),
        }

    @staticmethod
    def _node_dict(node: ControlPlaneNode) -> dict:
        status = node.status
        if node.last_seen_at is None or node.last_seen_at < _utcnow() - timedelta(minutes=2):
            status = "offline"
        return {
            "node_id": node.node_id,
            "name": node.name,
            "hostname": node.hostname,
            "status": status,
            "allowed_roots": list(node.allowed_roots or []),
            "writable_roots": list(node.writable_roots or []),
            "capabilities": dict(node.capabilities or {}),
            "created_at": _iso(node.created_at),
            "updated_at": _iso(node.updated_at),
            "last_seen_at": _iso(node.last_seen_at),
        }

    @staticmethod
    def _location_dict(location: DatasetLocation) -> dict:
        return {
            "location_id": location.location_id,
            "node_id": location.node_id,
            "dataset_key": location.dataset_key,
            "root": location.root,
            "output_dir": location.output_dir,
            "state": location.state,
            "metadata": dict(location.details or {}),
            "created_at": _iso(location.created_at),
            "updated_at": _iso(location.updated_at),
            "last_seen_at": _iso(location.last_seen_at),
        }

    @staticmethod
    def _job_dict(job: RemoteJob) -> dict:
        if job.status not in JOB_STATES:
            raise ValueError(f"invalid persisted job state: {job.status}")
        return {
            "job_id": job.job_id,
            "node_id": job.node_id,
            "location_id": job.location_id,
            "requested_by": job.requested_by,
            "operation": job.operation,
            "options": dict(job.options or {}),
            "status": job.status,
            "lease_owner": job.lease_owner,
            "lease_until": _iso(job.lease_until),
            "idempotency_key": job.idempotency_key,
            "result": dict(job.result or {}),
            "error": job.error,
            "created_at": _iso(job.created_at),
            "updated_at": _iso(job.updated_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        }

    @staticmethod
    def _event_dict(event: RemoteJobEvent) -> dict:
        return {
            "event_id": event.event_id,
            "job_id": event.job_id,
            "level": event.level,
            "message": event.message,
            "payload": dict(event.payload or {}),
            "created_at": _iso(event.created_at),
        }
