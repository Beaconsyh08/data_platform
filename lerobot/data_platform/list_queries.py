"""Bounded list cursors and database-side JSON projections; no persisted list cache."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import JSON, and_, func, or_, type_coerce
from sqlalchemy.orm import with_expression


def without_json(column, paths):
    return type_coerce(func.json_remove(column, *paths), JSON)


def location_projection(model):
    return with_expression(model.details, without_json(model.details, ["$.episodes", "$.stats", "$.tasks"]))


def job_projection(model):
    return (
        with_expression(
            model.options,
            without_json(
                model.options,
                ["$.snapshot", "$.task_config", "$.operator_snapshot", "$.registry", "$._source_locations"],
            ),
        ),
        with_expression(
            model.result,
            without_json(
                model.result,
                [
                    "$.precompute",
                    "$.mcap_import.files",
                    "$.mcap_import.rejected",
                    "$.preprocess.episode_lineage",
                    *[
                        f"$.{field}.metadata.{key}"
                        for field in ("dataset_location", "synced_location")
                        for key in ("episodes", "stats", "tasks")
                    ],
                ],
            ),
        ),
    )


@dataclass(frozen=True)
class ListPage:
    size: int
    scope: str
    position: tuple[str | float, str] | None = None

    @classmethod
    def parse(cls, args, *, scope):
        try:
            size = int(args.get("page_size", 50))
        except (ValueError, TypeError) as exc:
            raise ValueError("page_size must be an integer") from exc
        if not 1 <= size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        cursor = args.get("cursor")
        position = None
        if cursor:
            try:
                if len(cursor) > 1024:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                position = value["position"]
                if value["scope"] != fingerprint or len(position) != 2 or not isinstance(position[1], str):
                    raise ValueError()
                if not isinstance(position[0], (str, float, int)):
                    raise ValueError()
                if isinstance(position[0], (float, int)) and not math.isfinite(position[0]):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise ValueError("Invalid cursor or changed list filters") from exc
        return cls(size, fingerprint, tuple(position) if position else None)

    def apply(self, query, timestamp, identity, *, date_time=False):
        if self.position:
            value, key = self.position
            value = parse_time(value) if date_time else float(value)
            query = query.where(or_(timestamp < value, and_(timestamp == value, identity < key)))
        return query.order_by(timestamp.desc(), identity.desc()).limit(self.size + 1)

    def response(self, rows, *, timestamp, identity):
        more = len(rows) > self.size
        rows = rows[: self.size]
        cursor = None
        if more:
            value = timestamp(rows[-1])
            if isinstance(value, datetime):
                value = value.isoformat()
            cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "scope": self.scope,
                            "position": [value, identity(rows[-1])],
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return rows, {"next_cursor": cursor, "has_more": more}


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError) as exc:
        raise ValueError("Time filters must be ISO 8601 timestamps") from exc
