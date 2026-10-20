"""Real-browser compatibility of composed console modules and the Pipeline subview."""

import json
import os
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from lerobot.data_platform.operators.registry import operation_ids, worker_manifest
from tests.datasets.test_ai_assistant import FakeQwen, call, draft
from tests.datasets.test_console_services import console_app
from tests.datasets.test_control_plane import _bootstrap, _store
from tests.datasets.test_quality_eef import make_eef


def test_console_browse_forms_pipeline_switching_and_viewer_role(tmp_path, monkeypatch):
    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set DATA_PLATFORM_BROWSER_EXECUTABLE for real-browser acceptance")
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    store = _store(tmp_path)
    app = console_app(tmp_path, database_url=str(store.engine.url))
    plan = draft()
    plan["steps"].insert(
        0, {"step_id": "snapshot", "operator_id": "curation.snapshot", "version": "1.0.0", "parameters": {}}
    )
    fake_qwen = FakeQwen(
        [
            call("describe_operator", {"operator_id": "preprocess.split"}),
            call("describe_operator", {"operator_id": "curation.snapshot"}),
            call("preview_pipeline", {"summary": "First episode", "definition": plan}),
        ]
    )
    app.extensions["data_platform_assistant"].client_factory = lambda **kwargs: fake_qwen
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    source = make_eef(tmp_path / "source")
    _, node = store.enroll_node(
        name="console-node",
        hostname="localhost",
        allowed_roots=[str(tmp_path)],
        writable_roots=[str(tmp_path)],
        enrollment_token="test",
        expected_token="test",
        capabilities={
            **worker_manifest(),
            "operations": sorted(operation_ids(remote=True)),
            "job_protocol": 2,
            "curation_protocol": 1,
            "data_profile_protocol": 100,
        },
    )
    locations = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": f"console/source-{index}",
                "root": str(source if index == 0 else tmp_path / "other"),
                "output_dir": str(tmp_path / f"cache-{index}"),
                "metadata": json.loads((source / "meta/info.json").read_text()),
            }
            for index in range(2)
        ],
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable, args=["--no-sandbox", "--disable-gpu"]
            )
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            assets = os.environ.get("DATA_PLATFORM_BROWSER_ASSETS")
            if assets:
                page.route(
                    "https://cdn.tailwindcss.com/**",
                    lambda route: route.fulfill(
                        path=str(Path(assets) / "ui-tailwind.js"), content_type="application/javascript"
                    ),
                )
                page.route(
                    "https://cdn.jsdelivr.net/npm/alpinejs@*/dist/cdn.min.js",
                    lambda route: route.fulfill(
                        path=str(Path(assets) / "ui-alpine.js"), content_type="application/javascript"
                    ),
                )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            assert (
                page.request.post(
                    base + "/api/auth/login",
                    data={"username": "platform-admin", "password": "strong-admin-password"},
                ).status
                == 200
            )
            page.goto(base + "/?page=preprocessing&tab=pipelines")
            page.wait_for_function("window.Alpine && document.querySelector('[x-data]')._x_dataStack")
            page.get_by_label("Dataset", exact=True).select_option(locations[0]["location_id"])
            page.get_by_role("button", name="Add step", exact=True).click()
            page.get_by_label("Operator", exact=True).select_option("curation.snapshot")
            page.get_by_role("button", name="Add step", exact=True).click()
            page.get_by_label("Operator", exact=True).select_option("preprocess.split")
            page.get_by_label("episode_range", exact=True).fill("0:1")
            page.get_by_label("Name", exact=True).fill("Console pipeline")
            page.get_by_role("button", name="Save version", exact=True).click()
            page.wait_for_function(
                "document.querySelector('[x-effect=\"syncPipelineWorkspace($el)\"]').shadowRoot.querySelector('#saved').value !== ''"
            )
            page.locator("#ai-message").fill("提取第一个 episode")
            page.get_by_role("button", name="Ask Qwen", exact=True).click()
            page.get_by_role("button", name="Load draft into editor", exact=True).click()
            playwright.expect(page.get_by_label("Name", exact=True)).to_have_value("First episode")
            assert page.locator("#run").is_disabled()
            assert not store.list_jobs()
            # Changing the active dataset rebuilds the embedded editor, never reuses the old preview.
            page.evaluate(
                "async id => {const app=Alpine.$data(document.querySelector('[x-data]')); await app.useRemoteDataset(app.remoteLocations.find(x=>x.location_id===id));}",
                locations[1]["location_id"],
            )
            page.get_by_label("Dataset", exact=True).wait_for()
            page.wait_for_function(
                "id => document.querySelector('[x-effect=\"syncPipelineWorkspace($el)\"]').shadowRoot.querySelector('#location').value === id",
                arg=locations[1]["location_id"],
            )
            assert page.locator("#run").is_disabled()
            # Existing form state survives navigation between modules.
            page.evaluate("async () => {await Alpine.$data(document.body).selectTab('transform');}")
            page.locator('input[type="radio"][value="smooth_action"]').check()
            page.locator(r'input[x-model\.number="preprocess.smooth_window"]').fill("13")
            page.evaluate(
                "async () => {const app=Alpine.$data(document.body);await app.selectPage('runs');await app.selectTab('transform');}"
            )
            assert (
                page.evaluate("Alpine.$data(document.querySelector('[x-data]')).preprocess.smooth_window")
                == 13
            )
            page.get_by_role("link", name="Pipelines", exact=True).click()
            page.get_by_role("button", name="Add step", exact=True).wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            store.register_user(
                username="console-viewer", password="strong-viewer-password", display_name="Viewer"
            )
            page.request.post(base + "/api/auth/logout", data={})
            assert (
                page.request.post(
                    base + "/api/auth/login",
                    data={"username": "console-viewer", "password": "strong-viewer-password"},
                ).status
                == 200
            )
            page.goto(base + "/?page=preprocessing&tab=pipelines")
            assert page.get_by_role("button", name="Add step", exact=True).is_disabled()
            assert page.get_by_role("button", name="Save version", exact=True).is_disabled()
            assert page.request.post(base + "/api/pipelines", data={"definition": {}}).status == 403
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
