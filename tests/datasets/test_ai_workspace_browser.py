"""Real browser checks for the nonmodal dock and shared account settings."""

import os
import threading
from types import SimpleNamespace

import pytest
from flask import render_template_string
from werkzeug.serving import make_server

from lerobot.data_platform.ai_tools.personalization import preferences
from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.processing import register_processing_routes
from tests.datasets.test_ai_assistant import assistant, processing_setup  # noqa: F401
from tests.datasets.test_control_plane import _app


def test_workspace_remains_interactive_and_restores_shared_history_preferences(assistant, tmp_path):  # noqa: F811
    executable = os.environ.get("DATA_PLATFORM_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set DATA_PLATFORM_BROWSER_EXECUTABLE for real browser acceptance")
    playwright = pytest.importorskip("playwright.sync_api")
    service, fake, owner, value = assistant
    app = _app(tmp_path, service.store)
    ctx = SimpleNamespace(control_plane_store=service.store, lifecycle_store=None)
    register_processing_routes(app, ctx)
    register_ai_assistant_routes(app, ctx)
    app.extensions["data_platform_assistant"] = service

    @app.get("/workspace")
    def workspace():
        return render_template_string(
            """<body><button id="work" onclick="this.textContent='Still working'">Work</button>
        {% include 'assistant_workspace.html' %}<script>{% include 'pipeline_assistant.js' %}
        {% include 'assistant_launcher.js' %}
        window.syncPlatformAssistant({owner:{{ owner|tojson }}, locationId:{{ location_id|tojson }},label:'Fixture dataset'});
        </script></body>""",
            owner=owner,
            location_id=value["location_id"],
        )

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=executable, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.request.post(
                base + "/api/auth/login", data={"username": "admin", "password": "strong-admin-password"}
            )
            page.goto(base + "/workspace")
            playwright.expect(page.locator("#ai-message")).to_be_enabled()
            page.locator("#work").focus()
            page.evaluate(
                "window.spaceEvents = 0; window.addEventListener('keydown', e => {if(e.code === 'Space') window.spaceEvents++;})"
            )
            page.keyboard.press("Control+Shift+Space")
            playwright.expect(page.locator("#ai-message")).to_be_focused()
            assert page.evaluate("window.spaceEvents") == 0  # Viewer playback must not receive the shortcut.
            page.locator("#ai-message").fill("Unsent draft")
            page.locator("#ai-message").dispatch_event("keydown", {"key": "Escape", "isComposing": True})
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            page.keyboard.press("Escape")
            playwright.expect(page.locator("#work")).to_be_focused()
            playwright.expect(page.locator("#assistant-dock")).to_be_hidden()
            page.keyboard.press("Meta+Shift+Space")
            playwright.expect(page.locator("#ai-message")).to_have_value("Unsent draft")
            page.locator("#ai-message").dispatch_event(
                "keydown", {"code": "Space", "key": " ", "ctrlKey": True, "shiftKey": True, "repeat": True}
            )
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            page.keyboard.press("Control+Shift+Space")
            playwright.expect(page.locator("#work")).to_be_focused()
            assert page.evaluate("window.spaceEvents") == 0
            page.evaluate(
                "const modal = document.createElement('dialog'); modal.id = 'test-modal'; modal.textContent = 'Platform dialog'; document.body.append(modal); modal.showModal();"
            )
            page.keyboard.press("Control+Shift+Space")
            playwright.expect(page.locator("#assistant-dock")).to_be_hidden()
            page.keyboard.press("Escape")
            page.locator("#test-modal").evaluate("node => node.remove()")
            page.get_by_role("button", name="Open AI assistant", exact=True).click()
            assert page.locator("#assistant-reopen").get_attribute("aria-keyshortcuts")
            page.locator("#work").click()
            page.keyboard.press("Escape")  # Escape outside the assistant belongs to the platform.
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            assert page.locator("#work").inner_text() == "Still working"
            assert page.locator("dialog").count() == 0 and page.locator("iframe").count() == 0
            page.locator("#ai-prefs-toggle").click()
            playwright.expect(page.locator("#ai-prefs-save")).to_be_enabled()
            page.locator("#ai-pref-language").select_option("en")
            page.locator("#ai-pref-language").focus()
            page.keyboard.press("Escape")
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            page.locator("#ai-pref-detail").select_option("concise")
            page.locator("#ai-pref-favorites").select_option("viewer.prepare")
            page.locator("#ai-prefs-save").click()
            playwright.expect(page.locator("#ai-prefs-status")).to_have_text("Preferences saved.")
            assert preferences(service, owner)["settings"]["language"] == "en"
            fake.responses = [{"choices": [{"message": {"content": "A remembered response"}}]}]
            page.locator("#ai-message").fill("Remember this conversation")
            page.locator("#ai-message").press("Enter")
            playwright.expect(page.locator("#ai-history")).to_contain_text("A remembered response")
            page.locator("#ai-new").click()
            page.locator("#ai-chats-toggle").click()
            page.locator("#ai-chats button").first.click()
            playwright.expect(page.locator("#ai-history")).to_contain_text("A remembered response")
            page.evaluate(
                "owner => window.syncPlatformAssistant({owner,locationId:'another-dataset',label:'Another dataset'})",
                owner,
            )
            playwright.expect(page.locator("#assistant-pinned")).to_be_visible()
            assert page.locator("#ai-history").inner_text().find("A remembered response") >= 0
            page.locator("#work").click()
            page.reload()
            playwright.expect(page.locator("#assistant-dock")).to_be_visible()
            playwright.expect(page.locator("#ai-history")).to_contain_text("A remembered response")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="Collapse AI assistant", exact=True).click()
            playwright.expect(page.locator("#assistant-dock")).to_be_hidden()
            page.get_by_role("button", name="Open AI assistant", exact=True).click()
            page.evaluate("window.syncPlatformAssistant({owner:null})")
            assert page.locator("#platform-assistant-workspace").count() == 0
            page.keyboard.press("Control+Shift+Space")
            assert page.locator("#platform-assistant-workspace").count() == 0
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
