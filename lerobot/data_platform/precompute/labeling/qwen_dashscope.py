from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any

from lerobot.data_platform.precompute.labeling.qwen_remote import build_qwen_detection_prompt, parse_qwen_detections

DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_MODEL = "qwen3.6-plus"
DASHSCOPE_MODELS = [
    "qwen3.6-plus",
    "qwen3.7-plus",
    "qwen3.6-flash",
    "qwen3-vl-plus",
    "qwen3-vl-flash",
]
DASHSCOPE_API_KEY_ENV_VARS = ("DASHSCOPE_API_KEY", "QWEN_DASHSCOPE_API_KEY")


def dashscope_env_api_key() -> str | None:
    for name in DASHSCOPE_API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def normalize_base_url(base_url: str | None) -> str:
    value = (base_url or DEFAULT_DASHSCOPE_BASE_URL).strip() or DEFAULT_DASHSCOPE_BASE_URL
    value = value.rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")]
    return value


def get_capabilities() -> dict:
    return {
        "available": True,
        "default_endpoint": DEFAULT_DASHSCOPE_BASE_URL,
        "default_model": DEFAULT_DASHSCOPE_MODEL,
        "models": DASHSCOPE_MODELS,
        "requires_token": True,
        "token_env_vars": list(DASHSCOPE_API_KEY_ENV_VARS),
        "token_configured": bool(dashscope_env_api_key()),
        "error": None,
    }


def format_dashscope_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"OpenAI-compatible VLM HTTP {exc.code}: {body or exc.reason}"
    return str(exc)


def dashscope_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
    return str(content)


def image_data_url(image_pil) -> str:
    buffer = BytesIO()
    image_pil.convert("RGB").save(buffer, format="JPEG", quality=92)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class QwenDashScopeDetector:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int = 120,
    ):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.model_id = model
        self.device = "dashscope"
        self.timeout_s = int(timeout_s)

    @classmethod
    def load(
        cls,
        base_url: str = DEFAULT_DASHSCOPE_BASE_URL,
        model: str = DEFAULT_DASHSCOPE_MODEL,
        api_key: str | None = None,
        timeout_s: int = 120,
    ):
        api_key = (api_key or dashscope_env_api_key() or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenAI-compatible VLM object labeling requires an API key value. "
                "Set DASHSCOPE_API_KEY/QWEN_DASHSCOPE_API_KEY, paste it in the UI token field, "
                "or use EMPTY for a local vLLM/SGLang server that ignores auth."
            )
        return cls(base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s)

    def _post_chat_completion(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(format_dashscope_error(exc)) from exc

    def detect_for_prompt(self, image_pil, text_prompt: str, **_) -> list[dict]:
        prompt = build_qwen_detection_prompt(text_prompt)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url(image_pil)}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 1024,
        }

        last_exc = None
        for attempt in range(3):
            try:
                response = self._post_chat_completion(payload)
                content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
                return parse_qwen_detections(dashscope_content_text(content), image_pil.size)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"OpenAI-compatible VLM detection failed after 3 attempts: {last_exc}") from last_exc

    def close(self) -> None:
        pass
