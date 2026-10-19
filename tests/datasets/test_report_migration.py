"""Migration must leave live work alone and restore byte-equivalent JSON evidence."""

from dataclasses import replace

import pytest

from lerobot.data_platform.control_plane import RemoteJob, _utcnow
from lerobot.data_platform.management_storage import JobAttempt, JobControl
from lerobot.data_platform.report_archive import archived_fields, hydrate_result
from lerobot.data_platform.report_migration import ReportMigrator
from tests.datasets.test_artifact_store import archive  # noqa: F401
from tests.datasets.test_job_management import setup  # noqa: F401


def completed(context):
    store, _, _, node, _, create = context
    job = create()
    report = {"precompute": {"data": "a" * 300000}, "progress": {"current": 1}, "output_dir": "/data/output"}
    with store.sessions.begin() as session:
        row = session.get(RemoteJob, job["job_id"])
        row.status, row.finished_at, row.result = "done", _utcnow(), report
        control = session.get(JobControl, row.job_id)
        control.phase, control.stop_confirmed, control.attempt_id = "done", True, "attempt-one"
        session.add(
            JobAttempt(
                attempt_id="attempt-one",
                job_id=row.job_id,
                attempt_no=1,
                worker_instance_id="worker",
                credential_digest="test",
                protocol=2,
                status="done",
                finished_at=1,
                result=report,
            )
        )
    return store, job["job_id"], report


def test_migration_deduplicates_and_restores(setup, archive):  # noqa: F811
    store, job_id, original = completed(setup)
    archive.config = replace(archive.config, migration_enabled=True)
    migrator = ReportMigrator(store, archive)
    assert migrator.migrate(job_id) > 600000
    job = store.get_job(job_id)
    assert archived_fields(job["result"]) == ["precompute"]
    assert len(archive.transport.objects) == 1
    assert hydrate_result(job["result"], archive) == original
    summary = store.list_jobs(summary=True)[0]
    assert summary["archived_reports"] == ["precompute"]
    assert "precompute" not in summary["result"]
    assert migrator.migrate(job_id) == 0
    assert migrator.restore(job_id)
    assert store.get_job(job_id)["result"] == original
    assert not migrator.restore(job_id)


def test_changed_record_during_upload_is_not_overwritten(setup, archive):  # noqa: F811
    store, job_id, original = completed(setup)
    archive.config = replace(archive.config, migration_enabled=True)
    put = archive.transport.put

    journal = archive.root / "migrations" / f"{job_id}.json"

    def change(key, path):
        put(key, path)
        journal.write_text('{"previous_journal": true}')
        with store.sessions.begin() as session:
            row = session.get(RemoteJob, job_id)
            row.result = {**row.result, "updated_evidence": True}

    archive.transport.put = change
    assert ReportMigrator(store, archive).migrate(job_id) == 0
    assert store.get_job(job_id)["result"] == {**original, "updated_evidence": True}
    assert journal.read_text() == '{"previous_journal": true}'


def test_disabled_and_live_dependencies_preserve_inline(setup, archive):  # noqa: F811
    store, job_id, original = completed(setup)
    migrator = ReportMigrator(store, archive)
    with pytest.raises(ValueError, match="disabled"):
        migrator.migrate(job_id)
    archive.config = replace(archive.config, migration_enabled=True)
    setup[-1]()
    assert migrator.migrate(job_id) == 0
    assert store.get_job(job_id)["result"] == original
    assert not archive.transport.objects


def test_restore_refuses_changed_records(setup, archive):  # noqa: F811
    store, job_id, _ = completed(setup)
    archive.config = replace(archive.config, migration_enabled=True)
    migrator = ReportMigrator(store, archive)
    migrator.migrate(job_id)
    with store.sessions.begin() as session:
        row = session.get(RemoteJob, job_id)
        row.result = {**row.result, "late_update": True}
    with pytest.raises(ValueError, match="changed"):
        migrator.restore(job_id)


def test_rollback_requires_inline_restore(setup, archive):  # noqa: F811
    from lerobot.data_platform.report_archive import require_reader_compatibility

    store, job_id, _ = completed(setup)
    archive.config = replace(archive.config, migration_enabled=True)
    migrator = ReportMigrator(store, archive)
    migrator.migrate(job_id)
    require_reader_compatibility(store, 1)
    with pytest.raises(RuntimeError, match="Restore inline"):
        require_reader_compatibility(store, 0)
    migrator.restore(job_id)
    require_reader_compatibility(store, 0)
