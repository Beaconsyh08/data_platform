"""Evidence-driven fusion with blind event discovery, adversarial review and bounded repair."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import shutil
import time
from pathlib import Path

from lerobot.data_platform.temporal_caption import PROMPT, validate_result, write_json

VERSION = "fusion-review-1"
PARTITION = (
    PROMPT
    + """
Additionally give each segment boundary_interval: [earliest_possible_start, latest_possible_start],
integer inclusive uncertainty bounds containing start_frame; first boundary is [0,0].
Never claim exact contact from sparse or occluded observations. Maintain stable object descriptions.
You may add scene and quality but do not invent task success. All segments must cover the ENTIRE episode.
"""
)
LEDGER = """Independently inspect the observed demonstration. Do not propose a caption partition.
Return JSON {"scene":"description", "objects":[{"id":"o1","description":"visual identity"}],
"events":[{"start_frame":0,"end_frame":10,"object_id":"o1 or unknown", "arm":"left|right|both|none",
"description":"observed interaction", "evidence":"camera and frame evidence", "uncertainty":"ambiguity"}],
"limitations":["observation limitation"]}. Event ranges are half-open and may overlap.
Track the same object across cameras, including handovers. Do not confuse image sides with manipulators.
Describe observations, not commanded actions. The task instruction is not evidence that it happened.
"""
REVIEW = """Audit the proposed annotation against the supplied independent event ledger AND fresh images.
Find missing short actions/regrasps/failures, object identity swaps, incorrect arms, unsupported contact,
misplaced boundaries, captions inconsistent with their interval, and incorrect task-compliance claims.
A ledger and previous reviewer can also be wrong: cite visual evidence rather than trusting either.
Return JSON {"issues":[{"start_frame":0,"end_frame":10,"severity":"major|minor",
"kind":"omission|identity|arm|boundary|unsupported|task|other", "description":"problem and evidence",
"suggestion":"specific correction or retain uncertainty"}], "notes":"scope and remaining limits"}.
Use half-open frame ranges. An empty issues list means no issue detected, not human validation.
"""


def uniform_frames(count: int, fps: float, hz: float, offset: int = 0) -> list[int]:
    return sorted({0, count - 1, *range(offset, count, max(1, round(fps / hz)))})


def focus_frames(count: int, fps: float, centers: list[int]) -> list[int]:
    frames = set(uniform_frames(count, fps, 2))
    for center in centers:
        frames.update(range(max(0, center - round(fps)), min(count, center + round(fps) + 1), 3))
    return sorted(frames)


def validate_ledger(value: dict, count: int) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise ValueError("Missing independent event ledger")
    for event in value["events"]:
        validate_range(event, count)
        if not isinstance(event.get("description"), str) or not event["description"].strip():
            raise ValueError("Missing event description")
    return value


def validate_range(value: dict, count: int) -> None:
    start, end = value.get("start_frame"), value.get("end_frame")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= count:
        raise ValueError("Invalid evidence frame range")


def validate_partition(value: dict, count: int) -> dict:
    validate_result(value, count)
    for segment in value["segments"]:
        interval = segment.get("boundary_interval")
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(type(v) is not int for v in interval)
            or not 0 <= interval[0] <= segment["start_frame"] <= interval[1] < count
        ):
            raise ValueError(
                f"Invalid boundary uncertainty interval {interval!r} for segment start {segment['start_frame']}; "
                f"provide two INTEGER bounds [lo, hi] with 0 <= lo <= {segment['start_frame']} <= hi < {count}. "
                "This interval describes the START boundary, not the segment end."
            )
    if value["segments"][0]["boundary_interval"] != [0, 0]:
        raise ValueError("First boundary must be [0,0]")
    return value


def validate_review(value: dict, count: int) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("issues"), list):
        raise ValueError("Missing review issues")
    for issue in value["issues"]:
        validate_range(issue, count)
        if issue.get("severity") not in {"major", "minor"} or not isinstance(issue.get("description"), str):
            raise ValueError("Invalid review issue")
    return value


def signal_evidence(meta: dict) -> dict:
    context = review_context_from_meta(meta)
    events = []
    for side in ("left", "right"):
        values = [row.get(f"{side}_gripper_pos") for row in meta["signals"]]
        if not values or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            continue
        lo, hi = min(values), max(values)
        if hi - lo < 1e-6:
            continue
        threshold = (lo + hi) / 2
        last = -math.inf
        for frame in range(1, len(values)):
            if (values[frame] >= threshold) != (values[frame - 1] >= threshold) and frame - last >= max(
                1, round(meta["fps"] * 0.15)
            ):
                events.append(
                    {
                        "frame": frame,
                        "side": side,
                        "direction": "rise" if values[frame] >= threshold else "fall",
                    }
                )
                last = frame
    return {
        "signals": context[:: max(1, round(meta["fps"] / 6))],
        "events": events,
        "limitations": "Speeds use source position units/s; gripper midrange crossings are candidates, not contact/open/close truth.",
    }


def review_context_from_meta(meta: dict) -> list[dict]:
    rows = []
    previous = None
    for item in meta["signals"]:
        row = {"frame": item["frame"]}
        for side in ("left", "right"):
            pose, old = item.get(f"{side}_arm_pose"), (previous or {}).get(f"{side}_arm_pose")
            row[f"{side}_pose"] = pose
            row[f"{side}_gripper"] = item.get(f"{side}_gripper_pos")
            row[f"{side}_speed"] = round(math.dist(pose[:3], old[:3]) * meta["fps"], 4) if pose and old else 0
        rows.append(row)
        previous = item
    return rows


def infer_fusion(
    bundle: Path, output: Path, dataset_key: str, model: str = "qwen3.8-max", *, client=None, progress=None
):
    """Run independent hypotheses and a blind audit; persist stage receipts for explicit same-input resume."""
    from PIL import Image

    from lerobot.data_platform.qwen import QwenClient

    client = client or QwenClient(timeout_s=600)
    meta = json.loads((bundle / "episode.json").read_text())
    count, fps = meta["frame_count"], meta["fps"]
    if count < 2:
        raise ValueError("Fusion requires at least two frames")
    output.mkdir(parents=True, exist_ok=True)
    identity = {
        "version": VERSION,
        "source_digest": meta["source_digest"],
        "dataset_key": dataset_key,
        "model": model,
    }
    marker = output / "fusion-input.json"
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise ValueError("Cannot resume fusion using changed inputs")
    if not marker.exists() and any(output.iterdir()):
        raise FileExistsError("Fusion output is not an empty directory")
    write_json(marker, identity)
    stages = []
    common = {k: meta[k] for k in ("episode_index", "frame_count", "fps", "task_hint", "cameras")}

    def ask(stage, instructions, frames, evidence, validator, *, video=False, crops=()):
        if progress:
            progress(f"Fusion + Review: {stage}")
        path = output / f"stage-{stage}.json"
        # Each receipt binds the full prompt and input sampling, not only the stage name.
        text = instructions + "\nOBSERVATIONS:\n" + json.dumps({**common, **evidence}, ensure_ascii=False)
        signature = hashlib.sha256(
            json.dumps([text, frames, list(crops)], ensure_ascii=False).encode()
        ).hexdigest()
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt["signature"] != signature:
                raise ValueError("Fusion stage inputs changed; use a new output directory")
            validator(receipt["result"], count)
            stages.append(receipt)
            return receipt["result"]
        content = [{"type": "text", "text": text}]
        urls = []
        for frame in frames:
            url = (
                "data:image/jpeg;base64,"
                + base64.b64encode((bundle / "frames" / f"{frame:06d}.jpg").read_bytes()).decode()
            )
            if video:
                urls.append(url)
            else:
                content.extend(
                    [
                        {"type": "text", "text": f"Frame {frame}, t={frame / fps:.3f}s"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ]
                )
        if video:
            content.append({"type": "video", "video": urls, "fps": (len(frames) - 1) * fps / (count - 1)})
        for frame in crops:
            with Image.open(bundle / "frames" / f"{frame:06d}.jpg") as image:
                for index, camera in enumerate(meta["cameras"]):
                    panel = image.crop((224 * index, 24, 224 * (index + 1), 248))
                    stream = io.BytesIO()
                    panel.save(stream, format="JPEG", quality=95)
                    url = "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()
                    content.extend(
                        [
                            {
                                "type": "text",
                                "text": f"Single camera {camera}, frame {frame}; same source resolution",
                            },
                            {"type": "image_url", "image_url": {"url": url}},
                        ]
                    )
        started = time.time()
        for attempt in range(3):
            response = client.post_chat_completion(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "enable_thinking": False,
                    "temperature": 0.1,
                    "max_tokens": 14000,
                    "response_format": {"type": "json_object"},
                }
            )
            try:
                value = validator(json.loads(response["choices"][0]["message"]["content"]), count)
                break
            except (ValueError, KeyError, TypeError) as exc:
                write_json(
                    output / f"invalid-{stage}-{attempt}.json", {"error": str(exc), "response": response}
                )
                if attempt == 2:
                    raise
                content.append(
                    {
                        "type": "text",
                        "text": f"Your last response failed structural validation: {exc}. Correct the schema without inventing new evidence. Return corrected complete JSON. Previous response:\n"
                        + response["choices"][0]["message"]["content"],
                    }
                )
        receipt = {
            "stage": stage,
            "signature": signature,
            "result": value,
            "frames": frames,
            "crop_frames": list(crops),
            "usage": response.get("usage"),
            "elapsed_s": time.time() - started,
        }
        write_json(path, receipt)
        (output / f"stage-{stage}.prompt.txt").write_text(text)
        stages.append(receipt)
        return value

    signals = signal_evidence(meta)
    wide = uniform_frames(count, fps, 3)
    if len(wide) > 160:
        raise ValueError("Fusion currently supports short episodes up to approximately 53 seconds")
    global_view = ask("01-global", LEDGER, wide, {}, validate_ledger)
    visual = ask("02-visual", PARTITION, wide, {}, validate_partition)
    video_frames = sorted({round(i * (count - 1) / (min(80, count) - 1)) for i in range(min(80, count))})
    events = ask(
        "03-events",
        PARTITION + "\nUse visual sequence and measured event candidates jointly.",
        video_frames,
        signals,
        validate_partition,
        video=True,
    )
    centers = sorted(
        {s["start_frame"] for r in (visual, events) for s in r["segments"][1:]}
        | {e["frame"] for e in signals["events"]}
    )
    dense = focus_frames(count, fps, centers)
    # Inspect all dense windows in bounded independent packets; do not silently discard evidence.
    packets = []
    for i in range(0, len(dense), 140):
        frames = dense[i : i + 140]
        crop_frames = frames[:: max(1, len(frames) // 8)][:8]
        packets.append(
            ask(f"04-evidence-{i // 140}", LEDGER, frames, signals, validate_ledger, crops=crop_frames)
        )
    draft = ask(
        "05-fusion",
        PARTITION
        + "\nResolve conflicting hypotheses using the supplemental visual evidence. Do not majority-vote. Retain ambiguity when unresolved.",
        wide,
        {
            "global": global_view,
            "visual_hypothesis": visual,
            "event_hypothesis": events,
            "focused_observations": packets,
            **signals,
        },
        validate_partition,
    )
    blind_frames = uniform_frames(count, fps, 5, max(1, round(fps / 10)))
    blind = ask(
        "06-blind-audit",
        LEDGER
        + "\nActively look for short missed regrasp/transfer/failed attempts. No candidate captions are provided.",
        blind_frames[:140],
        signals,
        validate_ledger,
    )
    for i in range(140, len(blind_frames), 140):
        extra = ask(f"06-blind-audit-{i // 140}", LEDGER, blind_frames[i : i + 140], signals, validate_ledger)
        blind = {**blind, "events": [*blind["events"], *extra["events"]]}
    review = ask(
        "07-review", REVIEW, wide, {"candidate": draft, "independent_events": blind}, validate_review
    )
    initial_review = copy.deepcopy(review)
    final = copy.deepcopy(draft)
    repairs = []
    for iteration in range(2):
        if not review["issues"]:
            break
        issues = review["issues"]
        focus = sorted(
            {
                f
                for issue in issues
                for f in range(
                    max(0, issue["start_frame"] - round(fps)), min(count, issue["end_frame"] + round(fps)), 2
                )
            }
        )
        evidence = []
        for i in range(0, len(focus), 140):
            frames = focus[i : i + 140]
            evidence.append(
                ask(
                    f"08-recheck-{iteration}-{i // 140}",
                    LEDGER,
                    frames,
                    {},
                    validate_ledger,
                    crops=frames[:: max(1, len(frames) // 8)][:8],
                )
            )
        frozen = [
            s
            for s in final["segments"]
            if not any(
                s["start_frame"] < e["end_frame"] + fps and s["end_frame"] > e["start_frame"] - fps
                for e in issues
            )
        ]

        def validate_repair(value, frames, frozen=frozen):
            validate_partition(value, frames)
            if any(segment not in value["segments"] for segment in frozen):
                raise ValueError("Unaffected frozen segments must remain exactly unchanged")
            return value

        before = final
        final = ask(
            f"09-repair-{iteration}",
            PARTITION
            + "\nRepair only flagged intervals and adjacent boundaries. Keep frozen_segments verbatim. Reviewer claims are fallible; use fresh evidence and retain uncertainty if needed.",
            wide,
            {"candidate": final, "issues": issues, "fresh_evidence": evidence, "frozen_segments": frozen},
            validate_repair,
        )
        repairs.append({"issues": issues, "before": before, "after": final})
        review = ask(
            f"10-verify-{iteration}",
            REVIEW,
            wide,
            {"candidate": final, "independent_events": blind, "fresh_evidence": evidence},
            validate_review,
        )
    shutil.copyfile(bundle / "video.mp4", output / "video.mp4")
    write_json(output / "episode.json", meta)
    totals = {
        k: sum((s.get("usage") or {}).get(k, 0) for s in stages)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    audit = {
        "status": "unresolved" if review["issues"] else "no_issues_detected",
        "human_reviewed": False,
        "initial_issues": initial_review["issues"],
        "remaining_issues": review["issues"],
        "notes": review.get("notes", ""),
        "repairs": repairs,
        "independent_events": blind,
        "stages": stages,
    }
    for variant, result in (("fusion_draft", draft), ("fusion_reviewed", final)):
        write_json(
            output / f"{variant}.json",
            {
                "schema_version": 1,
                "scheme_id": "fusion_review",
                "dataset_key": dataset_key,
                "dataset_name": meta["dataset_name"],
                "episode_index": meta["episode_index"],
                "frame_count": count,
                "fps": fps,
                "duration_s": count / fps,
                "variant": variant,
                "model": model,
                "created_at": time.time(),
                "elapsed_s": sum(s["elapsed_s"] for s in stages),
                "usage": totals,
                "source_digest": meta["source_digest"],
                "pipeline_version": VERSION,
                "sampled_frames": dense,
                "result": result,
                "review": {**audit, "status": "draft" if variant == "fusion_draft" else audit["status"]},
            },
        )
