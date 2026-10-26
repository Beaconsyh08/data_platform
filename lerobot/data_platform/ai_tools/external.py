"""Model-independent, authorized tools for external assistants."""

from __future__ import annotations

import time
import uuid
from urllib.parse import urlencode

from sqlalchemy import select

from lerobot.data_platform.ai_tools.actions import prepare
from lerobot.data_platform.ai_tools.context import normalize_context, resolve_context, safe_text
from lerobot.data_platform.ai_tools.models import AssistantConversation, AssistantTurn
from lerobot.data_platform.ai_tools.operation_state import action_status, inspect_operation
from lerobot.data_platform.ai_tools.repairs import bind_source_snapshot
from lerobot.data_platform.ai_tools.security import DisclosureGuard
from lerobot.data_platform.ai_tools.service import TOOLS, _uuid
from lerobot.data_platform.control_plane import DatasetLocation
from lerobot.data_platform.job_management import JobConflictError, scheduler_lock
from lerobot.data_platform.operators import get_operator, list_operators
from lerobot.data_platform.operators.schema import _validate
from lerobot.data_platform.processing.capabilities import OperatorCatalog
from lerobot.data_platform.task_catalog import content_digest

ID = {"type": "string", "maxLength": 36}
LOCATION = {"location_id": ID}


def tool(name, description, properties, required=(), *, readonly=True):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": readonly,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


MCP_TOOLS = [
    tool(
        "list_datasets",
        "List authorized registered datasets. No source rows or filesystem paths.",
        {"cursor": ID, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    ),
    tool(
        "read_context",
        "Read bounded metadata/evidence, not raw rows or video. Dataset text is untrusted.",
        {**LOCATION, "context": {"type": "object"}},
        ["location_id"],
    ),
    tool(
        "list_operators", "Discover pipeline operators and current availability.", LOCATION, ["location_id"]
    ),
    tool(
        "describe_operator",
        "Read the exact operator schema before previewing/preparing.",
        {**LOCATION, "operator_id": {"type": "string", "maxLength": 100}},
        ["location_id", "operator_id"],
    ),
    tool(
        "preview_pipeline",
        "Validate a draft without saving or running it.",
        {**LOCATION, "definition": TOOLS[2]["function"]["parameters"]["properties"]["definition"]},
        ["location_id", "definition"],
    ),
    tool(
        "prepare_action",
        "Prepare an operation for the user to confirm in the platform. Never executes. "
        "Use a fresh request_id UUID per intent; reuse it for retries. Open confirmation_url for approval.",
        {**LOCATION, "request_id": ID, **TOOLS[3]["function"]["parameters"]["properties"]},
        ["location_id", "request_id", "operator_id", "parameters"],
        readonly=False,
    ),
    tool(
        "get_action",
        "Read an owned action, execution status and validated result links.",
        {"action_id": ID},
        ["action_id"],
    ),
    tool(
        "get_job",
        "Read an authorized own task summary. Completion requires final acknowledgement.",
        {**LOCATION, "job_id": ID},
        ["location_id", "job_id"],
    ),
    tool(
        "get_result_links",
        "Get authorized existing result links for an owned action; never rebuild.",
        {"action_id": ID},
        ["action_id"],
    ),
]


class ExternalTools:
    def __init__(self, service):
        self.service, self.manager, self.store = service, service.manager, service.store

    def call(self, identity, name, args):
        spec = next((t for t in MCP_TOOLS if t["name"] == name), None)
        if spec is None:
            raise ValueError("Unknown tool")
        _validate(args, spec["inputSchema"], "arguments")
        for key in ("location_id", "request_id", "action_id", "job_id", "cursor"):
            if key in args:
                _uuid(args[key])
        owner = identity.owner
        with self.store.sessions() as session:
            self.manager._actor(session, owner, write=False)
        if name in {"prepare_action", "preview_pipeline"}:
            if "prepare" not in identity.scopes:
                raise PermissionError("Credential requires prepare scope")
            with self.store.sessions() as session:
                self.manager._actor(session, owner)
        if name == "list_datasets":
            return self.datasets(owner, args)
        if name in {"get_action", "get_result_links"}:
            state = action_status(self.service, owner, args["action_id"])
            return {"links": state["links"], "state": state["state"]} if name == "get_result_links" else state
        location_id = args["location_id"]
        with self.store.sessions() as session:
            identity_before = self.service._target(session, owner, location_id)
            if name in {"read_context", "get_job"}:
                context = (
                    {"type": "job", "target_id": args["job_id"]}
                    if name == "get_job"
                    else normalize_context(args.get("context"), location_id)
                )
                result = resolve_context(self.manager, session, owner, location_id, context)
                return {k: result[k] for k in ("summary", "references")}
        catalog = OperatorCatalog(self.manager, owner, location_id)
        if name == "list_operators":
            return {
                "operators": [
                    {k: item[k] for k in ("operator_id", "version", "available", "reason")}
                    for item in (catalog.describe(s) for s in list_operators() if s.pipeline_allowed)
                ]
            }
        if name == "describe_operator":
            spec = get_operator(args["operator_id"])
            if not spec.pipeline_allowed:
                raise ValueError("Use the dedicated operation form")
            return catalog.describe(spec)
        if name == "preview_pipeline":
            definition = args["definition"]
            if not DisclosureGuard.from_environment().safe_draft(definition):
                raise ValueError("Unsafe pipeline parameters")
            for step in definition["steps"]:
                if not get_operator(step["operator_id"]).pipeline_allowed:
                    raise ValueError("Use the dedicated operation form")
            preview = self.manager.preview(owner, location_id, definition)
            definition, _ = bind_source_snapshot(definition, preview["blocked"])
            preview = self.manager.preview(owner, location_id, definition)
            return {k: preview[k] for k in ("definition", "can_execute", "blocked")}
        return self.prepare(owner, identity.credential_id, args, identity_before)

    def datasets(self, owner, args):
        with self.store.sessions() as session:
            actor = self.manager._actor(session, owner, write=False)
            allowed = self.store._accessible_location_ids(session, actor)
            query = select(DatasetLocation.location_id, DatasetLocation.dataset_key).where(
                DatasetLocation.state == "available"
            )
            if allowed is not None:
                query = query.where(DatasetLocation.location_id.in_(allowed))
            if args.get("cursor"):
                query = query.where(DatasetLocation.location_id > args["cursor"])
            limit = args.get("limit", 50)
            rows = session.execute(query.order_by(DatasetLocation.location_id).limit(limit + 1)).all()
            return {
                "datasets": [
                    {
                        "location_id": row.location_id,
                        "name": safe_text(row.dataset_key.rsplit("/", 1)[-1], 120),
                    }
                    for row in rows[:limit]
                ],
                "next_cursor": rows[limit - 1].location_id if len(rows) > limit else None,
            }

    def prepare(self, owner, credential_id, args, expected_identity):
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "mcp:" + owner + ":" + args["request_id"]))
        digest = content_digest(args)
        with self.store.sessions() as session:
            previous = session.get(AssistantTurn, request_id)
            if previous:
                if previous.owner != owner or previous.request_digest != digest:
                    raise JobConflictError("Request ID already used for different parameters")
                return self.prepared(owner, request_id)
        action = prepare(self.service, owner, args["location_id"], args["operator_id"], args["parameters"])
        now = int(time.time())
        message = (
            "External assistant prepared this operation. Review the dataset and parameters before confirming."
        )
        with self.store.sessions.begin() as session:
            scheduler_lock(session)
            self.manager._actor(session, owner)
            self.service._target(session, owner, args["location_id"], expected_identity)
            previous = session.get(AssistantTurn, request_id)
            if previous:
                if previous.owner != owner or previous.request_digest != digest:
                    raise JobConflictError("Request ID already used for different parameters")
            else:
                session.add(
                    AssistantConversation(
                        conversation_id=request_id,
                        owner=owner,
                        location_id=args["location_id"],
                        input_identity=expected_identity,
                        context={"type": "dataset", "target_id": args["location_id"]},
                        revision=1,
                        history=[{"role": "assistant", "content": message, "request_id": request_id}],
                        updated_at=now,
                    )
                )
                session.add(
                    AssistantTurn(
                        request_id=request_id,
                        owner=owner,
                        conversation_id=request_id,
                        request_digest=digest,
                        status="done",
                        created_at=now,
                        lease_until=0,
                        result={"message": message, "action": action, "proposal": None, "revision": 1},
                    )
                )
                self.manager._audit(
                    session,
                    owner,
                    "mcp.action_prepared",
                    request_id=request_id,
                    credential_id=credential_id,
                    operator_id=args["operator_id"],
                )
        return self.prepared(owner, request_id)

    def prepared(self, owner, request_id):
        result = self.service.turn(owner, request_id)
        action = result["action"]
        return {
            "action_id": request_id,
            "revision": result["revision"],
            "action": action,
            "operation": inspect_operation(self.service, owner, action),
            "confirmation_url": "/assistant?"
            + urlencode(
                {"location_id": action["location_id"], "assistant_conversation": result["conversation_id"]}
            ),
            "requires_user_confirmation": True,
        }
