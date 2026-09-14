from __future__ import annotations

import base64
import time
from io import BytesIO

from lerobot.data_platform.precompute.labeling.qwen_remote import (
    build_qwen_detection_prompt,
    parse_qwen_detections,
)
from lerobot.data_platform.qwen import (  # Re-export the existing labeling API for callers.
    DASHSCOPE_API_KEY_ENV_VARS as DASHSCOPE_API_KEY_ENV_VARS,
)
from lerobot.data_platform.qwen import (
    DASHSCOPE_MODELS as DASHSCOPE_MODELS,
)
from lerobot.data_platform.qwen import (
    DEFAULT_DASHSCOPE_BASE_URL as DEFAULT_DASHSCOPE_BASE_URL,
)
from lerobot.data_platform.qwen import (
    DEFAULT_DASHSCOPE_MODEL as DEFAULT_DASHSCOPE_MODEL,
)
from lerobot.data_platform.qwen import (
    QwenClient,
    resolve_api_key,
)
from lerobot.data_platform.qwen import (
    dashscope_content_text as dashscope_content_text,
)
from lerobot.data_platform.qwen import (
    dashscope_env_api_key as dashscope_env_api_key,
)
from lerobot.data_platform.qwen import (
    format_dashscope_error as format_dashscope_error,
)
from lerobot.data_platform.qwen import (
    normalize_base_url as normalize_base_url,
)


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
        api_key = resolve_api_key(base_url, api_key)
        return cls(base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s)

    def _post_chat_completion(self, payload: dict) -> dict:
        return QwenClient(self.base_url, self.api_key, self.timeout_s).post_chat_completion(payload)

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
        raise RuntimeError(
            f"OpenAI-compatible VLM detection failed after 3 attempts: {last_exc}"
        ) from last_exc

    def close(self) -> None:
        pass
