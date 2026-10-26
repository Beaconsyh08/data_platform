"""Real execution provenance cannot be relabeled as a fresh acceptance run."""

import pytest

from lerobot.data_platform.acceptance import collect_matrix
from lerobot.data_platform.control_plane import RemoteJob
from tests.datasets.test_artifact_store import archive  # noqa: F401
from tests.datasets.test_job_management import setup  # noqa: F401
from tests.datasets.test_report_migration import completed


def test_matrix_validates_existing_attempt_and_original_release(setup, archive):  # noqa: F811
    store, job_id, _ = completed(setup)
    with store.sessions.begin() as session:
        job = session.get(RemoteJob, job_id)
        job.options = {"operator_snapshot": {"version": "1", "release": "release-a", "data_format": "v3.0"}}
        node_id = job.node_id
    item = {
        "operator": "viewer.prepare",
        "version": "1",
        "node_id": node_id,
        "format": "v3.0",
        "scenario": "nominal",
        "job_id": job_id,
        "attempt_id": "attempt-one",
    }
    report = collect_matrix(store, archive, [item], "release-a", changed_operators={"viewer.prepare"})
    assert report["cases"][0]["reused"] is False
    reused = collect_matrix(store, archive, [item], "release-b", changed_operators=set())
    assert reused["cases"][0]["source_release"] == "release-a"
    assert reused["cases"][0]["reused"] is True
    with pytest.raises(ValueError, match="fresh"):
        collect_matrix(store, archive, [item], "release-b", changed_operators={"viewer.prepare"})
    with pytest.raises(ValueError, match="snapshot"):
        collect_matrix(store, archive, [{**item, "format": "v2.1"}], "release-a", changed_operators=set())
    with pytest.raises(ValueError, match="Attempt"):
        collect_matrix(
            store, archive, [{**item, "attempt_id": "missing"}], "release-a", changed_operators=set()
        )


def test_installed_login_url_predicate_accepts_playwright_string_urls(tmp_path, monkeypatch):
    import json
    from unittest.mock import MagicMock

    from lerobot.data_platform.acceptance import installed_browser

    (tmp_path / "release.json").write_text(json.dumps({"release": "candidate"}))
    runtime = MagicMock()
    page = runtime.chromium.launch.return_value.new_page.return_value
    responses = []
    for payload in (
        {"release": "candidate", "environment": "dev", "maintenance": False, "instance_id": "dev-id"},
        {"jobs": []},
        {"pipelines": []},
        {"runs": []},
    ):
        response = MagicMock()
        response.ok = True
        response.json.return_value = payload
        responses.append(response)
    page.request.get.side_effect = responses
    manager = MagicMock()
    manager.__enter__.return_value = runtime
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: manager)
    result = installed_browser("https://example.test", tmp_path, "/browser", "test-user", "test-only")
    predicate = page.wait_for_url.call_args.args[0]
    assert not predicate("https://example.test/login")
    assert predicate("https://example.test/?page=datasets")
    assert result["passed"] and result["instance_id"] == "dev-id"
