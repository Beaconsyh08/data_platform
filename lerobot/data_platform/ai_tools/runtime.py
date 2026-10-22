"""Durable generation with existing authorization, tools and bounded Qwen protocol.

Version 1 checkpoints the bounded generation node and journals each provider call.
On recovery, read/prepare tools are revalidated and saved responses replayed only
when the complete authorized request digest matches. No execution tools exist.
"""

import json
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from lerobot.data_platform.ai_tools.runtime_checkpointer import RuntimeSaver
from lerobot.data_platform.ai_tools.security import REFUSAL, DisclosureGuard, restricted_request
from lerobot.data_platform.ai_tools.service import SYSTEM, AssistantService


class GenerationState(TypedDict, total=False):
    result: dict


class RecordedQwenClient:
    def __init__(self, factory, repository, lease, **options):
        self.factory, self.repository, self.lease = factory, repository, lease
        self.options = options
        self.timeout_s = options["timeout_s"]
        self.index = 0

    def post_chat_completion(self, payload):
        response = self.repository.reserve_call(self.lease, self.index, payload)
        if response is None:
            remaining = self.repository.check_budget(self.lease)
            client = self.factory(**{**self.options, "timeout_s": min(self.timeout_s, remaining)})
            response = DisclosureGuard.from_environment().tree(client.post_chat_completion(payload))
            if len(json.dumps(response, ensure_ascii=False).encode()) > 262144:
                raise ValueError("Provider response exceeds limit")
            self.repository.record_response(self.lease, self.index, response)
        self.index += 1
        return response


def generate(service, repository, lease):
    owner, model, payload = repository.snapshot(lease)

    def check():
        repository.check_budget(lease)
        with service.store.sessions() as session:
            service._target(session, owner, payload["value"]["location_id"], payload["identity"])

    def run(state):
        check()
        current = AssistantService(
            service.manager,
            config=replace(service.config, model=model),
            client_factory=lambda **options: RecordedQwenClient(
                service.client_factory, repository, lease, **options
            ),
        )
        if restricted_request(payload["value"]["message"]):
            result = {"message": REFUSAL, "proposal": None, "security": {"code": "restricted_request"}}
        else:
            result = current._generate(
                owner,
                payload["value"],
                payload["history"],
                payload["proposal"],
                payload["identity"],
                execution_guard=check,
            )
        check()
        return {"result": DisclosureGuard.from_environment().response(result, instructions=SYSTEM)}

    builder = StateGraph(GenerationState)
    builder.add_node("generate", run)
    builder.add_edge(START, "generate")
    builder.add_edge("generate", END)
    saver = RuntimeSaver(repository, lease)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": lease.request_id}}
    check()
    # Do not export private assistant state via ambient LangSmith configuration.
    with tracing_context(enabled=False):
        result = graph.invoke(None if saver.get_tuple(config) else {}, config, durability="sync")
    check()
    return result["result"]
