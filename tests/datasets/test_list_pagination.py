"""Cursor pages remain permission scoped and avoid materializing report JSON."""

import pytest
from sqlalchemy import event

from lerobot.data_platform.control_plane import RemoteJob
from lerobot.data_platform.list_queries import ListPage
from tests.datasets.test_job_management import setup  # noqa: F401


def test_cursor_pages_and_filter_binding(setup):  # noqa: F811
    store, admin, owner, node, location, _ = setup
    jobs = [
        store.create_job(
            location_id=location["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
        )
        for _ in range(7)
    ]
    scope = {"actor": owner["user_id"]}
    page = ListPage.parse({"page_size": 3}, scope=scope)
    rows, info = store.list_jobs(actor=owner, summary=True, page=page)
    assert len(rows) == 3 and info["has_more"]
    seen = [r["job_id"] for r in rows]
    while info["has_more"]:
        rows, info = store.list_jobs(
            actor=owner,
            summary=True,
            page=ListPage.parse({"page_size": 3, "cursor": info["next_cursor"]}, scope=scope),
        )
        seen.extend(r["job_id"] for r in rows)
    assert len(seen) == len(set(seen)) == len(jobs)
    first, info = store.list_jobs(actor=owner, summary=True, page=page)
    with pytest.raises(ValueError):
        ListPage.parse({"cursor": info["next_cursor"]}, scope={"actor": admin["user_id"]})


def test_summary_select_projects_reports_in_database(setup):  # noqa: F811
    store, _, owner, node, location, _ = setup
    job = store.create_job(
        location_id=location["location_id"], requested_by=owner["user_id"], operation="viewer.prepare"
    )
    with store.sessions.begin() as session:
        row = session.get(RemoteJob, job["job_id"])
        row.result = {"precompute": {"large": "x" * 1000000}, "progress": {"current": 2}}
    sql = []

    def capture(conn, cursor, statement, parameters, context, many):
        sql.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        rows = store.list_jobs(actor=owner, summary=True)
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert rows[0]["result"] == {"progress": {"current": 2}}
    assert any("json_remove" in statement.lower() for statement in sql)
    assert "large" in store.get_job(job["job_id"])["result"]["precompute"]
