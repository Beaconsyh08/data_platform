"""Server-resolved attribution cannot be forged by a client-supplied strategy ID."""

from lerobot.data_platform.ai_tools.models import AssistantTurn
from lerobot.data_platform.ai_tools.strategies import Strategies
from lerobot.data_platform.processing.planner import PipelineSpec


def resolve(manager, session, owner, request_id, spec):
    if not request_id:
        return None
    turn = session.get(AssistantTurn, request_id)
    if not turn or turn.owner != owner or turn.status != "done":
        raise KeyError("Assistant request not found")
    strategy = turn.result.get("strategy")
    if not strategy:
        return None
    from lerobot.data_platform.ai_tools.service import AssistantService

    Strategies(AssistantService(manager)).check(session, strategy)
    proposed = turn.result.get("action") or turn.result.get("proposal")
    if not proposed:
        raise ValueError("Request did not propose a pipeline")
    return {
        **strategy,
        "request_id": request_id,
        "user_modified": PipelineSpec.from_dict(proposed["definition"]).digest != spec.digest,
    }
