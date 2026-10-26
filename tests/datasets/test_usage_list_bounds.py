"""Usage lists avoid full audit payloads; summaries explicitly disclose their bounded scope."""

from sqlalchemy import event

from lerobot.data_platform.management_storage import UsageLogStore


def test_usage_summary_projection_order_and_lazy_detail(tmp_path):
    logs = UsageLogStore(f"sqlite:///{tmp_path / 'usage.db'}")
    for index in range(5):
        logs.append(
            {
                "event_id": str(index),
                "timestamp": "2026-10-21T00:00:00+00:00",
                "actor": {"user_id": "owner", "username": "Operator"},
                "operation": "test",
                "status": "success",
                "details": {"duration_ms": index * 10, "report": "x" * 50000},
            }
        )
    sql = []

    def capture(conn, cursor, statement, parameters, context, many):
        sql.append(statement)

    event.listen(logs.engine, "before_cursor_execute", capture)
    try:
        result = logs.query({}, summary=True, limit=2)
    finally:
        event.remove(logs.engine, "before_cursor_execute", capture)
    assert [row["event_id"] for row in result["events"]] == ["4", "3"]
    assert result["has_more"] and "report" not in str(result)
    assert "dp_usage_events.event_id DESC" in sql[-1]
    assert ", dp_usage_events.payload " not in sql[-1]
    assert logs.get("4")["details"]["report"]
    assert logs.get("missing") is None
    assert logs.query({}, limit=1)["events"][0]["details"]["report"]
    recent = logs.summary({"sample_limit": "2"})
    assert recent["event_count"] == 2 and recent["truncated"]
    assert recent["active_users"] == 1 and recent["average_request_ms"] == 35
    assert recent["since"] and recent["until"]
    assert logs.summary({})["event_count"] == 5
    assert logs.summary({"operation": "missing", "sample_limit": "2"})["event_count"] == 0
