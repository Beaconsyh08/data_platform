from __future__ import annotations


def _xc(detection: dict) -> float:
    bbox = detection["bbox"]
    return (bbox["left"] + bbox["right"]) / 2


def _center(detection: dict) -> tuple[float, float]:
    bbox = detection["bbox"]
    return (bbox["left"] + bbox["right"]) / 2, (bbox["top"] + bbox["bottom"]) / 2


def _area(detection: dict) -> float:
    bbox = detection["bbox"]
    return max(0, bbox["right"] - bbox["left"]) * max(0, bbox["bottom"] - bbox["top"])


def _dist(a: dict, b: dict) -> float:
    ax, ay = _center(a)
    bx, by = _center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _hand_side_key(detection: dict, arm: str | None) -> float:
    x = _xc(detection)
    if arm == "left":
        return x
    if arm == "right":
        return -x
    return -float(detection.get("confidence", 0.0))


def _last_frame_movement(first: dict, detections_target_last: list[dict] | None) -> float:
    if not detections_target_last:
        return 0.0
    nearest = min(detections_target_last, key=lambda item: _dist(first, item))
    center_dist = _dist(first, nearest)
    area_first = max(_area(first), 1.0)
    area_last = max(_area(nearest), 1.0)
    area_delta = abs(area_first - area_last) ** 0.5
    return center_dist + 0.25 * area_delta


def select_bbox_with_context(
    detections_target: list[dict],
    detections_ref: list[dict] | None,
    direction: str | None,
    *,
    arm: str | None = None,
    detections_target_last: list[dict] | None = None,
) -> tuple[dict | None, bool, str]:
    """Select the task target from all target-class detections."""
    if not detections_target:
        return None, False, "no_target_detections"
    if direction is None:
        arm = arm if arm in {"left", "right"} else None
        if arm:
            movement_scores = {
                idx: _last_frame_movement(detection, detections_target_last)
                for idx, detection in enumerate(detections_target)
            }
            max_movement = max(movement_scores.values(), default=0.0)
            moved_indices = {
                idx for idx, score in movement_scores.items()
                if max_movement >= 20.0 and score >= max_movement - 5.0
            }
            candidates = [
                detection for idx, detection in enumerate(detections_target)
                if not moved_indices or idx in moved_indices
            ]
            selected = min(candidates, key=lambda item: (_hand_side_key(item, arm), -float(item.get("confidence", 0.0))))
            method = f"last_frame_motion_then_{arm}_hand_nearest" if moved_indices else f"{arm}_hand_nearest"
            return selected, True, method
        return detections_target[0], True, "top_confidence"
    if detections_ref is None:
        key = min if direction == "left" else max
        return key(detections_target, key=_xc), True, f"absolute_{direction}"

    best = None
    best_dist = float("inf")
    for target in detections_target:
        for reference in detections_ref:
            ok = _xc(target) < _xc(reference) if direction == "left" else _xc(target) > _xc(reference)
            if not ok:
                continue
            dist = _dist(target, reference)
            if dist < best_dist:
                best_dist = dist
                best = target

    if best is not None:
        return best, True, f"relative_{direction}_of_reference"
    return None, False, "no_candidate_on_correct_side"


def select_bbox(
    detections_target: list[dict],
    detections_ref: list[dict] | None,
    direction: str | None,
) -> tuple[dict | None, bool]:
    """Select the target bbox using the task direction and optional reference detections."""
    selected, relation_satisfied, _ = select_bbox_with_context(detections_target, detections_ref, direction)
    return selected, relation_satisfied
