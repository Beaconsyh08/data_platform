"""Task-derived suggestions. Foreground reads use SQL only and revalidate every source."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from lerobot.data_platform.ai_tools import followups, memory
from lerobot.data_platform.ai_tools.models import (
    AssistantExperience,
    AssistantFollowup,
    AssistantMemory,
    AssistantTurn,
)
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.environment import EnvironmentIdentity
from lerobot.data_platform.job_management import JobConflictError
from lerobot.data_platform.task_catalog import content_digest


class ExperienceChangedError(JobConflictError):
    """An in-flight answer must not keep using superseded notes or task evidence."""


def enabled():
    return os.environ.get("DATA_PLATFORM_AI_EXPERIENCE_ENABLED", "0") == "1"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def scope_id(owner, location):
    identity = EnvironmentIdentity.from_env()
    return digest([asdict(identity) if identity else None, owner, location])


@dataclass(frozen=True)
class Source:
    source_id: str
    run_id: str
    run_revision: int
    completed_at: int
    text: str
    digest: str


@dataclass(frozen=True)
class Snapshot:
    notes_revision: int
    sources: tuple[Source, ...]

    @property
    def digest(self):
        return digest(asdict(self))


def enqueue(session, owner, location):
    """Called inside the existing scheduler/audit transaction; never contacts a provider."""
    identity = scope_id(owner, location)
    row = session.get(AssistantExperience, identity)
    if row is None:
        row = AssistantExperience(scope_id=identity, owner=owner, location_id=location, epoch=0)
        session.add(row)
    row.epoch += 1
    row.status, row.observations, row.source_digest = "pending", [], ""
    row.next_check_at, row.updated_at = 0, int(time.time())
    # Do not release an in-flight lease. Its publisher must see the changed epoch.


def invalidate(session, owner):
    for row in session.scalars(select(AssistantExperience).where(AssistantExperience.owner == owner)):
        row.epoch += 1
        row.status, row.observations, row.source_digest = "pending", [], ""
        row.next_check_at, row.updated_at = 0, int(time.time())


def snapshot(service, session, owner, location):
    service._target(session, owner, location)
    notes = session.get(AssistantMemory, owner)
    revision = notes.revision if notes else 0
    settings = memory.settings(session, owner)
    if not settings["enabled"] or not settings["followups_enabled"]:
        return Snapshot(revision, ())
    # Conservative rule barrier: no task predating ANY explicit note/settings edit
    # can seed derived advice. Same-second records are excluded too.
    cutoff = max(notes.updated_at if notes else 0, int(time.time()) - 30 * 86400)
    rows = session.scalars(
        select(AssistantFollowup)
        .where(
            AssistantFollowup.owner == owner,
            AssistantFollowup.location_id == location,
            AssistantFollowup.status == "ready",
            AssistantFollowup.updated_at > cutoff,
        )
        .order_by(AssistantFollowup.updated_at.desc(), AssistantFollowup.action_id)
        .limit(20)
    ).all()
    guard, sources, seen_runs = DisclosureGuard.from_environment(), [], set()
    for row in rows:
        try:
            run, steps = followups._authorized(service, session, row)
        except (PermissionError, KeyError, JobConflictError, ValueError):
            continue
        # PipelineRun.created_at is a legacy MySQL FLOAT and loses epoch precision.
        # Prove the original initiating action through the deterministic run key,
        # then use its exact BIGINT creation time. A later action reusing an old
        # Run cannot supply a newer timestamp for that Run.
        origin = session.get(AssistantTurn, row.action_id)
        delivery = "assistant:" + row.action_id
        pipeline_id = str(uuid.uuid5(uuid.NAMESPACE_URL, content_digest([owner, delivery])))
        original_key = content_digest([owner, pipeline_id, 1, location, delivery])
        if (
            run.run_id in seen_runs
            or origin is None
            or origin.owner != owner
            or origin.created_at <= cutoff
            or run.idempotency_key != original_key
            or run.status != row.summary.get("run_status")
            or run.revision != row.summary.get("run_revision")
            or not followups._acknowledged(session, run, steps)
        ):
            continue
        record = {
            "run_id": run.run_id,
            "origin_action_id": origin.request_id,
            "origin_created_at": origin.created_at,
            "run_revision": run.revision,
            "completed_at": row.updated_at,
            # Followup prose is UI guidance, not evidence. In particular the
            # operator's review gate flag does not certify dataset quality or
            # waive human review. Keep only acknowledged execution facts.
            "outcome": {
                key: row.summary[key] for key in ("operators", "run_status", "completed_steps", "total_steps")
            },
        }
        text = encode(guard.tree(record))
        if len(text.encode()) > 6000:
            continue
        sources.append(Source(row.action_id, run.run_id, run.revision, row.updated_at, text, digest(record)))
        seen_runs.add(run.run_id)
    return Snapshot(revision, tuple(sources))


def context(service, owner, location):
    """No Hindsight, embedding, model or network call is permitted on this path."""
    if not enabled():
        return []
    with service.store.sessions() as session:
        row = session.get(AssistantExperience, scope_id(owner, location))
        if row is None or row.status != "ready" or not row.observations:
            return []
        current = snapshot(service, session, owner, location)
        if row.notes_revision != current.notes_revision or row.source_digest != current.digest:
            return []
        sources = {s.source_id: s for s in current.sources}
        selected = []
        for item in row.observations[:4]:
            citations = item.get("sources", [])
            if len({s.get("run_id") for s in citations}) < 2:
                continue
            if not all(
                (source := sources.get(c.get("source_id")))
                and source.digest == c.get("digest")
                and source.run_id == c.get("run_id")
                and isinstance(c.get("quote"), str)
                and len(c["quote"]) >= 12
                and c["quote"] in source.text
                for c in citations
            ):
                continue
            candidate = DisclosureGuard.from_environment().tree(item)
            if len(encode(selected + [candidate]).encode()) <= 6000:
                selected.append(candidate)
        return selected


@dataclass(frozen=True)
class MemoryVersion:
    notes_revision: int
    source_digest: str | None


def memory_version(service, session, owner, location, *, has_experiences):
    row = session.get(AssistantMemory, owner)
    return MemoryVersion(
        row.revision if row else 0,
        snapshot(service, session, owner, location).digest if has_experiences else None,
    )


def generation_guard(service, owner, location, *, has_experiences):
    """Reject changed authority/evidence across model calls and durable request recovery."""

    def version():
        with service.store.sessions() as session:
            return memory_version(service, session, owner, location, has_experiences=has_experiences)

    initial = version()

    def check():
        if version() != initial:
            raise ExperienceChangedError("Saved memory or task evidence changed. Start a new request.")

    return check, initial
