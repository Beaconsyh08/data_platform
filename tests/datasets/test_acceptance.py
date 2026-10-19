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
