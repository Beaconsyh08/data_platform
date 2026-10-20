"""Real Chrome acceptance of draft loading, recovery, scope switching and explicit execution."""

import os
import threading
from types import SimpleNamespace

import pytest
from werkzeug.serving import make_server

from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_ai_assistant import (
    assistant,  # noqa: F401
    processing_setup,  # noqa: F401
)
from tests.datasets.test_control_plane import _app


def test_qwen_browser_draft_restore_scope_and_explicit_run(assistant, tmp_path):  # noqa: F811
    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set DATA_PLATFORM_BROWSER_EXECUTABLE for real-browser acceptance")
    playwright = pytest.importorskip("playwright.sync_api")
    service, fake, owner, value = assistant
    app = _app(tmp_path, service.store)
    ctx = SimpleNamespace(control_plane_store=service.store, lifecycle_store=None)
    register_processing_routes(app, ctx)
    register_ai_assistant_routes(app, ctx)
    app.extensions["data_platform_assistant"] = service
    first = service.store.get_location(value["location_id"])
    second = service.store.sync_locations(
        first["node_id"],
        [
            {
                "dataset_key": "node/second",
                "root": str(tmp_path / "second"),
                "output_dir": str(tmp_path / "second-cache"),
                "metadata": first["metadata"],
            }
        ],
    )[0]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    release, entered = threading.Event(), threading.Event()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable, args=["--no-sandbox", "--disable-gpu"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            assert (
                page.request.post(
                    base + "/api/auth/login",
                    data={
                        "username": "admin",
                        "password": "strong-admin-password",
                    },
                ).status
                == 200
            )
            page.goto(base + "/pipelines?location_id=" + first["location_id"])
            page.locator("#ai-panel > summary").click()
            page.locator("#ai-message").fill("取第一个 episode")
            page.get_by_role("button", name="Ask Qwen", exact=True).click()
            page.get_by_role("button", name="Load draft into editor", exact=True).wait_for()
            assert service.store.list_jobs() == []
            assert page.locator("#steps article").count() == 0
            page.reload()
            page.locator("#ai-panel > summary").click()
            page.get_by_role("button", name="Load draft into editor", exact=True).click()
            playwright.expect(page.locator("#steps article")).to_have_count(1)
            assert page.get_by_role("button", name="Run saved version", exact=True).is_disabled()
            page.get_by_label("episode_range", exact=True).fill("0:2")
            page.get_by_label("episode_range", exact=True).press("Tab")
            page.get_by_role("button", name="Save version", exact=True).click()
            playwright.expect(
                page.get_by_role("button", name="Run saved version", exact=True)
            ).to_be_enabled()
            assert service.store.list_jobs() == []
            page.get_by_role("button", name="Run saved version", exact=True).click()
            playwright.expect(page.locator("#run-panel")).to_be_visible()
            run_id = service.manager.runs(owner)[0]["run_id"]
            service.manager.tick(run_id)
            assert len(service.store.list_jobs()) == 1

            # A slow answer from the previous dataset must not populate the newly selected context.
            def slow(_payload):
                entered.set()
                assert release.wait(15)
                return {"choices": [{"message": {"content": "Old dataset answer"}}]}

            fake.post_chat_completion = slow
            page.locator("#ai-message").fill("继续")
            page.get_by_role("button", name="Ask Qwen", exact=True).click()
            assert entered.wait(10)
            page.get_by_label("Dataset", exact=True).select_option(second["location_id"])
            release.set()
            playwright.expect(page.locator("#ai-message")).to_be_enabled()
            assert "Old dataset answer" not in page.locator("#ai-history").inner_text()
            assert page.locator("#ai-proposal").is_hidden()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            viewer = service.store.register_user(
                username="viewer", password="strong-viewer-password", display_name="Viewer"
            )
            service.store.update_user(viewer["user_id"], active=True)
            page.request.post(base + "/api/auth/logout", data={})
            page.request.post(
                base + "/api/auth/login", data={"username": "viewer", "password": "strong-viewer-password"}
            )
            page.goto(base + "/pipelines?location_id=" + first["location_id"])
            page.locator("#ai-panel > summary").click()
            playwright.expect(page.locator("#ai-status")).to_contain_text("read-only")
            assert page.get_by_role("button", name="Ask Qwen", exact=True).is_disabled()
            assert page.locator("#ai-history").inner_text() == ""
            assert not errors
            browser.close()
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=10)
