"""Real-browser compatibility of composed console modules and the Pipeline subview."""

import json
import os
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from lerobot.data_platform.operation_log import append_operation_event
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
            page.get_by_label("Operator", exact=True).select_option("preprocess.split")
            page.get_by_label("episode_range", exact=True).fill("0:1")
            page.get_by_label("Name", exact=True).fill("Console pipeline")
            page.get_by_role("button", name="Save version", exact=True).click()
            page.wait_for_function(
                "document.querySelector('[x-effect=\"syncPipelineWorkspace($el)\"]').shadowRoot.querySelector('#saved').value !== ''"
            )
            playwright.expect(page.locator("#steps article")).to_have_count(1)
            assert "Snapshot" not in page.locator("#preview-result").inner_text()
            assert "curation.snapshot" not in page.get_by_label("Operator", exact=True).inner_text()
            page.locator("#pipeline-ask-ai").click()
            assert page.get_by_role("button", name="Ask AI", exact=True).count() == 0
            # The header must respond to the remaining workspace width, not the viewport.
            page.get_by_role("button", name="Open jobs", exact=True).click(timeout=3000)
            page.keyboard.press("Escape")
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            page.locator("#ai-message").fill("提取第一个 episode")
            page.get_by_role("button", name="Ask Qwen", exact=True).click()
            page.get_by_role("button", name="Load draft into editor", exact=True).click()
            playwright.expect(page.get_by_label("Name", exact=True)).to_have_value("First episode")
            playwright.expect(page.locator("#steps article")).to_have_count(1)
            assert "curation.snapshot" not in page.locator("#ai-proposal-summary").inner_text()
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
            # Lists stay small, and expanding one audit event fetches its complete record.
            audit = append_operation_event(
                tmp_path / "console/static",
                "browser_audit",
                status="success",
                details={"result": ["audit-detail-marker" * 100] * 100},
            )
            page.evaluate(
                "async () => {const app=Alpine.$data(document.body); app.runsFilter.scope='all'; await app.selectPage('runs'); await app.loadOperationLogs();}"
            )
            assert page.evaluate("Alpine.$data(document.body).operationLogs.every(e=>e.summary)")
            entry = page.locator("details").filter(has=page.locator("summary", has_text="browser audit"))
            with page.expect_response(lambda r: "/api/operation_log?event_id=" in r.url):
                entry.locator("summary").click()
            playwright.expect(entry.locator("pre")).to_contain_text("audit-detail-marker")
            assert (
                page.evaluate(
                    "Alpine.$data(document.body).operationLogs.find(e=>e.operation==='browser_audit').fullDetail.event_id"
                )
                == audit["event_id"]
            )
            page.evaluate("Alpine.$data(document.body).loadOperationLogs()")
            playwright.expect(entry.locator("pre")).to_contain_text("audit-detail-marker")
            page.route("**/api/operation_log?**", lambda route: route.abort())
            page.evaluate("Alpine.$data(document.body).loadOperationLogs()")
            assert page.evaluate("Boolean(Alpine.$data(document.body).operationLogsError)")
            assert page.evaluate("Alpine.$data(document.body).operationLogs.length > 0")
            page.unroute("**/api/operation_log?**")
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
            page.goto(base + "/assistant?type=dataset&location_id=" + locations[0]["location_id"])
            page.locator("#ai-prefs-toggle").click()
            playwright.expect(page.locator("#ai-prefs-save")).to_be_enabled()
            page.locator("#ai-pref-language").select_option("en")
            page.locator("#ai-prefs-save").click()
            playwright.expect(page.locator("#ai-prefs-status")).to_have_text("Preferences saved.")
            page.locator("#ai-prefs-clear").click()
            playwright.expect(page.locator("#ai-prefs-status")).to_have_text("Preferences cleared.")
            assert not store.list_jobs()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_environment_banner_wraps_long_release_and_account(environment):
    """Long immutable release IDs must remain readable without horizontal page scrolling."""
    from jinja2 import Template

    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set DATA_PLATFORM_BROWSER_EXECUTABLE for real-browser acceptance")
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[2] / "lerobot/data_platform"
    release = "V0.3.14_HindsightReusableExperience_20261023"
    username = "development-acceptance-" + "account" * 8
    header = Template((root / "templates/data_platform_environment.html").read_text()).render(
        environment=environment, release=release
    )
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("http://banner.test/", lambda r: r.fulfill(body=f"<head>{header}</head><body></body>"))
        page.route("**/environment.js", lambda r: r.fulfill(path=str(root / "static/environment.js")))
        page.route(
            "**/api/auth/status",
            lambda r: r.fulfill(json={"user": {"user_id": "test", "username": username, "role": "operator"}}),
        )
        page.route(
            "**/api/dev/production",
            lambda r: r.fulfill(json={"prod": {"release": release}, "comparison": "same"}),
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://banner.test/")
        playwright.expect(page.locator("#dp-environment-bar")).to_contain_text(username)
        for width in (320, 390, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator("#dp-environment-bar strong").inner_text().endswith(release)
            assert page.locator("#dp-environment-bar").evaluate("el => el.scrollWidth <= el.clientWidth")
        assert not errors
        browser.close()
