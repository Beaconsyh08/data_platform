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
        row.options = {
            "operator_snapshot": {"large": "x" * 1000000},
            "registry": ["x" * 1000000],
            "local_compute": True,
        }
    sql = []

    def capture(conn, cursor, statement, parameters, context, many):
        sql.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        rows = store.list_jobs(actor=owner, summary=True)
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert rows[0]["result"] == {"progress": {"current": 2}}
    assert rows[0]["options"] == {"local_compute": True}
    assert "operator_snapshot" in store.get_job(job["job_id"])["options"]
    assert any("json_remove" in statement.lower() for statement in sql)
    assert "large" in store.get_job(job["job_id"])["result"]["precompute"]


def test_permission_filter_precedes_pages_and_checks_hidden_sources(setup):  # noqa: F811
    store, admin, owner, node, location, create = setup
    other = store.sync_locations(node["node_id"], [{"root": "/data/other", "dataset_key": "other"}])[0]
    visible = create()
    hidden = create()
    with store.sessions.begin() as session:
        session.get(RemoteJob, hidden["job_id"]).options = {"_source_locations": [other]}
    # Internal summaries also feed queue/conflict decoration, including legacy jobs
    # whose additional inputs exist only in _source_locations.
    internal = next(
        row for row in store.list_jobs(actor=admin, summary=True) if row["job_id"] == hidden["job_id"]
    )
    assert internal["options"]["_source_locations"][0]["location_id"] == other["location_id"]
    from lerobot.data_platform.control_plane_summaries import summarize_job

    assert "_source_locations" not in summarize_job(internal)["options"]
    store.set_mutation_locations(owner["user_id"], [location["location_id"]], actor=admin)
    rows, page = store.list_jobs(actor=owner, summary=True, page=ListPage.parse({"page_size": 1}, scope={}))
    assert [row["job_id"] for row in rows] == [visible["job_id"]]
    assert not page["has_more"]


def test_local_job_filter_precedes_limit_and_preserves_ownership(setup):  # noqa: F811
    store, admin, owner, _, _, create = setup
    local = create()
    with store.sessions.begin() as session:
        session.get(RemoteJob, local["job_id"]).options = {"local_compute": True}
    create()  # A newer remote job must not hide the older local one.
    assert [r["job_id"] for r in store.list_jobs(actor=owner, local_only=True, limit=1)] == [local["job_id"]]
    assert store.list_jobs(actor={"user_id": admin["user_id"], "role": "operator"}, local_only=True) == []
