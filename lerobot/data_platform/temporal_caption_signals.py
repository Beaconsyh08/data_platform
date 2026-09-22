"""Deterministic signal events and temporal alignment; no semantic or contact verdicts."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

VERSION = "signal-analysis-1"


@dataclass(frozen=True)
class SignalEvent:
    frame: int
    feature: str
    dimension: int
    kind: str
    magnitude: float
    threshold: float


@dataclass(frozen=True)
class SignalAnalysis:
    enabled: bool
    events: tuple[SignalEvent, ...]
    channels: tuple[dict, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"version": VERSION, **asdict(self)}


def analyze_signals(meta: dict) -> SignalAnalysis:
    """Detect per-dimension jumps and motion on/off using full-rate, finite measurements.

    Thresholds are scale-relative heuristics, not calibrated probabilities. Never bridge
    missing samples or infer action/state correspondence from equal vector dimensions.
    """
    limits = (
        "Temporal agreement does not prove semantic agreement, contact or task success.",
        "Action/state dimensions, units and correspondence are not assumed; no tracking error is inferred.",
        "Thresholds are episode-relative; constant-rate motion and small changes can go undetected.",
    )
    if meta.get("include_signals") is False:
        return SignalAnalysis(False, (), (), (*limits, "Signal inputs disabled."))
    rows, count, fps = meta.get("signals", []), meta["frame_count"], meta["fps"]
    if len(rows) != count or any(row.get("frame") != i for i, row in enumerate(rows)):
        raise ValueError("Signal rows must align with every episode frame")
    keys = sorted({k for row in rows for k in row if k != "frame"})
    events, channels = [], []
    separation = max(1, round(fps * 0.15))
    for key in keys:
        widths = {len(v) if isinstance(v, list) else 1 for row in rows if (v := row.get(key)) is not None}
        if len(widths) != 1 or not next(iter(widths), 0):
            channels.append({"feature": key, "status": "invalid_shape"})
            continue
        for dim in range(next(iter(widths))):
            values = []
            for row in rows:
                value = row.get(key)
                value = value[dim] if isinstance(value, list) else value
                values.append(float(value) if type(value) in (int, float) else float("nan"))
            x = np.asarray(values)
            finite = np.isfinite(x)
            valid = finite[1:] & finite[:-1]
            if valid.sum() < 2:
                channels.append({"feature": key, "dimension": dim, "status": "insufficient_finite_data"})
                continue
            delta = np.abs(np.diff(x))
            d = delta[valid]
            span = float(np.ptp(x[finite]))
            median = float(np.median(d))
            mad = float(np.median(np.abs(d - median)))
            threshold = max(median + 6 * 1.4826 * mad, span * 0.05, 1e-9)
            channels.append(
                {
                    "feature": key,
                    "dimension": dim,
                    "status": "ok",
                    "valid_pairs": int(valid.sum()),
                    "threshold": threshold,
                    "range": span,
                }
            )
            candidates = [
                (i + 1, "change", float(delta[i]), threshold)
                for i in range(count - 1)
                if valid[i] and delta[i] > threshold
            ]
            # Keep slow gripper transitions as candidates even without an impulsive delta.
            if "gripper" in key and span > 1e-6:
                midpoint = float((np.min(x[finite]) + np.max(x[finite])) / 2)
                candidates.extend(
                    (
                        i + 1,
                        "midrange_rise" if x[i + 1] >= midpoint else "midrange_fall",
                        float(delta[i]),
                        midpoint,
                    )
                    for i in range(count - 1)
                    if valid[i] and (x[i] >= midpoint) != (x[i + 1] >= midpoint)
                )
            # Motion regimes complement impulse detection for sustained linear ramps.
            motion_threshold = max(float(np.quantile(d, 0.9)) * 0.2, span * 1e-5, 1e-9)
            moving = delta > motion_threshold
            for i in range(1, len(delta)):
                if valid[i - 1] and valid[i] and moving[i] != moving[i - 1]:
                    candidates.append(
                        (
                            i + 1,
                            "motion_start" if moving[i] else "motion_stop",
                            float(delta[i]),
                            motion_threshold,
                        )
                    )
            # Suppress nearby detections per kind/dimension, retaining strongest first.
            selected: dict[str, list[int]] = {}
            for frame, kind, magnitude, cutoff in sorted(candidates, key=lambda c: (-c[2], c[0])):
                accepted = selected.setdefault(kind, [])
                if all(abs(frame - old) >= separation for old in accepted):
                    accepted.append(frame)
                    events.append(SignalEvent(frame, key, dim, kind, magnitude, cutoff))
    return SignalAnalysis(
        True,
        tuple(sorted(events, key=lambda e: (e.frame, e.feature, e.dimension, e.kind))),
        tuple(channels),
        limits,
    )


def boundary_matrix(visual: dict, joint: dict, signals: SignalAnalysis, fps: float) -> dict:
    """Cluster by bounded span (no transitive chaining); preserve every contributing source."""
    tolerance = max(1, round(fps * 0.25))
    evidence = []
    for source, partition in (("visual", visual), ("joint", joint)):
        evidence.extend({"frame": s["start_frame"], "source": source} for s in partition["segments"][1:])
    evidence.extend({"source": "signal", **asdict(event)} for event in signals.events)
    groups: list[list[dict]] = []
    for item in sorted(evidence, key=lambda e: (e["frame"], e["source"])):
        if not groups or item["frame"] - groups[-1][0]["frame"] > tolerance:
            groups.append([])
        groups[-1].append(item)
    rows = []
    for i, group in enumerate(groups):
        sources = {e["source"] for e in group}
        hypothesis = sources & {"visual", "joint"}
        status = (
            "temporal_support"
            if "visual" in sources and "signal" in sources
            else "signal_only"
            if not hypothesis
            else "hypothesis_disagreement"
            if len(hypothesis) == 1
            else "visual_joint_only"
        )
        # Repeated signal dimensions must not bias the representative frame.
        frame = int(np.median(sorted({e["frame"] for e in group})))
        rows.append(
            {
                "id": f"b{i}",
                "frame": frame,
                "range": [group[0]["frame"], group[-1]["frame"]],
                "status": status,
                "sources": sorted(sources),
                "evidence": group,
            }
        )
    return {
        "tolerance_frames": tolerance,
        "signal_status": "enabled" if signals.enabled else "disabled",
        "meaning": "Temporal proximity only; disagreement is between boundary hypotheses, not proof of semantic conflict.",
        "rows": rows,
    }


def signal_audit(partition: dict, signals: SignalAnalysis, fps: float) -> dict:
    """Account for independent signal events against final boundaries without declaring omissions."""
    boundaries = [s["start_frame"] for s in partition["segments"][1:]]
    tolerance = max(1, round(fps * 0.25))
    unmatched = [
        asdict(e) for e in signals.events if not any(abs(e.frame - b) <= tolerance for b in boundaries)
    ]
    return {
        "enabled": signals.enabled,
        "unmatched_events": unmatched,
        "meaning": "Unmatched changes are review candidates; continuous actions can contain signal changes.",
    }


def cross_modal_report(
    visual: dict, joint: dict, partition: dict, signals: SignalAnalysis, fps: float
) -> dict:
    """Build the same reproducible report at inference and publication boundaries."""
    matrix = boundary_matrix(visual, joint, signals, fps)
    return {
        "local_analysis": signals.to_dict(),
        "boundary_matrix": matrix,
        "signal_audit": signal_audit(partition, signals, fps),
        "segment_support": [
            {
                "start_frame": segment["start_frame"],
                "candidate_ids": [
                    row["id"]
                    for row in matrix["rows"]
                    if abs(row["frame"] - segment["start_frame"]) <= matrix["tolerance_frames"]
                ],
            }
            for segment in partition["segments"]
        ],
    }
