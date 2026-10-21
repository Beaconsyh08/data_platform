"""Use the same HTTPS session, CSRF and assistant API as the browser."""

from __future__ import annotations

import time
import uuid
from urllib.parse import urlencode, urlsplit

import requests


class ClientError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class PlatformClient:
    def __init__(self, url: str, *, ca_bundle: str | None = None, session=None):
        origin = urlsplit(url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in ("", "/")
        ):
            raise ClientError("Use an HTTPS service origin, for example https://platform.example.com")
        self.origin = url.rstrip("/")
        self.session = session or requests.Session()
        self.session.verify = ca_bundle or True
        self.csrf = "anonymous"
        self.user = None
        self.location = None
        self.conversation = None
        self.pending = None
        self.last_action = None
        self.last_run = None

    def request(self, method, path, body=None):
        if not path.startswith("/api/") or path.startswith("//"):
            raise ClientError("Unsupported service request")
        try:
            response = self.session.request(
                method,
                self.origin + path,
                json=body,
                timeout=(10, 90),
                allow_redirects=False,
                headers={"Origin": self.origin, "X-Data-Platform-CSRF": self.csrf},
            )
        except (requests.RequestException, OSError):
            raise ClientError(
                "Connection failed. Check the server address, TLS certificate and network."
            ) from None
        if response.status_code == 401:
            raise ClientError("Session expired. Log in again.", status=401)
        if response.status_code >= 300:
            # Do not render arbitrary proxy/driver error bodies in the terminal.
            raise ClientError(
                {
                    403: "Permission denied.",
                    409: "State changed; refresh the conversation or action.",
                    429: "A request is still running. Try again shortly.",
                    503: "Service unavailable or under maintenance.",
                }.get(response.status_code, "The service rejected this request."),
                status=response.status_code,
            )
        try:
            result = response.json()
        except ValueError:
            raise ClientError("Invalid service response") from None
        if not isinstance(result, dict):
            raise ClientError("Invalid service response")
        return result

    def login(self, username, password):
        self.request("POST", "/api/auth/login", {"username": username, "password": password})
        state = self.request("GET", "/api/auth/status")
        if not state.get("authenticated"):
            raise ClientError("Login failed")
        self.csrf, self.user = state["csrf_token"], state["user"]
        return self.user

    def datasets(self):
        return self.request("GET", "/api/control/locations?summary=1")["locations"]

    def conversations(self, cursor=None):
        query = {"limit": 20}
        if cursor:
            query["cursor"] = cursor
        return self.request("GET", "/api/ai-assistant/conversations?" + urlencode(query))

    def preferences(self):
        return self.request("GET", "/api/ai-assistant/preferences")

    def save_preferences(self, revision, settings):
        return self.request(
            "PUT",
            "/api/ai-assistant/preferences",
            {
                "expected_revision": revision,
                "settings": settings,
            },
        )

    def clear_preferences(self, revision):
        return self.request("DELETE", "/api/ai-assistant/preferences", {"expected_revision": revision})

    def select(self, location):
        self.location = location
        self.new_chat()

    def new_chat(self):
        self.conversation = {"conversation_id": str(uuid.uuid4()), "revision": 0, "history": []}
        self.pending = self.last_action = self.last_run = None

    def chat(self, message):
        if not self.location:
            raise ClientError("Choose a dataset with /datasets first.")
        if self.pending:
            raise ClientError("A previous response is pending. Use /resume before sending another message.")
        body = {
            "request_id": str(uuid.uuid4()),
            "conversation_id": self.conversation["conversation_id"],
            "expected_revision": self.conversation["revision"],
            "location_id": self.location["location_id"],
            "message": message,
            "context": self.conversation.get("context")
            or {"type": "dataset", "target_id": self.location["location_id"]},
        }
        self.pending = body
        return self.resume()

    def resume(self):
        if not self.pending:
            raise ClientError("No response is pending.")
        # Identical request identity is retained through timeouts/retries.
        try:
            result = (
                self.request("POST", "/api/ai-assistant/turns", self.pending)
                if "message" in self.pending
                else self.request("GET", "/api/ai-assistant/turns/" + self.pending["request_id"])
            )
        except ClientError as error:
            # Rejected admissions cannot be recovered by replaying the same invalid request.
            # Network/server failures remain pending because acceptance is uncertain.
            if error.status in {400, 401, 403, 404, 409, 411, 413, 429}:
                self.pending = None
            raise
        deadline = time.monotonic() + 130
        while result["status"] == "running" and time.monotonic() < deadline:
            time.sleep(1)
            result = self.request("GET", "/api/ai-assistant/turns/" + self.pending["request_id"])
        if result["status"] == "running":
            raise ClientError("Response is still pending. Use /resume later.")
        self.conversation = self.request(
            "GET", "/api/ai-assistant/conversations/" + result["conversation_id"]
        )
        self.last_action = self.conversation.get("action")
        self.pending = None
        return result

    def restore(self, conversation_id):
        identity = str(uuid.UUID(conversation_id))
        value = self.request("GET", "/api/ai-assistant/conversations/" + identity)
        location = next(
            (item for item in self.datasets() if item["location_id"] == value["location_id"]), None
        )
        if location is None:
            raise ClientError("The conversation's dataset is unavailable.")
        self.location, self.conversation = location, value
        self.last_action = value.get("action")
        self.pending = None
        if value.get("pending_request"):
            self.pending = {"request_id": value["pending_request"]}
        return value

    def confirm(self, *, rerun=False):
        if not self.last_action:
            raise ClientError("Ask the assistant to prepare an operation first.")
        action = self.last_action
        self.last_run = self.request(
            "POST",
            "/api/ai-assistant/actions/" + action["action_id"] + "/confirm",
            {"revision": action["revision"], **({"rerun": True} if rerun else {})},
        )
        self.last_action["execution"] = self.last_run
        return self.last_run

    def status(self):
        if not self.last_action:
            raise ClientError("Ask the assistant to prepare an operation first.")
        value = self.request("GET", "/api/ai-assistant/actions/" + self.last_action["action_id"])
        self.conversation["operation"] = value
        return value

    def links(self):
        """Return fresh server-generated, same-origin links; never launch arbitrary model URLs."""
        value = self.status()
        result = []
        for link in value.get("links", []):
            path = link.get("url", "")
            parts = urlsplit(path)
            if (
                parts.scheme
                or parts.netloc
                or not path.startswith("/")
                or path.startswith("//")
                or "\\" in path
            ):
                continue
            result.append({"title": link["title"], "url": self.origin + path})
        return result

    def logout(self):
        try:
            self.request("POST", "/api/auth/logout", {})
        finally:
            self.session.cookies.clear()
            self.session.close()
            self.user = None
