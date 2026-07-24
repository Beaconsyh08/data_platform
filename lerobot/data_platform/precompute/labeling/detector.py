from __future__ import annotations

from typing import Protocol

DEFAULT_MODEL_ID = "IDEA-Research/grounding-dino-base"
DEFAULT_BOX_THRESHOLD = 0.25
DEFAULT_TEXT_THRESHOLD = 0.25
DEFAULT_BACKEND = "grounding_dino"
MAX_AREA_FRAC = 0.10

try:
    import torch
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    _AVAILABLE = True
    _IMPORT_ERROR = None
except Exception as e:  # optional dependency guard
    torch = None
    AutoProcessor = None
    AutoModelForZeroShotObjectDetection = None
    _AVAILABLE = False
    _IMPORT_ERROR = e


def ensure_available() -> None:
    if not _AVAILABLE:
        raise RuntimeError(
            "Object labeling requires `transformers` and `torch`. "
            "Install via `pip install transformers torch torchvision`. "
            f"(import error: {_IMPORT_ERROR})"
        )


def cuda_devices() -> list[str]:
    if not _AVAILABLE:
        return []
    try:
        if not torch.cuda.is_available():
            return []
        return [f"cuda:{idx}" for idx in range(torch.cuda.device_count())]
    except Exception:
        return []


def get_capabilities(default_model: str = DEFAULT_MODEL_ID) -> dict:
    devices = cuda_devices()
    gpu = bool(devices)
    grounding_dino = {
        "available": bool(_AVAILABLE),
        "gpu": gpu,
        "cuda_device_count": len(devices),
        "devices": devices,
        "default_model": default_model,
        "error": None if _AVAILABLE else str(_IMPORT_ERROR),
    }
    try:
        from lerobot.data_platform.precompute.labeling.qwen_remote import (
            get_capabilities as get_qwen_capabilities,
        )

        qwen_remote = get_qwen_capabilities()
    except Exception as exc:
        qwen_remote = {"available": False, "error": str(exc)}
    try:
        from lerobot.data_platform.precompute.labeling.qwen_dashscope import (
            get_capabilities as get_qwen_dashscope_capabilities,
        )

        qwen_dashscope = get_qwen_dashscope_capabilities()
    except Exception as exc:
        qwen_dashscope = {"available": False, "error": str(exc)}
    return {
        **grounding_dino,
        "backends": {
            "grounding_dino": grounding_dino,
            "qwen_remote": qwen_remote,
            "qwen_dashscope": qwen_dashscope,
        },
        "default_backend": DEFAULT_BACKEND,
    }


class DetectorBackend(Protocol):
    model_id: str
    device: str

    def detect_for_prompt(
        self,
        image_pil,
        text_prompt: str,
        box_threshold: float = DEFAULT_BOX_THRESHOLD,
        text_threshold: float = DEFAULT_TEXT_THRESHOLD,
    ) -> list[dict]: ...


class GroundingDINOWrapper:
    """Local GroundingDINO implementation of DetectorBackend."""

    def __init__(self, model, processor, device: str, model_id: str):
        self.model = model
        self.processor = processor
        self.device = device
        self.model_id = model_id

    @classmethod
    def load(cls, model_id: str = DEFAULT_MODEL_ID, device: str | None = None):
        ensure_available()
        load_mode = "online"
        try:
            processor = AutoProcessor.from_pretrained(model_id, local_files_only=True)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id, local_files_only=True)
            load_mode = "offline-cache"
        except Exception:
            processor = AutoProcessor.from_pretrained(model_id)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id)

        if device is None:
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(f"CUDA device requested but CUDA is not available: {device}")
        model = model.to(device)
        model.eval()
        wrapper = cls(model=model, processor=processor, device=device, model_id=model_id)
        wrapper.load_mode = load_mode
        return wrapper

    def detect_for_prompt(
        self,
        image_pil,
        text_prompt: str,
        box_threshold: float = DEFAULT_BOX_THRESHOLD,
        text_threshold: float = DEFAULT_TEXT_THRESHOLD,
    ) -> list[dict]:
        prompt = text_prompt if text_prompt.endswith(".") else f"{text_prompt}."
        inputs = self.processor(images=image_pil, text=prompt, return_tensors="pt").to(self.device)

        with torch.no_grad():
            outputs = self.model(**inputs)

        results = self.processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=box_threshold,
            text_threshold=text_threshold,
            target_sizes=[image_pil.size[::-1]],
        )[0]

        width, height = image_pil.size
        max_area = MAX_AREA_FRAC * width * height
        detections = []
        for idx in range(len(results["boxes"])):
            box = results["boxes"][idx].tolist()
            left, top, right, bottom = [int(value) for value in box]
            top = max(0, min(top, height - 1))
            left = max(0, min(left, width - 1))
            bottom = max(0, min(bottom, height))
            right = max(0, min(right, width))
            if (right - left) * (bottom - top) > max_area:
                continue

            label = results["labels"][idx]
            detections.append(
                {
                    "bbox": {"top": top, "left": left, "bottom": bottom, "right": right},
                    "confidence": round(results["scores"][idx].item(), 4),
                    "label": label if isinstance(label, str) else str(label),
                }
            )

        detections.sort(key=lambda item: item["confidence"], reverse=True)
        return detections


def load_detector(kind: str = DEFAULT_BACKEND, **kwargs) -> DetectorBackend:
    if kind == "grounding_dino":
        return GroundingDINOWrapper.load(
            model_id=kwargs.get("model_id", DEFAULT_MODEL_ID),
            device=kwargs.get("device"),
        )
    if kind == "qwen_remote":
        from lerobot.data_platform.precompute.labeling.qwen_remote import (
            DEFAULT_QWEN_ENDPOINT,
            DEFAULT_QWEN_MODEL,
            QwenRemoteDetector,
        )

        return QwenRemoteDetector.load(
            endpoint=kwargs.get("endpoint", DEFAULT_QWEN_ENDPOINT),
            model=kwargs.get("model", DEFAULT_QWEN_MODEL),
            min_pixels=kwargs.get("min_pixels", 1024),
            max_pixels=kwargs.get("max_pixels", 9800),
            token=kwargs.get("token"),
        )
    if kind == "qwen_dashscope":
        from lerobot.data_platform.precompute.labeling.qwen_dashscope import (
            DEFAULT_DASHSCOPE_BASE_URL,
            DEFAULT_DASHSCOPE_MODEL,
            QwenDashScopeDetector,
        )

        return QwenDashScopeDetector.load(
            base_url=kwargs.get("endpoint", DEFAULT_DASHSCOPE_BASE_URL),
            model=kwargs.get("model", DEFAULT_DASHSCOPE_MODEL),
            api_key=kwargs.get("token"),
        )
    raise ValueError(f"Unknown detector backend: {kind}")
