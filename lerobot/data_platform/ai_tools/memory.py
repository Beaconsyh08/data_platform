"""Explicit account-private notes; never inferred from chat or written by model tools."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import update

from lerobot.data_platform.ai_tools.models import AssistantFollowup, AssistantMemory
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock

DEFAULTS = {"enabled": True, "followups_enabled": True}
LIMIT = 20


def settings(session, owner):
    row = session.get(AssistantMemory, owner)
    return row.settings if row else dict(DEFAULTS)


def _visible(service, session, owner, note):
    if note["scope"] == "dataset":
        try:
            service._target(session, owner, note["location_id"], note["input_identity"])
        except (PermissionError, KeyError, JobConflictError):
            return False
    return True


def read(service, owner):
    with service.store.sessions() as session:
        service.manager._actor(session, owner, write=False)
        row = session.get(AssistantMemory, owner)
        return {
            "revision": row.revision if row else 0,
            "settings": row.settings if row else dict(DEFAULTS),
            "notes": [
                {
                    key: DisclosureGuard.from_environment().tree(value)
                    for key, value in note.items()
                    if key != "input_identity"
                }
                for note in (row.notes if row else [])
                if _visible(service, session, owner, note)
            ],
        }


def change(service, owner, value):
    """All note/settings changes use one optimistic revision and the existing audit transaction."""
    if (
        not isinstance(value, dict)
        or type(value.get("expected_revision")) is not int
        or value["expected_revision"] < 0
    ):
        raise ValueError("Memory revision is required")
    operation = value.get("operation")
    fields = {
        "save": {"memory_id", "text", "scope", "location_id"},
        "delete": {"memory_id"},
        "clear": set(),
        "settings": {"settings"},
    }
    if operation not in fields or set(value) - (fields[operation] | {"operation", "expected_revision"}):
        raise ValueError("Unsupported memory change")
    with service.store.sessions.begin() as session:
        scheduler_lock(session)
        service.manager._actor(session, owner, write=False)
        row = session.get(AssistantMemory, owner)
        if value["expected_revision"] != (row.revision if row else 0):
            raise JobConflictError("Memory changed. Reload before saving.")
        if row is None:
            row = AssistantMemory(owner=owner, revision=0, settings=dict(DEFAULTS), notes=[], updated_at=0)
            session.add(row)
        notes = list(row.notes)
        if operation == "save":
            text = value.get("text")
            if (
                not isinstance(text, str)
                or not 1 <= len(text.strip()) <= 500
                or any(ord(c) < 32 and c not in "\n\t" for c in text)
                or not DisclosureGuard.from_environment().safe_draft(text)
            ):
                raise ValueError("Use a short note without credentials or server paths")
            scope = value.get("scope")
            location = value.get("location_id")
            if scope not in {"account", "dataset"} or (scope == "account" and location is not None):
                raise ValueError("Choose account or dataset memory")
            identity = None
            if scope == "dataset":
                from lerobot.data_platform.ai_tools.service import _uuid

                location = _uuid(location)
                identity = service._target(session, owner, location)
            memory_id = value.get("memory_id")
            previous = next((n for n in notes if n["memory_id"] == memory_id), None)
            if memory_id and (previous is None or not _visible(service, session, owner, previous)):
                raise KeyError("Memory not found")
            if previous is None and len(notes) >= LIMIT:
                raise JobConflictError("Keep at most 20 notes. Delete or clear old notes first.")
            notes = [n for n in notes if n["memory_id"] != memory_id]
            notes.append(
                {
                    "memory_id": memory_id or str(uuid.uuid4()),
                    "text": text.strip(),
                    "scope": scope,
                    "location_id": location,
                    "input_identity": identity,
                    "updated_at": int(time.time()),
                }
            )
        elif operation == "delete":
            if not any(n["memory_id"] == value.get("memory_id") for n in notes):
                raise KeyError("Memory not found")
            notes = [n for n in notes if n["memory_id"] != value["memory_id"]]
        elif operation == "clear":
            notes = []
        else:
            selected = value.get("settings")
            if (
                not isinstance(selected, dict)
                or set(selected) != set(DEFAULTS)
                or any(type(v) is not bool for v in selected.values())
            ):
                raise ValueError("Use the supported memory settings")
            row.settings = dict(selected)
            if not selected["followups_enabled"]:
                session.execute(
                    update(AssistantFollowup)
                    .where(AssistantFollowup.owner == owner, AssistantFollowup.status == "pending")
                    .values(status="muted", updated_at=int(time.time()))
                )
        row.notes, row.updated_at = notes, int(time.time())
        row.revision += 1
        from lerobot.data_platform.ai_tools.experience import invalidate

        invalidate(session, owner)
        service.manager._audit(session, owner, "assistant.memory." + operation, revision=row.revision)
    return read(service, owner)


def context(service, owner, location_id):
    """Return bounded authorized data, with no write or tool-execution authority."""
    with service.store.sessions() as session:
        service._target(session, owner, location_id)
        row = session.get(AssistantMemory, owner)
        if row is None or not row.settings["enabled"]:
            return []
        candidates = [n for n in row.notes if n["scope"] == "account" or n["location_id"] == location_id]
        candidates.sort(
            key=lambda n: (n["scope"] == "dataset", n["updated_at"], n["memory_id"]), reverse=True
        )
        return [
            {"scope": n["scope"], "text": DisclosureGuard.from_environment().redact(n["text"])}
            for n in candidates
            if _visible(service, session, owner, n)
        ][:8]
