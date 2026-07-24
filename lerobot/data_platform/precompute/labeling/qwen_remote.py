from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

PUBLIC_QWEN_ENDPOINT = "https://qwen-object-detection-with-qwen.ms.show/"
DEFAULT_QWEN_ENDPOINT = "https://studio-qwen-object-detection-with-qwen.api-inference.modelscope.net/"
DEFAULT_QWEN_MODEL = "qwen3.6-35b-a3b"
QWEN_MODELS = ["qwen3.6-35b-a3b", "qwen3.5-35b-a3b"]
QWEN_TOKEN_ENV_VARS = ("MODELSCOPE_SDK_TOKEN", "QWEN_REMOTE_TOKEN", "MODELSCOPE_TOKEN")

try:
    from gradio_client import Client, handle_file

    _AVAILABLE = True
    _IMPORT_ERROR = None
except Exception as e:
    Client = None
    handle_file = None
    _AVAILABLE = False
    _IMPORT_ERROR = e


def is_available() -> bool:
    return bool(_AVAILABLE)


def _env_token() -> str | None:
    for name in QWEN_TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def normalize_endpoint(endpoint: str | None) -> str:
    value = (endpoint or DEFAULT_QWEN_ENDPOINT).strip() or DEFAULT_QWEN_ENDPOINT
    if value.rstrip("/") == PUBLIC_QWEN_ENDPOINT.rstrip("/"):
        return DEFAULT_QWEN_ENDPOINT
    return value if value.endswith("/") else f"{value}/"


def get_capabilities() -> dict:
    return {
        "available": is_available(),
        "default_endpoint": DEFAULT_QWEN_ENDPOINT,
        "public_endpoint": PUBLIC_QWEN_ENDPOINT,
        "default_model": DEFAULT_QWEN_MODEL,
        "models": QWEN_MODELS,
        "requires_token": True,
        "token_env_vars": list(QWEN_TOKEN_ENV_VARS),
        "token_configured": bool(_env_token()),
        "error": None if _AVAILABLE else str(_IMPORT_ERROR),
    }


def _extract_json_payload(value: Any) -> Any:
    if isinstance(value, dict):
        # Gradio returns an output-image file dict before the raw model text.
        # Skip those transport dictionaries and keep searching the tuple/list.
        if "bbox_2d" in value or "bbox" in value:
            return [value]
        raise ValueError("Remote Qwen response did not contain detection JSON.")
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value) and any(
            "bbox_2d" in item or "bbox" in item for item in value
        ):
            return value
        for item in value:
            try:
                return _extract_json_payload(item)
            except ValueError:
                continue
        raise ValueError("Remote Qwen response did not contain JSON.")
    if isinstance(value, tuple):
        for item in value:
            try:
                return _extract_json_payload(item)
            except ValueError:
                continue
        raise ValueError("Remote Qwen response did not contain JSON.")
    if not isinstance(value, str):
        raise ValueError("Remote Qwen response did not contain JSON text.")

    text = value.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            raise ValueError("Remote Qwen response did not contain a JSON array.")
        return json.loads(match.group(0))


def build_qwen_detection_prompt(text_prompt: str) -> str:
    return (
        f'Detect "{text_prompt}" in the image. Do not guess or invent boxes. '
        "Return only visible instances of the requested object; if none are visible, return []. "
        "Output the results in the following JSON format:\n"
        "```json\n"
        '[{"bbox_2d":[x1,y1,x2,y2],"label":"<name>","confidence":0.0}]\n'
        "```\n"
        "Use bbox_2d coordinates. Set confidence from 0 to 1 for visual certainty. "
        "Output ONLY JSON."
    )


def _format_remote_error(exc: Exception) -> str:
    message = str(exc)
    if "SDK Token" in message or "X-Studio-Token" in message or "10010101007" in message:
        return (
            "Remote Qwen requires a ModelScope SDK Token. Set MODELSCOPE_SDK_TOKEN, "
            "QWEN_REMOTE_TOKEN, or pass qwen_token from the UI/CLI. Original error: "
            f"{message}"
        )
    if "403" in message or "Forbidden" in message:
        return (
            "Remote Qwen endpoint returned 403 Forbidden. The public Gradio endpoint refused this "
            "client/network request before model inference. Use the ModelScope API endpoint with "
            "an SDK token, use Local GroundingDINO, or replace the endpoint with a reachable "
            "self-hosted Gradio URL. Original error: "
            f"{message}"
        )
    return message


def _parse_confidence(item: dict) -> tuple[float, str]:
    raw = item.get("confidence", item.get("score"))
    if raw is None:
        return 1.0, "vlm_implicit"
    if isinstance(raw, str):
        match = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not match:
            return 1.0, "vlm_implicit"
        raw = match.group(0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0, "vlm_implicit"
    if 1.0 < value <= 100.0:
        value = value / 100.0
    return max(0.0, min(1.0, value)), "vlm_self_reported"


def parse_qwen_detections(value: Any, image_size: tuple[int, int]) -> list[dict]:
    payload = _extract_json_payload(value)
    if not isinstance(payload, list):
        raise ValueError("Remote Qwen detection JSON must be a list.")

    width, height = image_size
    detections = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox_2d") or item.get("bbox")
        if not isinstance(bbox, list | tuple) or len(bbox) != 4:
            continue
        x1_raw, y1_raw, x2_raw, y2_raw = [float(v) for v in bbox]
        coord_space = "pixel"
        if all(0.0 <= value <= 1.0 for value in [x1_raw, y1_raw, x2_raw, y2_raw]):
            x1_raw *= width
            x2_raw *= width
            y1_raw *= height
            y2_raw *= height
            coord_space = "normalized_0_1"
        elif max(x1_raw, x2_raw) > width or max(y1_raw, y2_raw) > height:
            if max(x1_raw, y1_raw, x2_raw, y2_raw) <= 1000:
                x1_raw = x1_raw * width / 1000
                x2_raw = x2_raw * width / 1000
                y1_raw = y1_raw * height / 1000
                y2_raw = y2_raw * height / 1000
                coord_space = "qwen_0_1000"

        x1, y1, x2, y2 = [int(round(v)) for v in [x1_raw, y1_raw, x2_raw, y2_raw]]
        left = max(0, min(x1, width - 1))
        top = max(0, min(y1, height - 1))
        right = max(0, min(x2, width))
        bottom = max(0, min(y2, height))
        if right <= left or bottom <= top:
            continue
        confidence, confidence_source = _parse_confidence(item)
        detections.append(
            {
                "bbox": {"top": top, "left": left, "bottom": bottom, "right": right},
                "confidence": round(confidence, 4),
                "confidence_source": confidence_source,
                "label": str(item.get("label") or item.get("name") or "object"),
                "raw_bbox_2d": list(bbox),
                "bbox_coordinate_space": coord_space,
            }
        )
    return detections


class QwenRemoteDetector:
    def __init__(self, client, endpoint: str, model: str, min_pixels: int, max_pixels: int):
        self.client = client
        self.endpoint = normalize_endpoint(endpoint)
        self.model = model
        self.model_id = model
        self.device = "remote"
        self.min_pixels = int(min_pixels)
        self.max_pixels = int(max_pixels)

    @classmethod
    def load(
        cls,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        model: str = DEFAULT_QWEN_MODEL,
        min_pixels: int = 1024,
        max_pixels: int = 9800,
        token: str | None = None,
    ):
        if not _AVAILABLE:
            raise RuntimeError(
                "Remote Qwen object labeling requires `gradio-client`. "
                "Install via `pip install gradio-client`. "
                f"(import error: {_IMPORT_ERROR})"
            )
        endpoint = normalize_endpoint(endpoint)
        token = (token or _env_token() or "").strip()
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
            raise RuntimeError(
                f"Could not connect to Remote Qwen endpoint {endpoint}: {_format_remote_error(exc)}"
            ) from exc
        return cls(client, endpoint, model, min_pixels, max_pixels)

    def detect_for_prompt(self, image_pil, text_prompt: str, **_) -> list[dict]:
        prompt = build_qwen_detection_prompt(text_prompt)
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
                        model=self.model,
                        min_pixels_str=str(self.min_pixels),
                        max_pixels_str=str(self.max_pixels),
                        api_name="/run_detection_streaming",
                    )
                    return parse_qwen_detections(result, image_pil.size)
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(2**attempt)
            raise RuntimeError(
                f"Remote Qwen detection failed after 3 attempts: {_format_remote_error(last_exc)}"
            )
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        for method_name in ("close", "reset_session"):
            method = getattr(self.client, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:
                pass
