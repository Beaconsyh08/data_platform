from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from lerobot.data_platform.precompute.labeling.qwen_dashscope import (
    DASHSCOPE_API_KEY_ENV_VARS,
    DASHSCOPE_MODELS,
    DEFAULT_DASHSCOPE_BASE_URL,
    DEFAULT_DASHSCOPE_MODEL,
    dashscope_content_text,
    dashscope_env_api_key,
    format_dashscope_error,
    image_data_url,
    normalize_base_url,
)
from lerobot.data_platform.precompute.labeling.qwen_remote import (
    DEFAULT_QWEN_ENDPOINT,
    DEFAULT_QWEN_MODEL,
    QWEN_MODELS,
    QWEN_TOKEN_ENV_VARS,
    _env_token as qwen_remote_env_token,
    normalize_endpoint as normalize_qwen_remote_endpoint,
)
from lerobot.data_platform.precompute.labeling.task_parser import normalize_object_name
from lerobot.data_platform.precompute.tagging.schema import DEFAULT_VLM_MODEL, normalize_background_label

DEFAULT_VLM_BACKEND = "qwen_dashscope"

try:
    from gradio_client import Client, handle_file

    _AVAILABLE = True
    _IMPORT_ERROR = None
except Exception as exc:  # optional dependency guard
    Client = None
    handle_file = None
    _AVAILABLE = False
    _IMPORT_ERROR = exc


def _remote_qwen_capabilities(default_model: str = DEFAULT_QWEN_MODEL) -> dict:
    return {
        "available": bool(_AVAILABLE),
        "dependencies_available": bool(_AVAILABLE),
        "backend": "qwen_remote",
        "gpu": False,
        "default_endpoint": DEFAULT_QWEN_ENDPOINT,
        "default_model": default_model,
        "models": QWEN_MODELS,
        "requires_token": True,
        "token_env_vars": list(QWEN_TOKEN_ENV_VARS),
        "token_configured": bool(qwen_remote_env_token()),
        "error": (
            None
            if _AVAILABLE
            else (
                "VLM tagging requires Remote Qwen support via `gradio-client`. "
                "Install via `pip install gradio-client` or `pip install 'lerobot[labeling-remote]'`."
            )
        ),
        "dependency_error": None if _AVAILABLE else str(_IMPORT_ERROR),
    }


def _dashscope_capabilities(default_model: str = DEFAULT_DASHSCOPE_MODEL) -> dict:
    return {
        "available": True,
        "dependencies_available": True,
        "backend": "qwen_dashscope",
        "gpu": False,
        "default_endpoint": DEFAULT_DASHSCOPE_BASE_URL,
        "default_model": default_model,
        "models": DASHSCOPE_MODELS,
        "requires_token": True,
        "token_env_vars": list(DASHSCOPE_API_KEY_ENV_VARS),
        "token_configured": bool(dashscope_env_api_key()),
        "error": None,
        "dependency_error": None,
    }


def get_capabilities(default_model: str = DEFAULT_VLM_MODEL, backend: str = DEFAULT_VLM_BACKEND) -> dict:
    backends = {
        "qwen_dashscope": _dashscope_capabilities(DEFAULT_DASHSCOPE_MODEL),
        "qwen_remote": _remote_qwen_capabilities(DEFAULT_QWEN_MODEL),
    }
    selected = backends.get(backend) or backends[DEFAULT_VLM_BACKEND]
    if default_model and default_model != DEFAULT_VLM_MODEL:
        selected = {**selected, "default_model": default_model}
    return {
        **selected,
        "available": bool(selected.get("available")),
        "default_backend": DEFAULT_VLM_BACKEND,
        "backends": backends,
    }


def ensure_available(backend: str = DEFAULT_VLM_BACKEND) -> None:
    capabilities = get_capabilities(backend=backend)
    selected = capabilities.get("backends", {}).get(backend, capabilities)
    if not selected["available"]:
        detail = capabilities.get("dependency_error")
        raise RuntimeError(f"{selected['error']} (import error: {detail})")


def _extract_json_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return value[0]
        for item in value:
            try:
                return _extract_json_payload(item)
            except ValueError:
                continue
        raise ValueError("Remote Qwen tagging response did not contain JSON.")
    if isinstance(value, tuple):
        for item in value:
            try:
                return _extract_json_payload(item)
            except ValueError:
                continue
        raise ValueError("Remote Qwen tagging response did not contain JSON.")
    if not isinstance(value, str):
        raise ValueError("Remote Qwen tagging response did not contain JSON text.")

    text = value.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError("Remote Qwen tagging response did not contain a JSON object.")
        return json.loads(match.group(0))


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


_COUNT_WORDS = {
    "zero": 0,
    "none": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _normalize_background(value: Any, options: list[str]) -> str | None:
    return normalize_background_label(value)


def _normalize_color(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("color") or value.get("name") or value.get("label")
    if isinstance(value, list):
        value = " ".join(str(item) for item in value if item is not None)
    text = str(value).strip().lower()
    if not text or text in {"null", "none", "unknown", "unclear", "n/a"}:
        return None
    text = re.sub(r"\s+", " ", text)
    return text[:64]


def _normalize_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("count") or value.get("value") or value.get("objects")
    if isinstance(value, (int, float)):
        count = int(value)
        return count if count >= 0 else None
    text = str(value).strip().lower()
    if text in _COUNT_WORDS:
        return _COUNT_WORDS[text]
    for word, count in _COUNT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            return count
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def _normalize_prompt_action_match(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("prompt_action_match") or value.get("match") or value.get("status") or value.get("result")
    text = str(value).strip().lower()
    if not text or text in {"null", "none", "n/a"}:
        return None
    key = _key(text)
    if key in {"match", "matched", "correct", "yes", "true", "consistent", "same"}:
        return "match"
    if key in {"mismatch", "mismatched", "incorrect", "wrong", "no", "false", "inconsistent", "different"}:
        return "mismatch"
    if "mismatch" in key or "incorrect" in key or "different" in key or "wrong" in key:
        return "mismatch"
    if "match" in key or "correct" in key or "same" in key:
        return "match"
    return "unclear"


def _normalize_allowed_objects(allowed_objects: list[str] | None) -> list[str]:
    out = []
    seen = set()
    for value in allowed_objects or []:
        normalized = normalize_object_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _normalize_prompt_action_payload(payload: Any, allowed_objects: list[str] | None = None) -> dict:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("Prompt-action match JSON must be an object.")
    raw = _payload_lookup(payload, "prompt_action_match", "match", "status", "result")
    value = _normalize_prompt_action_match(raw)
    observed_object = normalize_object_name(
        _payload_lookup(payload, "observed_object", "manipulated_object", "grasped_object", "object")
    )
    allowed = _normalize_allowed_objects(allowed_objects)
    if allowed and observed_object and observed_object not in set(allowed):
        observed_object = None
        if value == "mismatch":
            value = "unclear"
    return {
        "value": value,
        "observed_object": observed_object,
        "reason": _payload_lookup(payload, "reason", "explanation", "evidence"),
    }


def _payload_lookup(payload: dict, *names: str) -> Any:
    normalized = {_key(key): value for key, value in payload.items()}
    for name in names:
        key = _key(name)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_values(payload: Any, tag_defs: list[dict]) -> dict:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("Remote Qwen tagging JSON must be an object.")

    values = {}
    for tag in tag_defs:
        name = tag["name"]
        if name == "background":
            raw = _payload_lookup(payload, "background", "background_type", "furniture", "surface")
            values[name] = _normalize_background(raw, tag.get("options") or [])
        elif name == "background_color":
            raw = _payload_lookup(payload, "background_color", "furniture_color", "surface_color", "color")
            values[name] = _normalize_color(raw)
        elif name == "object_count":
            raw = _payload_lookup(payload, "object_count", "objects_count", "num_objects", "count")
            values[name] = _normalize_count(raw)
        elif name == "prompt_action_match":
            raw = _payload_lookup(payload, "prompt_action_match", "match", "status", "result")
            values[name] = _normalize_prompt_action_match(raw)
        else:
            values[name] = _payload_lookup(payload, name)
    return values


def _build_prompt_action_match_prompt(
    task: str,
    *,
    image_count: int = 1,
    allowed_objects: list[str] | None = None,
) -> str:
    image_note = (
        "You are given two images: the first frame and the final frame of one robot episode."
        if image_count >= 2
        else "You are given the final frame of one robot episode."
    )
    allowed = _normalize_allowed_objects(allowed_objects)
    allowed_note = ""
    object_schema = "<object or null>"
    if allowed:
        allowed_text = ", ".join(allowed)
        object_schema = "|".join(allowed) + "|null"
        allowed_note = (
            f"The only valid task object types in this dataset are: {allowed_text}. "
            'The "observed_object" field must be exactly one of those object strings, or null if unclear. '
            "Do not invent new object types and do not output labels outside this list. "
        )
    return (
        f"{image_note} The task prompt is: {task!r}. "
        "Determine whether the object that the robot finally manipulates or grasps matches the object requested by the prompt. "
        "For example, if the prompt asks for a yellow duck but the final manipulated object is a dog, return mismatch. "
        "If the final manipulated object matches the requested object, return match. "
        "If the image is ambiguous or the robot is not clearly manipulating an object, return unclear. "
        + allowed_note
        + "Output the results in the following JSON format:\n"
        "```json\n"
        f'{{"prompt_action_match":"match|mismatch|unclear","observed_object":"{object_schema}","reason":"<short evidence>"}}'
        "\n```\n"
        "Return ONLY one JSON object, no markdown and no extra text."
    )


def _build_prompt(tag_defs: list[dict]) -> str:
    names = {tag["name"] for tag in tag_defs}
    fields = []
    schema_items = []
    if "background" in names:
        fields.append(
            "background: the dominant background furniture/supporting surface containing the task objects; "
            "choose exactly one of round_table, square_table, tv_cabinet, sofa, or null when unsure."
        )
        schema_items.append('"background":"round_table|square_table|tv_cabinet|sofa|null"')
    if "background_color" in names:
        fields.append("background_color: the dominant color of that background furniture/surface, or null when unsure.")
        schema_items.append('"background_color":"<color>|null"')
    if "object_count" in names:
        fields.append(
            "object_count: number of visible task-relevant toy/manipulation objects; "
            "exclude robot arms, hands, people, and background furniture."
        )
        schema_items.append('"object_count":0')
    if "prompt_action_match" in names:
        fields.append(
            "prompt_action_match: whether the final manipulated object matches the task prompt; "
            "choose match, mismatch, or unclear."
        )
        schema_items.append('"prompt_action_match":"match|mismatch|unclear"')

    if not fields:
        for tag in tag_defs:
            fields.append(f"{tag['name']}: {tag.get('prompt') or 'infer this tag from the image.'}")
            schema_items.append(f'"{tag["name"]}":null')

    schema = "{" + ",".join(schema_items) + "}"
    return (
        "Analyze the first frame image for dataset tagging. Do not guess; use null for fields that are not clearly visible. "
        "Fields: "
        + " ".join(fields)
        + "\nOutput the results in the following JSON format:\n"
        "```json\n"
        + schema
        + "\n```\n"
        "Return ONLY one JSON object, no markdown and no extra text."
    )


class VLMTagger:
    def __init__(
        self,
        client,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        model_id: str = DEFAULT_VLM_MODEL,
        min_pixels: int = 1024,
        max_pixels: int = 9800,
    ):
        self.client = client
        self.endpoint = endpoint
        self.model_id = model_id
        self.min_pixels = int(min_pixels)
        self.max_pixels = int(max_pixels)

    @classmethod
    def load(
        cls,
        model_id: str = DEFAULT_VLM_MODEL,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        backend: str = "qwen_remote",
        token: str | None = None,
        min_pixels: int = 1024,
        max_pixels: int = 9800,
    ):
        if backend == "qwen_dashscope":
            return DashScopeVLMTagger.load(base_url=endpoint, model_id=model_id, api_key=token)
        ensure_available(backend)
        endpoint = normalize_qwen_remote_endpoint(endpoint)
        token = (token or qwen_remote_env_token() or "").strip()
        headers = {"X-Studio-Token": token} if token else None
        try:
            client = Client(endpoint, headers=headers) if headers else Client(endpoint)
        except TypeError as exc:
            if headers and "headers" in str(exc):
                raise RuntimeError(
                    "Your `gradio-client` version does not support custom headers. "
                    "Upgrade it with `pip install -U gradio-client` to pass the ModelScope SDK token."
                ) from exc
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not connect to Remote Qwen endpoint {endpoint}: {exc}") from exc
        return cls(client, endpoint=endpoint, model_id=model_id, min_pixels=min_pixels, max_pixels=max_pixels)

    def _predict_payload(self, image_pil, prompt: str) -> Any:
        if image_pil is None:
            raise ValueError("VLM tagging requires an image.")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                image_pil.convert("RGB").save(tmp, format="JPEG", quality=92)

            last_exc = None
            for attempt in range(3):
                try:
                    result = self.client.predict(
                        image=handle_file(str(tmp_path)),
                        user_prompt=prompt,
                        model=self.model_id,
                        min_pixels_str=str(self.min_pixels),
                        max_pixels_str=str(self.max_pixels),
                        api_name="/run_detection_streaming",
                    )
                    return _extract_json_payload(result)
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(2**attempt)
            raise RuntimeError(f"Remote Qwen tagging failed after 3 attempts: {last_exc}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def predict_many(self, image, tag_defs: list[dict]) -> dict:
        if not tag_defs:
            return {}
        prompt = _build_prompt(tag_defs)
        payload = self._predict_payload(image, prompt)
        return _normalize_values(payload, tag_defs)

    def predict(self, image, tag_def: dict):
        return self.predict_many(image, [tag_def]).get(tag_def["name"])

    def predict_prompt_action_match(
        self,
        first_image,
        final_image,
        task: str,
        allowed_objects: list[str] | None = None,
    ) -> dict:
        # Gradio remote accepts one image; use the final frame because that contains the outcome.
        payload = self._predict_payload(
            final_image or first_image,
            _build_prompt_action_match_prompt(task, image_count=1, allowed_objects=allowed_objects),
        )
        return _normalize_prompt_action_payload(payload, allowed_objects=allowed_objects)

    def close(self) -> None:
        for method_name in ("close", "reset_session"):
            method = getattr(self.client, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:
                pass


class DashScopeVLMTagger:
    def __init__(self, *, base_url: str, api_key: str, model_id: str, timeout_s: int = 120):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.model_id = model_id
        self.timeout_s = int(timeout_s)

    @classmethod
    def load(
        cls,
        base_url: str = DEFAULT_DASHSCOPE_BASE_URL,
        model_id: str = DEFAULT_DASHSCOPE_MODEL,
        api_key: str | None = None,
        timeout_s: int = 120,
    ):
        api_key = (api_key or dashscope_env_api_key() or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenAI-compatible VLM tagging requires an API key value. "
                "Set DASHSCOPE_API_KEY/QWEN_DASHSCOPE_API_KEY, paste it in the UI token field, "
                "or use EMPTY for a local vLLM/SGLang server that ignores auth."
            )
        return cls(base_url=base_url, api_key=api_key, model_id=model_id, timeout_s=timeout_s)

    def _post_chat_completion(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
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

    def _predict_payload_images(self, image_pils: list, prompt: str) -> Any:
        image_pils = [image for image in image_pils if image is not None]
        if not image_pils:
            raise ValueError("VLM tagging requires an image.")
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": image_data_url(image)}} for image in image_pils)
        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": 0,
            "max_tokens": 512,
        }
        last_exc = None
        for attempt in range(3):
            try:
                response = self._post_chat_completion(payload)
                content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
                return _extract_json_payload(dashscope_content_text(content))
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"OpenAI-compatible VLM tagging failed after 3 attempts: {last_exc}") from last_exc

    def _predict_payload(self, image_pil, prompt: str) -> Any:
        if image_pil is None:
            raise ValueError("VLM tagging requires an image.")
        return self._predict_payload_images([image_pil], prompt)

    def predict_many(self, image, tag_defs: list[dict]) -> dict:
        if not tag_defs:
            return {}
        payload = self._predict_payload(image, _build_prompt(tag_defs))
        return _normalize_values(payload, tag_defs)

    def predict(self, image, tag_def: dict):
        return self.predict_many(image, [tag_def]).get(tag_def["name"])

    def predict_prompt_action_match(
        self,
        first_image,
        final_image,
        task: str,
        allowed_objects: list[str] | None = None,
    ) -> dict:
        payload = self._predict_payload_images(
            [first_image, final_image],
            _build_prompt_action_match_prompt(task, image_count=2, allowed_objects=allowed_objects),
        )
        return _normalize_prompt_action_payload(payload, allowed_objects=allowed_objects)

    def close(self) -> None:
        pass
