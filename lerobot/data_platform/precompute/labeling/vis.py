from __future__ import annotations

from PIL import ImageDraw


def _draw_box(draw, detection: dict, color: str, width: int, tag: str) -> None:
    bbox = detection["bbox"]
    draw.rectangle([bbox["left"], bbox["top"], bbox["right"], bbox["bottom"]], outline=color, width=width)
    confidence = detection.get("confidence", 0.0)
    draw.text((bbox["left"], max(0, bbox["top"] - 12)), f"{tag} {confidence:.2f}", fill=color)


def draw_detections(
    image_pil,
    detections_target: list[dict],
    detections_ref: list[dict],
    selected: dict | None,
    parsed: dict,
):
    img = image_pil.copy()
    draw = ImageDraw.Draw(img)

    for detection in detections_target or []:
        _draw_box(draw, detection, "#fb923c", 2, f"toy {parsed['target']}")
    for detection in detections_ref or []:
        _draw_box(draw, detection, "#38bdf8", 2, parsed["reference"])
    if selected is not None:
        bbox = selected["bbox"]
        draw.rectangle([bbox["left"], bbox["top"], bbox["right"], bbox["bottom"]], outline="#020617", width=5)
        draw.rectangle([bbox["left"], bbox["top"], bbox["right"], bbox["bottom"]], outline="#22c55e", width=3)
        draw.text((bbox["left"], max(0, bbox["top"] - 12)), f"TARGET {selected['confidence']:.2f}", fill="#22c55e")

    header = parsed["target"]
    if parsed["direction"] is not None:
        header += f" | {parsed['direction']}"
    if parsed["reference"] is not None:
        header += f" of {parsed['reference']}"
    draw.text((4, 4), header, fill="white")
    return img
