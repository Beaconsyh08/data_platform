"""Shared Qwen credentials and chat transport for server-side model features."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DASHSCOPE_BASE_URL = (
    os.environ.get("DASHSCOPE_BASE_URL", "").strip() or "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_DASHSCOPE_MODEL = "qwen3.6-plus"
DASHSCOPE_MODELS = ["qwen3.6-plus", "qwen3.7-plus", "qwen3.6-flash", "qwen3-vl-plus", "qwen3-vl-flash"]
DASHSCOPE_API_KEY_ENV_VARS = ("DASHSCOPE_API_KEY", "QWEN_DASHSCOPE_API_KEY")


def dashscope_env_api_key() -> str | None:
    return next(
        (value for name in DASHSCOPE_API_KEY_ENV_VARS if (value := os.getenv(name, "").strip())), None
    )


def normalize_base_url(base_url: str | None) -> str:
    value = ((base_url or "").strip() or DEFAULT_DASHSCOPE_BASE_URL).rstrip("/")
    return value.removesuffix("/chat/completions")


def resolve_api_key(base_url: str | None, api_key: str | None = None) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    if normalize_base_url(base_url) != normalize_base_url(DEFAULT_DASHSCOPE_BASE_URL):
        raise ValueError(
            "A custom endpoint requires an explicit API key (or EMPTY for local unauthenticated servers). "
            "To use the shared server key, configure DASHSCOPE_BASE_URL on the server."
        )
    key = dashscope_env_api_key()
    if not key:
        raise ValueError("Configure DASHSCOPE_API_KEY on Server A, then restart data-platform-web.")
    return key


def dashscope_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content
        )
    return str(content)


def format_dashscope_error(exc: Exception) -> str:
    # Provider bodies can echo credentials or request contents. Keep errors actionable without logging them.
    if isinstance(exc, urllib.error.HTTPError):
        hints = {401: "check the API key and region", 403: "check model access", 429: "rate limit or quota"}
        return (
            f"Qwen API HTTP {exc.code}: {hints.get(exc.code, 'check the model and endpoint configuration')}"
        )
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "Qwen API connection failed or timed out; check server connectivity and retry."
    return "Qwen API returned an invalid response; retry or check the model configuration."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a server credential to a redirected destination.
        return None


@dataclass
class QwenClient:
    base_url: str = DEFAULT_DASHSCOPE_BASE_URL
    api_key: str = field(default="", repr=False)
    timeout_s: int = 120

    def __post_init__(self):
        self.base_url = normalize_base_url(self.base_url)
        self.api_key = resolve_api_key(self.base_url, self.api_key)

    def post_chat_completion(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout_s) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object")
            return result
        except Exception as exc:
            raise RuntimeError(format_dashscope_error(exc)) from None
