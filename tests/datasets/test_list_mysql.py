"""Run the same projection/page contracts against an isolated real MySQL database."""

import os
import uuid

import pytest

from lerobot.data_platform.control_plane import ControlPlaneStore
from tests.datasets import test_job_management, test_list_pagination


@pytest.fixture
def mysql_context(tmp_path, monkeypatch):
    socket = os.environ.get("DATA_PLATFORM_TEST_MYSQL_SOCKET")
    if not socket:
        pytest.skip("Explicit isolated MySQL socket required")
    import pymysql

    database = "dp_archive_test_" + uuid.uuid4().hex
    connection = pymysql.connect(unix_socket=socket, user="root", autocommit=True)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{database}`")
    stores = []

    def factory(_url):
        store = ControlPlaneStore(f"mysql+pymysql://root@localhost/{database}?unix_socket={socket}")
        stores.append(store)
        return store

    monkeypatch.setattr(test_job_management, "ControlPlaneStore", factory)
    try:
        yield test_job_management.setup.__wrapped__(tmp_path)
    finally:
        for store in stores:
            store.engine.dispose()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{database}`")
        connection.close()


def test_mysql_cursor_parity(mysql_context):
    test_list_pagination.test_cursor_pages_and_filter_binding(mysql_context)


def test_mysql_projection_parity(mysql_context):
    test_list_pagination.test_summary_select_projects_reports_in_database(mysql_context)


def test_mysql_permission_before_pagination(mysql_context):
    test_list_pagination.test_permission_filter_precedes_pages_and_checks_hidden_sources(mysql_context)
