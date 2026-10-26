"""Account-private history discovery and explicit, constrained response preferences."""

from __future__ import annotations

import time

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import load_only

from lerobot.data_platform.ai_tools.context import normalize_context
from lerobot.data_platform.ai_tools.models import AssistantConversation, AssistantPreferences
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.operators import list_operators

DEFAULTS = {"enabled": True, "language": "auto", "detail": "balanced", "favorite_operators": []}


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ValueError("Use the supported preference fields")
    if (
        type(settings["enabled"]) is not bool
        or settings["language"] not in ("auto", "zh", "en")
        or settings["detail"] not in ("concise", "balanced", "detailed")
    ):
        raise ValueError("Invalid preference value")
    favorites = settings["favorite_operators"]
    allowed = {
        op.operator_id
        for op in list_operators()
        if op.pipeline_allowed and op.operator_id != "curation.snapshot"
    }
    if (
        not isinstance(favorites, list)
        or len(favorites) > 5
        or any(not isinstance(item, str) or item not in allowed for item in favorites)
        or len(set(favorites)) != len(favorites)
    ):
        raise ValueError("Choose up to five registered pipeline operators")
    return {**settings, "favorite_operators": list(favorites)}


def preferences(service, owner, *, change=None, reset=False):
    with service.store.sessions.begin() as session:
        service.manager._actor(session, owner, write=False)
        if change is not None:
            if (
                not isinstance(change, dict)
                or set(change) != ({"expected_revision"} if reset else {"expected_revision", "settings"})
                or type(change.get("expected_revision")) is not int
                or change["expected_revision"] < 0
            ):
                raise ValueError("Preference revision is required")
            settings = dict(DEFAULTS) if reset else validate(change["settings"])
            scheduler_lock(session)
        row = (
            session.scalar(
                select(AssistantPreferences).where(AssistantPreferences.owner == owner).with_for_update()
            )
            if change is not None
            else session.get(AssistantPreferences, owner)
        )
        if change is not None:
            if change["expected_revision"] != (row.revision if row else 0):
                raise JobConflictError("Preferences changed. Reload before saving.")
            if row is None:
                row = AssistantPreferences(owner=owner, revision=0, settings={}, updated_at=0)
                session.add(row)
            row.settings = settings
            row.revision += 1
            row.updated_at = int(time.time())
            service.manager._audit(
                session,
                owner,
                "assistant.preferences.reset" if reset else "assistant.preferences.update",
                revision=row.revision,
            )
        return {
            "revision": row.revision if row else 0,
            "settings": validate(row.settings) if row else dict(DEFAULTS),
        }


def conversations(service, owner, *, cursor=None, limit=20):
    """Scan at most 100 owned rows; inaccessible objects never yield titles or IDs."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("History page size must be 1–50")
    from lerobot.data_platform.ai_tools.service import _uuid

    query = select(AssistantConversation).where(AssistantConversation.owner == owner)
    if cursor:
        if not isinstance(cursor, str) or len(cursor) > 64:
            raise ValueError("Invalid history cursor")
        timestamp, identity = cursor.split(":", 1)
        timestamp, identity = int(timestamp), _uuid(identity)
        query = query.where(
            or_(
                AssistantConversation.updated_at < timestamp,
                and_(
                    AssistantConversation.updated_at == timestamp,
                    AssistantConversation.conversation_id < identity,
                ),
            )
        )
    query = (
        query.options(
            load_only(
                AssistantConversation.conversation_id,
                AssistantConversation.owner,
                AssistantConversation.location_id,
                AssistantConversation.input_identity,
                AssistantConversation.context,
                AssistantConversation.updated_at,
            )
        )
        .order_by(AssistantConversation.updated_at.desc(), AssistantConversation.conversation_id.desc())
        .limit(101)
    )
    items, last = [], None
    guard = DisclosureGuard.from_environment()
    with service.store.sessions() as session:
        service.manager._actor(session, owner, write=False)
        rows = list(session.scalars(query))
        consumed = 0
        for row in rows[:100]:
            last = f"{row.updated_at}:{row.conversation_id}"
            consumed += 1
            try:
                current = service._target(session, owner, row.location_id, context=row.context)
            except (KeyError, PermissionError, JobConflictError, ValueError):
                continue
            title = next((item["content"] for item in row.history if item.get("role") == "user"), "New chat")
            location, _ = service.manager._location(session, row.location_id, owner, write=False)
            items.append(
                {
                    "conversation_id": row.conversation_id,
                    "location_id": row.location_id,
                    "context": normalize_context(row.context, row.location_id),
                    "title": guard.redact(title)[:100],
                    "dataset": guard.redact(location["dataset_key"])[:128],
                    "updated_at": row.updated_at,
                    "available": current == row.input_identity,
                    "reason": None if current == row.input_identity else "Context changed. Start a new chat.",
                }
            )
            if len(items) == limit:
                break
        return {"conversations": items, "next_cursor": last if consumed < len(rows) else None}
