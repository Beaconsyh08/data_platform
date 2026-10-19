"""Real Chrome acceptance for the authenticated pipeline editor and Agent artifact protocol.

Requires Playwright and DATA_PLATFORM_BROWSER_EXECUTABLE (an installed Chrome/Chromium).
"""

import json
import os
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.serving import make_server

from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.operators.registry import operation_ids, worker_manifest
from lerobot.data_platform.routes.curation import register_curation_routes
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_data_platform_agent import _agent
from tests.datasets.test_quality_eef import make_eef


def test_pipeline_browser_edit_run_restore_review_and_role(tmp_path):
    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set DATA_PLATFORM_BROWSER_EXECUTABLE for real-browser acceptance")
    playwright = pytest.importorskip("playwright.sync_api")
    control = _store(tmp_path)
    app = _app(tmp_path, control)
    client = app.test_client()
    ledger = LifecycleStore(tmp_path / "ledger")
    ctx = SimpleNamespace(control_plane_store=control, lifecycle_store=ledger)
    register_curation_routes(app, ctx)
    register_processing_routes(app, ctx)
    assert _bootstrap(client).status_code == 200
    source = make_eef(tmp_path / "source")
    token, node = control.enroll_node(
        name="browser-node",
        hostname="localhost",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        enrollment_token="test",
        expected_token="test",
        capabilities={
            **worker_manifest(),
            "operations": sorted(operation_ids(remote=True)),
            "job_protocol": 2,
            "data_profile_protocol": 100,
            "curation_protocol": 1,
            "curation_operations": sorted(operation_ids(family="curation")),
            "quality_checks": ["eef", "s3", "c3"],
        },
    )
    location = control.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "browser/source",
                "root": str(source),
                "output_dir": str(tmp_path / "cache"),
                "metadata": json.loads((source / "meta/info.json").read_text()),
            }
        ],
    )[0]
    manager = app.extensions["data_platform_processing"]
    owner = control.list_users()[0]["user_id"]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable, args=["--no-sandbox", "--disable-gpu"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.request.post(
                base + "/api/auth/login",
                data={"username": "platform-admin", "password": "strong-admin-password"},
            )
            assert response.status == 200
            page.goto(base + "/pipelines?location_id=" + location["location_id"])
            page.get_by_role("button", name="Add step", exact=True).click()
            page.locator("article").nth(0).get_by_label("Operator", exact=True).select_option(
                "curation.snapshot"
            )
            page.get_by_role("button", name="Add step", exact=True).click()
            page.locator("article").nth(1).get_by_label("Operator", exact=True).select_option(
                "preprocess.split"
            )
            page.locator("article").nth(1).get_by_label("episode_range", exact=True).fill("0:1")
            page.locator("article").nth(1).get_by_label("episode_range", exact=True).press("Tab")
            page.get_by_role("button", name="Add step", exact=True).click()
            page.locator("article").nth(2).get_by_label("Operator", exact=True).select_option(
                "preprocess.split"
            )
            page.locator("article").nth(2).get_by_role("button", name="↑", exact=True).click()
            page.locator("article").nth(1).get_by_role("button", name="↓", exact=True).click()
            source_step = page.locator("article").nth(1).locator("strong").inner_text().split(". ", 1)[1]
            page.locator("article").nth(2).get_by_label("Dataset input", exact=True).select_option(
                source_step
            )
            page.locator("article").nth(2).get_by_label("Review before this step", exact=True).check()
            page.get_by_label("Name", exact=True).fill("Browser pipeline")
            page.get_by_role("button", name="Save version", exact=True).click()
            page.wait_for_function("!document.getElementById('run').disabled")
            # Invalid JSON must invalidate the previously executable preview.
            page.locator("article").nth(1).get_by_text("Advanced parameters", exact=True).click()
            field = page.locator("article").nth(1).get_by_label("task_filter", exact=True)
            field.fill("[")
            field.press("Tab")
            assert page.locator("#run").is_disabled()
            field.fill("null")
            field.press("Tab")
            page.get_by_role("button", name="Save version", exact=True).click()
            page.wait_for_function("!document.getElementById('run').disabled")
            page.get_by_role("button", name="Run saved version", exact=True).click()
            page.wait_for_url("**&run_id=*")
            run_id = parse_qs(urlsplit(page.url).query)["run_id"][0]

            def execute_next():
                manager.tick(run_id)
                job = control.claim_job(node["node_id"], worker_instance_id="browser-worker")
                assert job
                headers = {
                    "Authorization": "Bearer " + token,
                    "X-Job-Attempt": job["execution"]["attempt_id"],
                    "X-Job-Credential": job["execution"]["credential"],
                }
                agent = _agent(tmp_path, tmp_path, tmp_path)

                def upload(_state, _job, name, path, **kwargs):
                    response = client.put(
                        f"/api/agents/jobs/{job['job_id']}/curation-artifacts/{name}",
                        headers=headers,
                        data=path.read_bytes(),
                    )
                    assert response.status_code == 200, response.get_json()

                agent.client.upload_artifact = upload
                result = agent.execute_job(job)
                response = client.post(
                    f"/api/agents/jobs/{job['job_id']}/complete",
                    headers=headers,
                    json={"status": "done", "result": result},
                )
                assert response.status_code == 200, response.get_json()
                manager.tick(run_id)

            execute_next()
            execute_next()
            assert manager.run(owner, run_id)["status"] == "waiting_review"
            page.reload()
            page.get_by_role("button", name="Approve", exact=True).wait_for()
            assert page.locator("#runs").input_value() == run_id
            page.get_by_role("button", name="Approve", exact=True).click()
            page.wait_for_function("!document.querySelector('#run-steps button')")
            execute_next()
            page.reload()
            page.wait_for_function("document.getElementById('run-status').textContent === 'done'")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            control.register_user(
                username="browser-viewer", password="strong-viewer-password", display_name="Viewer"
            )
            page.request.post(base + "/api/auth/logout", data={})
            assert (
                page.request.post(
                    base + "/api/auth/login",
                    data={"username": "browser-viewer", "password": "strong-viewer-password"},
                ).status
                == 200
            )
            page.goto(base + "/pipelines?location_id=" + location["location_id"])
            assert page.get_by_role("button", name="Add step", exact=True).is_disabled()
            assert page.request.get(base + "/api/pipeline-runs/" + run_id).status == 404
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
