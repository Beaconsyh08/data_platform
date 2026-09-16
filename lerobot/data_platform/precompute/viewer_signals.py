"""Resolve display semantics against actual CSV labels without changing processing profiles."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from lerobot.data_platform.precompute.signal_columns import signal_columns


def viewer_csv_labels(header: list[str], features: dict | None = None) -> list[str]:
    """Disambiguate legacy duplicate headers only when the complete feature order matches."""
    if len(header) == len(set(header)):
        return header
    features = features or {}
    legacy = []
    for column in signal_columns(features):
        names = features[column["key"]].get("names")
        while isinstance(names, dict) and names:
            names = next(iter(names.values()))
        if isinstance(names, (list, tuple)) and len(names) == len(column["value"]):
            legacy.extend(str(name) for name in names)
        else:
            legacy.extend(column["value"])
    if not legacy or header[:1] != ["timestamp"] or header[1 : 1 + len(legacy)] != legacy:
        raise ValueError("Ambiguous CSV columns do not match feature metadata; rebuild the Viewer cache.")
    qualified = [label for c in signal_columns(features, qualify=True) for label in c["value"]]
    result = ["timestamp", *qualified, *header[1 + len(legacy) :]]
    if len(result) != len(set(result)):
        raise ValueError("Duplicate CSV columns remain; rebuild the Viewer cache.")
    return result


@dataclass(frozen=True)
class ViewerSignal:
    label: str
    source: str
    index: int
    part: str = ""
    component: str = ""
    hidden: bool = False


def viewer_signal_presentation(features: dict, columns: list[dict], robot_type: str = "") -> dict:
    """Use feature positions for joints and declared component names for end effectors.

    Labels are matched to both historic positional CSVs and qualified named CSVs.
    Index-like dimension names are never interpreted as zero-based vector positions.
    """
    labels = {label for column in columns for label in column["value"]}
    descriptions = signal_columns(features)
    qualified = {c["key"]: c for c in signal_columns(features, qualify=True)}
    robot = robot_type.strip().lower()
    umi_robot = robot in {"umi", "umi-gripperbody-head", "umi-gripper-head"}
    # Older manifests omit robot_type. A complete declared pose layout still carries
    # explicit display semantics; recognizing it does not assign a robot/data profile.
    required_pose_names = {
        f"{part}_{axis}"
        for part in ("left", "right", "head")
        for axis in ("x", "y", "z", "qw", "qx", "qy", "qz")
    } | {"left_gripper", "right_gripper"}
    named_pose_layout = False
    for key in ("state", "action", "observation.state"):
        names = features.get(key, {}).get("names")
        while isinstance(names, dict) and names:
            names = next(iter(names.values()))
        if isinstance(names, (list, tuple)) and required_pose_names <= set(map(str, names)):
            named_pose_layout = True
    signals = []
    for column in descriptions:
        key = column["key"]
        names = features[key].get("names")
        while isinstance(names, dict) and names:
            names = next(iter(names.values()))
        if not isinstance(names, (list, tuple)) or len(names) != len(column["value"]):
            names = [str(i) for i in range(len(column["value"]))]
        for index, (label, name) in enumerate(zip(column["value"], names, strict=True)):
            candidates = (label, qualified[key]["value"][index], f"{key}_{index}")
            actual = next((candidate for candidate in candidates if candidate in labels), None)
            if actual is None:
                continue
            part, component = "", ""
            native = re.fullmatch(r"(head|left_arm|right_arm)_(?:quaternion_)?pose", key)
            if native and str(name) in {"x", "y", "z", "roll", "pitch", "yaw", "qw", "qx", "qy", "qz"}:
                part, component = native[1].replace("_arm", ""), str(name)
            elif key in {"left_gripper_pos", "right_gripper_pos"}:
                part, component = key.split("_")[0], "gripper"
            elif (umi_robot or named_pose_layout) and key in {"state", "action", "observation.state"}:
                packed = re.fullmatch(
                    r"(head|left|right)_(x|y|z|roll|pitch|yaw|qw|qx|qy|qz|gripper)", str(name)
                )
                if packed:
                    part, component = packed.groups()
            hidden = bool(
                native
                and "quaternion" in key
                and component in {"x", "y", "z"}
                and f"{native[1]}_pose" in features
            )
            signals.append(ViewerSignal(actual, key, index, part, component, hidden))
    mode = "end_effector" if any(s.part and s.component != "gripper" for s in signals) else "joint"
    return {"mode": mode, "series": [asdict(signal) for signal in signals]}
