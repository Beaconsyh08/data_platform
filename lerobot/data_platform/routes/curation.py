"""Shared Curation navigation, execution, review, and attempt-scoped publication."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import replace
from functools import wraps
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlencode

from flask import abort, jsonify, redirect, request, send_file

from lerobot.data_platform.curation import (
    CURATION_PROTOCOL,
    DATASET_OPERATIONS,
    OPERATIONS,
    CurationSnapshot,
    CurationTarget,
    artifact_path,
    publish_bundle,
    register_snapshot,
    snapshot_from_version,
)
from lerobot.data_platform.curation_execution import validate_parameters
from lerobot.data_platform.lifecycle import MATERIALIZATION_COMMITTED, MaterializationRun
from lerobot.data_platform.local_execution import enqueue_local
from lerobot.data_platform.precompute.data_profile import operation_capabilities, profile_from_info
from lerobot.data_platform.routes.control_plane import (
    _current_user,
    _role_denied,
    _validate_remote_output_path,
)
from lerobot.data_platform.task_catalog import content_digest


def register_curation_routes(app, ctx):
    def ledger():
        return ctx.lifecycle_store() if callable(ctx.lifecycle_store) else ctx.lifecycle_store

    def root():
        return ledger().root / "curation"

    def checked(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if request.method not in {"GET", "HEAD"} and not request.path.startswith("/api/agents/"):
                denied = (
                    _role_denied("operator", "data_manager", "admin") if ctx.control_plane_store else None
                )
                if denied:
                    return denied
            try:
                return handler(*args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=str(exc)), 403
            except KeyError as exc:
                return jsonify(error=str(exc)), 404
            except (ValueError, TypeError) as exc:
                status = (
                    409
                    if any(
                        term in str(exc).lower()
                        for term in (
                            "conflict",
                            "changed",
                            "synchronize",
                            "requires",
                            "unavailable",
                            "upgrade",
                        )
                    )
                    else 400
                )
                return jsonify(error=str(exc)), status

        return wrapped

    def resolve(payload):
        target = CurationTarget.from_dict(payload.get("target", payload))
        store = ledger()
        if target.location_id:
            if ctx.control_plane_store is None:
                raise ValueError("Agent datasets require the central console")
            location = ctx.control_plane_store.get_location(target.location_id)
            if target.dataset_key and target.dataset_key != location["dataset_key"]:
                raise ValueError("Target dataset does not match location")
            node = next(
                row for row in ctx.control_plane_store.list_nodes() if row["node_id"] == location["node_id"]
            )
            head = store._get_record("curation_locations", target.location_id)
            version_id = target.dataset_version_id or (head or {}).get("dataset_version_id")
            target = replace(target, dataset_key=location["dataset_key"], dataset_version_id=version_id)
            if version_id and not any(
                replica.location_id == target.location_id
                for replica in store.list_replicas(dataset_version_id=version_id)
            ):
                raise ValueError("Version does not belong to this location")
            info = location.get("metadata") or {}
        else:
            key = ctx.repo_key(target.dataset_key)
            dataset, static = ctx.ensure_dataset_loaded(key)
            if getattr(dataset, "root", None) is None:
                raise ValueError("Select the owning Agent location for a cache-only dataset")
            info = json.loads((Path(dataset.root) / "meta" / "info.json").read_text())
            location = {
                "dataset_key": target.dataset_key,
                "root": str(dataset.root),
                "output_dir": str(Path(static).parent),
                "metadata": info,
            }
            from lerobot.data_platform.local_execution import agent_capabilities_for_local

            node = {
                "name": "Local executor",
                "status": "online",
                "capabilities": agent_capabilities_for_local(),
                "writable_roots": [str(Path(dataset.root).parent)],
            }
            versions = store.list_versions(dataset_key=target.dataset_key)
            head = store._get_record("curation_local_heads", target.dataset_key)
            version_id = (
                target.dataset_version_id
                or (head or {}).get("version_id")
                or (versions[0].version_id if versions else None)
            )
            if version_id and not store.version_has_dataset_key(version_id, target.dataset_key):
                raise ValueError("Version does not belong to this dataset")
            target = replace(target, dataset_version_id=version_id)
        version = store.get_version(target.dataset_version_id) if target.dataset_version_id else None
        snapshot = store.curation_snapshot(version.version_id) if version else None
        if version and snapshot is None:
            snapshot = snapshot_from_version(store, version, Path(location["root"])).to_dict()
        if snapshot:
            info = snapshot["info"]
        return target, location, node, snapshot, info

    def capabilities(target, node, snapshot, info):
        user = _current_user()
        editable = ctx.control_plane_store is None or bool(
            user and user["role"] in {"operator", "data_manager", "admin"}
        )
        caps = node.get("capabilities", {})
        semantics = operation_capabilities(profile_from_info(info), info.get("features", {}))
        aliases = {
            "curation.stage": "auto_stage",
            "curation.quality": "quality_flags",
            "curation.embedding": "embedding",
            "curation.project": "embedding",
        }
        operations = {}
        for operation in sorted(OPERATIONS):
            reason = None
            if not editable:
                reason = "Viewer accounts are read-only"
            elif node.get("status") == "offline":
                reason = "Execution node is offline; saved results remain available"
            elif caps.get("curation_protocol") != CURATION_PROTOCOL:
                reason = "Upgrade the execution node to Curation protocol 1"
            elif operation not in caps.get("curation_operations", []):
                reason = "Operation is unavailable on this execution node"
            elif operation != "curation.snapshot" and snapshot is None:
                reason = "Synchronize a dataset version first"
            elif not semantics.get(aliases.get(operation), {"available": True}).get("available"):
                reason = semantics[aliases[operation]].get("reason", "Unsupported dataset layout")
            elif operation == "curation.embedding" and not caps.get("curation_backends", {}).get(
                "embedding", {}
            ).get("openpi_available"):
                reason = "Embedding backend is unavailable on this execution node"
            operations[operation] = {
                "can_view": True,
                "can_edit": editable,
                "can_execute": reason is None,
                "reason": reason,
            }
        return {
            "target": target.to_dict(),
            "execution_node": node["name"],
            "operations": operations,
            "backends": caps.get("curation_backends", {}),
            "can_edit": editable,
        }

    def result_record(run):
        record = ledger()._get_record("curation_runs", run)
        if record is None:
            raise KeyError("Curation result not found")
        if ctx.control_plane_store:
            job = ctx.control_plane_store.get_job(run)
            if job["status"] == "done":
                record = {**record, "result": job["result"]}
        return record

    def input_files(runs, target):
        inputs = {}
        if not isinstance(runs, list) or len(runs) > 20:
            raise ValueError("Select at most 20 input results")
        for run in runs:
            record = result_record(str(run))
            bound = record["target"]
            if (
                bound.get("dataset_version_id") != target.dataset_version_id
                or bound.get("dataset_key") != target.dataset_key
                or bound.get("location_id") != target.location_id
            ):
                raise ValueError("Input result belongs to a different dataset version")
            manifest = json.loads((root() / "runs" / run / "curation-manifest.json").read_text())
            aliases = {}
            variant = record["result"].get("output_variant")
            if variant and record["operation"] in {"curation.labeling", "curation.tagging"}:
                folder = record["operation"].split(".")[1]
                stem = "labels" if folder == "labeling" else "tags"
                aliases = {
                    f"{folder}/{stem}_{variant}.jsonl": f"{folder}/{stem}.jsonl",
                    f"{folder}/{stem}_reviewed_{variant}.jsonl": f"{folder}/{stem}_reviewed.jsonl",
                    f"{folder}/source_{variant}.json": f"{folder}/source.json",
                }
            for name, entry in manifest["files"].items():
                if name.startswith("static/"):
                    relative = name.removeprefix("static/")
                    if relative in aliases.values():
                        continue
                    relative = aliases.get(relative, relative)
                    if relative in inputs and inputs[relative]["sha256"] != entry["sha256"]:
                        raise ValueError("Selected input runs contain conflicting artifacts")
                    inputs[relative] = {**entry, "run": run, "path": name}
        return inputs

    def submit(body):
        if ctx.control_plane_store is None:
            raise ValueError("Persistent Curation execution requires the central console")
        denied = _role_denied("operator", "data_manager", "admin")
        if denied:
            return denied
        target, location, node, snapshot, info = resolve(body)
        operation = str(body.get("operation") or "")
        parameters = validate_parameters(operation, body.get("parameters", {}))
        capability = capabilities(target, node, snapshot, info)["operations"][operation]
        if not capability["can_execute"]:
            raise ValueError(capability["reason"])
        backends = node.get("capabilities", {}).get("curation_backends", {})
        if operation == "curation.labeling":
            backend = parameters.get("backend", "grounding_dino")
            capability = backends.get("labeling", {}).get("backends", {}).get(backend, {})
            if not capability.get("available"):
                raise ValueError(
                    capability.get("error") or "Labeling backend is unavailable on this execution node"
                )
        if operation == "curation.tagging":
            from lerobot.data_platform.precompute.tagging.schema import selected_tag_defs

            definitions = selected_tag_defs(parameters.get("selected_tags"))
            backend = parameters.get("vlm_backend", "qwen_dashscope")
            ready = backends.get("tagging", {}).get("backends", {}).get(backend, {}).get("available")
            if not ready:
                if parameters.get("selected_tags") and any(item["backend"] == "vlm" for item in definitions):
                    raise ValueError("Selected tags require a configured VLM backend on the execution node")
                parameters["selected_tags"] = [
                    item["name"] for item in definitions if item["backend"] != "vlm"
                ]
        delivery = request.headers.get("Idempotency-Key")
        if not delivery or len(delivery) > 128:
            raise ValueError("An Idempotency-Key of 1–128 characters is required")
        actor = _current_user()
        options = {
            "target": target.to_dict(),
            "parameters": parameters,
            "snapshot": snapshot,
            "task_config": snapshot["task_config"]
            if snapshot
            else ledger().tasks.snapshot(target.dataset_key).to_dict(),
            "dataset_id": snapshot["version"]["dataset_id"]
            if snapshot
            else "ds_" + content_digest([target.location_id, target.dataset_key])[:20],
            "input_files": input_files(body.get("input_runs", []), target),
        }
        if body.get("workspace_id"):
            workspace = ledger().get_workspace(str(body["workspace_id"]))
            if workspace.base_dataset_version_id != target.dataset_version_id:
                raise ValueError("Workspace belongs to a different dataset version")
            if workspace.revision != int(body.get("expected_revision", -1)):
                raise ValueError("Workspace revision conflict")
            options["workspace"] = workspace.to_dict()
        if body.get("publish_workspace"):
            if operation != "curation.validate_source" or "workspace" not in options:
                raise ValueError("Publication requires source validation and a workspace revision")
            options["publish_workspace"] = {
                "workspace_id": options["workspace"]["workspace_id"],
                "revision": options["workspace"]["revision"],
                "reviewer": actor["username"],
                "reason": str(body.get("reason") or ""),
            }
        if operation == "curation.materialize":
            manifest = ledger().get_manifest(str(body.get("manifest_id") or ""))
            if (
                manifest.base_dataset_version_id != target.dataset_version_id
                or manifest.status != "published"
            ):
                raise ValueError("Select a published manifest for this dataset version")
            profile = (
                ledger().get_profile(str(body["profile_id"]), kind="materialization")
                if body.get("profile_id")
                else ledger().default_materialization_profile()
            )
            options.update(manifest=manifest.to_dict(), profile=profile.to_dict())
            options["task_config"] = manifest.rule_versions.get("task_config") or snapshot["task_config"]
        if operation in DATASET_OPERATIONS:
            source = PurePosixPath(location["root"])
            output = str(
                body.get("out_root")
                or source.parent
                / f"{source.name}_{operation.split('.')[-1]}_{content_digest([target.to_dict(), delivery])[:12]}"
            )
            if target.location_id:
                options["out_root"] = _validate_remote_output_path(
                    ctx.control_plane_store, target.location_id, output
                )
            else:
                output_path = Path(output).expanduser().resolve()
                source_path = Path(location["root"]).resolve()
                if (
                    output_path == source_path
                    or source_path in output_path.parents
                    or output_path in source_path.parents
                ):
                    raise ValueError("Output must be a new sibling dataset")
                if output_path.exists():
                    raise ValueError("Output already exists")
                options["out_root"] = str(output_path)
            options["parents"] = [
                ledger().get_version(parent).to_dict() for parent in snapshot["version"]["parent_version_ids"]
            ]
            options["source_batches"] = [
                ledger().get_source_batch(batch).to_dict()
                for batch in snapshot["version"].get("source_batch_ids", [])
            ]
        identity = content_digest([actor["user_id"], target.to_dict(), operation, body, delivery])
        store = ctx.control_plane_store
        if target.location_id:
            job = store.create_job(
                location_id=target.location_id,
                requested_by=actor["user_id"],
                operation=operation,
                options=options,
                idempotency_key=identity,
            )
        else:
            job = enqueue_local(
                app,
                ctx,
                {
                    "id": str(uuid.uuid4()),
                    "dataset_key": target.dataset_key,
                    "output_root": options.get("out_root"),
                },
                command={"operation": operation, "options": options},
                idempotency_key=identity,
            )
        return jsonify(job=store.job_manager.decorate(job, actor)), 202

    app.extensions["data_platform_submit_curation"] = submit
    app.extensions["data_platform_resolve_curation"] = resolve

    @app.get("/api/curation/capabilities")
    @checked
    def curation_capabilities():
        target, _, node, snapshot, info = resolve(request.args.to_dict())
        return jsonify(capabilities(target, node, snapshot, info))

    @app.post("/api/curation/jobs")
    @checked
    def curation_jobs_create():
        return submit(request.get_json(silent=True) or {})

    @app.get("/api/curation/context")
    @checked
    def curation_context():
        target, location, node, snapshot, info = resolve(request.args.to_dict())
        version_id = target.dataset_version_id
        runs = [
            row
            for row in ledger()._list_records("curation_runs")
            if row["target"].get("dataset_version_id") == version_id
            and row["target"].get("dataset_key") == target.dataset_key
            and row["target"].get("location_id") == target.location_id
        ]
        workspaces = [
            row
            for row in ledger()._list_records("workspaces")
            if row["base_dataset_version_id"] == version_id
        ]
        return jsonify(
            **capabilities(target, node, snapshot, info),
            info=info,
            episodes=snapshot["episodes"] if snapshot else [],
            runs=runs,
            workspaces=workspaces,
            episode_refs=snapshot["version"]["episode_refs"] if snapshot else [],
            manifests=ledger().list_manifests(base_dataset_version_id=version_id) if version_id else [],
            viewer_url=(location.get("metadata") or {}).get("viewer_url"),
        )

    @app.get("/curation")
    @checked
    def curation_page():
        target, _, _, _, _ = resolve(request.args.to_dict())
        page = request.args.get("page", "annotation")
        if page not in {"explore", "quality", "annotation", "dataset_build"}:
            page = "annotation"
        selection = f"remote:{target.location_id}" if target.location_id else target.dataset_key
        return redirect("/?" + urlencode({"select": selection, "page": page}))

    @app.post("/api/curation/drafts")
    @checked
    def curation_draft_create():
        body = request.get_json(silent=True) or {}
        target, _, _, snapshot, _ = resolve(body)
        if snapshot is None:
            raise ValueError("Synchronize a dataset version first")
        workspace = ledger().create_workspace(
            target.dataset_version_id,
            owner=(_current_user() or {}).get("username", "local-user"),
            rule_versions={"task_config": snapshot["task_config"]},
        )
        return jsonify(workspace=workspace.to_dict()), 201

    @app.post("/api/curation/drafts/<workspace_id>/publish")
    @checked
    def curation_draft_publish(workspace_id):
        body = request.get_json(silent=True) or {}
        return submit(
            {
                **body,
                "workspace_id": workspace_id,
                "operation": "curation.validate_source",
                "publish_workspace": True,
            }
        )

    @app.patch("/api/curation/drafts/<workspace_id>/episodes/<int:episode_index>")
    @checked
    def curation_review_episode(workspace_id, episode_index):
        body = request.get_json(silent=True) or {}
        store = ledger()
        workspace = store.get_workspace(workspace_id)
        base = store.get_version(workspace.base_dataset_version_id)
        uid = base.uid_by_index().get(episode_index)
        if uid is None:
            raise ValueError("Episode does not belong to the workspace")
        ref = {"dataset_version_id": base.version_id, "episode_uid": uid}
        changes = {}
        if "decision" in body:
            decisions = [item for item in workspace.decisions if item["episode_ref"] != ref]
            if body["decision"]:
                decisions.append(
                    {
                        "episode_ref": ref,
                        "decision": body["decision"],
                        "reason": str(body.get("reason") or ""),
                    }
                )
            changes["decisions"] = decisions
        fields = dict(body.get("fields") or {})
        if "first_frame_bbox" in fields:
            box = (fields["first_frame_bbox"].get("selected") or {}).get("bbox")
            if box is not None:
                values = [float(box[key]) for key in ("left", "top", "right", "bottom")]
                if (
                    any(not math.isfinite(value) or value < 0 for value in values)
                    or values[2] <= values[0]
                    or values[3] <= values[1]
                ):
                    raise ValueError("Bounding box requires finite, ordered positive coordinates")
        if "tags" in fields:
            from lerobot.data_platform.precompute.tagging.schema import normalize_tag_values

            fields["tags"] = normalize_tag_values(fields["tags"])
        if "task" in fields and not str(fields["task"]).strip():
            raise ValueError("Task text cannot be empty")
        if "subtask_transitions" in fields:
            transitions = fields["subtask_transitions"]
            if not isinstance(transitions, list) or not transitions:
                raise ValueError("Add at least one stage transition")
            snapshot = store.curation_snapshot(base.version_id)
            info = store.version_info(base)
            episode = next(
                (
                    item
                    for item in (snapshot or {}).get("episodes", [])
                    if int(item["episode_index"]) == episode_index
                ),
                None,
            )
            duration = float(episode["length"]) / float(info["fps"]) if episode else float("inf")
            times = [float(item["time"]) for item in transitions]
            if (
                times != sorted(set(times))
                or any(not 0 <= value <= duration for value in times)
                or any(type(item["state"]) is not int or item["state"] < 0 for item in transitions)
            ):
                raise ValueError("Stage transitions require ordered, unique times and non-negative states")
        clear_fields = body.get("clear_fields", [])
        if not isinstance(clear_fields, list) or any(
            name not in {"task", "tags", "first_frame_bbox", "subtask_transitions"} for name in clear_fields
        ):
            raise ValueError("Unknown annotation field to clear")
        if fields or clear_fields:
            patches = list(workspace.annotation_patches)
            current = next((item for item in patches if item["episode_ref"] == ref), None)
            if current:
                current["fields"] = {
                    key: value
                    for key, value in {**current["fields"], **fields}.items()
                    if key not in clear_fields
                }
                if not current["fields"]:
                    patches.remove(current)
            else:
                if fields:
                    patches.append({"episode_ref": ref, "fields": fields})
            changes["annotation_patches"] = patches
        if "repair" in body:
            repair = body["repair"]
            repairs = [item for item in workspace.repair_recipes if ref not in item["episode_refs"]]
            if repair:
                repairs.append({"op": repair["op"], "episode_refs": [ref], "params": repair["params"]})
            changes["repair_recipes"] = repairs
        updated = store.update_workspace(
            workspace_id, expected_revision=int(body.get("expected_revision", -1)), changes=changes
        )
        return jsonify(workspace=updated.to_dict())

    @app.post("/api/curation/drafts/<workspace_id>/import-sidecars")
    @checked
    def curation_import_sidecars(workspace_id):
        from lerobot.data_platform.lifecycle import collect_curation_sidecars

        body = request.get_json(silent=True) or {}
        target, location, _, _, _ = resolve(body)
        store = ledger()
        workspace = store.get_workspace(workspace_id)
        if workspace.base_dataset_version_id != target.dataset_version_id:
            raise ValueError("Workspace belongs to a different dataset version")
        if target.location_id:
            cached = (location.get("metadata") or {}).get("cache_root")
            if not cached:
                raise ValueError("Prepare the Viewer cache before importing legacy sidecars")
            static = Path(cached) / "static"
        else:
            static = ctx.static_dir_for_key(ctx.repo_key(target.dataset_key))
        if static is None or not Path(static).is_dir():
            raise ValueError("Legacy sidecars are unavailable")
        patches, evidence = collect_curation_sidecars(
            Path(static), store.get_version(target.dataset_version_id)
        )
        merged = {item["episode_ref"]["episode_uid"]: item for item in workspace.annotation_patches}
        for patch in patches:
            key = patch["episode_ref"]["episode_uid"]
            previous = merged.get(key)
            if previous:
                for field, value in patch["fields"].items():
                    if field in previous["fields"] and value != previous["fields"][field]:
                        raise ValueError("Sidecar import conflicts with an existing review")
                patch = {**patch, "fields": {**previous["fields"], **patch["fields"]}}
            merged[key] = patch
        provenance = {"kind": "legacy_sidecar_import", "digest": content_digest([patches, evidence])}
        items = list(workspace.evidence)
        for item in [*evidence, provenance]:
            if item not in items:
                items.append(item)
        updated = store.update_workspace(
            workspace_id,
            expected_revision=int(body.get("expected_revision", -1)),
            changes={"annotation_patches": list(merged.values()), "evidence": items},
        )
        return jsonify(workspace=updated.to_dict())

    @app.get("/api/curation/runs/<run>")
    @checked
    def curation_result(run):
        from lerobot.data_platform.precompute.labeling.review import load_labels_jsonl, resolved_labels_path
        from lerobot.data_platform.precompute.tagging.review import current_tags

        record = result_record(run)
        variant = request.args.get("variant") or record["result"].get("output_variant")
        directory = root() / "runs" / run
        manifest = json.loads((directory / "curation-manifest.json").read_text())
        labels = load_labels_jsonl(resolved_labels_path(directory / "static" / "labeling", variant))
        tags = current_tags(directory / "static" / "tagging", variant)
        quality_path = directory / "static" / "flagged_episodes.json"
        quality = json.loads(quality_path.read_text()) if quality_path.is_file() else {}
        reasons = directory / "static" / "quality_flagged_episodes.json"
        if reasons.is_file():
            quality["flag_reasons"] = json.loads(reasons.read_text()).get("flag_reasons", {})
        from lerobot.data_platform.precompute.embedding import load_points

        snapshot = ledger().curation_snapshot(record["target"]["dataset_version_id"])
        meta = (
            SimpleNamespace(episodes={int(row["episode_index"]): row for row in snapshot["episodes"]})
            if snapshot
            else None
        )
        points = load_points(directory / "static", meta)
        return jsonify(
            **record,
            artifacts=list(manifest["files"]),
            labels=labels,
            tags=tags,
            points=points,
            quality=quality,
        )

    @app.get("/api/curation/comparison-candidates")
    @checked
    def curation_comparison_candidates():
        return jsonify(
            runs=[
                row
                for row in ledger()._list_records("curation_runs")
                if row["operation"] == "curation.compare_summary"
            ]
        )

    @app.post("/api/curation/drafts/<workspace_id>/caption-evidence")
    @checked
    def curation_caption_evidence(workspace_id):
        from lerobot.data_platform.temporal_caption import artifact_key, validate_result

        body = request.get_json(silent=True) or {}
        target, _, _, snapshot, _ = resolve(body)
        workspace = ledger().get_workspace(workspace_id)
        if workspace.base_dataset_version_id != target.dataset_version_id or snapshot is None:
            raise ValueError("Workspace belongs to a different dataset version")
        run, variant = str(body.get("run") or ""), str(body.get("variant") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run) or variant not in {
            "coarse",
            "refined",
            "caption",
            "fusion_draft",
            "fusion_reviewed",
        }:
            raise ValueError("Invalid caption result")
        caption_root = Path(
            os.environ.get("DATA_PLATFORM_TEMPORAL_CAPTION_ROOT") or ledger().root / "temporal_captions"
        )
        artifact = json.loads(
            (caption_root / artifact_key(target.dataset_key) / run / f"{variant}.json").read_text()
        )
        if artifact["dataset_key"] != target.dataset_key:
            raise ValueError("Caption belongs to a different dataset")
        validate_result(artifact["result"], artifact["frame_count"])
        version = ledger().get_version(target.dataset_version_id)
        if artifact.get("source_fingerprint") != version.fingerprint:
            raise ValueError(
                "Caption source identity is unavailable or changed; regenerate it on this dataset version"
            )
        index = int(artifact["episode_index"])
        row = next(item for item in snapshot["episodes"] if int(item["episode_index"]) == index)
        if int(row["length"]) != int(artifact["frame_count"]):
            raise ValueError("Caption frame count differs from selected version")
        evidence = {
            "kind": "temporal_caption",
            "run": run,
            "variant": variant,
            "artifact_digest": content_digest(artifact),
            "episode_ref": {
                "dataset_version_id": version.version_id,
                "episode_uid": version.uid_by_index()[index],
            },
        }
        items = list(workspace.evidence)
        if evidence not in items:
            items.append(evidence)
        updated = ledger().update_workspace(
            workspace_id,
            expected_revision=int(body.get("expected_revision", -1)),
            changes={"evidence": items},
        )
        return jsonify(workspace=updated.to_dict())

    @app.post("/api/curation/drafts/<workspace_id>/accept/<run>/<int:episode_index>")
    @checked
    def curation_accept_result(workspace_id, run, episode_index):
        from lerobot.data_platform.precompute.labeling.review import (
            first_frame_bbox_from_record,
            load_labels_jsonl,
            resolved_labels_path,
        )
        from lerobot.data_platform.precompute.tagging.review import current_tags

        body = request.get_json(silent=True) or {}
        store = ledger()
        workspace = store.get_workspace(workspace_id)
        record = result_record(run)
        if record["target"]["dataset_version_id"] != workspace.base_dataset_version_id:
            raise ValueError("Result belongs to a different dataset version")
        version = store.get_version(workspace.base_dataset_version_id)
        uid = version.uid_by_index().get(episode_index)
        if uid is None:
            raise ValueError("Unknown episode")
        static = root() / "runs" / run / "static"
        variant = body.get("variant") or record["result"].get("output_variant")
        fields = {}
        if record["operation"] == "curation.labeling":
            labels = load_labels_jsonl(resolved_labels_path(static / "labeling", variant))
            if episode_index not in labels:
                raise ValueError("No label result for this episode")
            fields["first_frame_bbox"] = first_frame_bbox_from_record(labels[episode_index])
        elif record["operation"] == "curation.tagging":
            tags = current_tags(static / "tagging", variant)
            if episode_index not in tags:
                raise ValueError("No tag result for this episode")
            fields["tags"] = tags[episode_index]["tags"]
        elif record["operation"] == "curation.stage":
            transitions = record["result"].get("transitions", {}).get(str(episode_index))
            if not transitions:
                raise ValueError("No stage transitions for this episode")
            fields["subtask_transitions"] = transitions
        ref = {"dataset_version_id": version.version_id, "episode_uid": uid}
        patches = list(workspace.annotation_patches)
        current = next((item for item in patches if item["episode_ref"] == ref), None)
        if fields:
            if current:
                current["fields"] = {**current["fields"], **fields}
            else:
                patches.append({"episode_ref": ref, "fields": fields})
        evidence = {
            "kind": record["operation"],
            "run": run,
            "episode_ref": ref,
            "variant": body.get("variant"),
        }
        items = list(workspace.evidence)
        if evidence not in items:
            items.append(evidence)
        updated = store.update_workspace(
            workspace_id,
            expected_revision=int(body.get("expected_revision", -1)),
            changes={"annotation_patches": patches, "evidence": items},
        )
        return jsonify(workspace=updated.to_dict())

    @app.get("/api/curation/runs/<run>/artifacts/<path:filename>")
    @checked
    def curation_read_artifact(run, filename):
        result_record(run)
        manifest = json.loads((root() / "runs" / run / "curation-manifest.json").read_text())
        if filename not in manifest["files"]:
            abort(404)
        path = artifact_path(root() / "runs" / run, filename)
        response = send_file(
            path, conditional=True, as_attachment=path.suffix.lower() in {".html", ".htm", ".svg", ".js"}
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/curation/compare", methods=["GET", "POST"])
    @checked
    def curation_compare():
        body = request.args.to_dict() if request.method == "GET" else request.get_json(silent=True) or {}
        records = [result_record(str(body[key])) for key in ("run_a", "run_b")]
        if any(row["operation"] != "curation.compare_summary" for row in records):
            raise ValueError("Prepare a comparison summary for both datasets")
        return jsonify(
            a=records[0],
            b=records[1],
            summary={
                side: json.loads((root() / "runs" / row["run"] / "compare.json").read_text())
                for side, row in zip(("a", "b"), records, strict=True)
            },
        )

    if ctx.control_plane_store is None:
        return
    control = ctx.control_plane_store

    def staged(job):
        attempt = request.headers.get("X-Job-Attempt", "")
        if not attempt or any(char not in "0123456789abcdef-" for char in attempt):
            abort(409)
        return root() / ".jobs" / job["job_id"] / attempt

    @app.put("/api/agents/jobs/<job_id>/curation-artifacts/<path:filename>")
    @checked
    def curation_upload(job_id, filename):
        job = control.get_job(job_id)
        if job["operation"] not in OPERATIONS or job["status"] != "running":
            abort(409)
        destination = artifact_path(staged(job), filename)
        maximum = 1024**3 if filename.startswith("static/") else 128 * 1024**2
        if request.content_length is not None and request.content_length > maximum:
            abort(413)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                count = 0
                while chunk := request.stream.read(1024 * 1024):
                    count += len(chunk)
                    if count > maximum:
                        abort(413)
                    handle.write(chunk)
            os.replace(temporary, destination)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return jsonify(uploaded=filename)

    @app.get("/api/agents/jobs/<job_id>/curation-inputs/<path:filename>")
    @checked
    def curation_input(job_id, filename):
        job = control.get_job(job_id)
        item = job["options"].get("input_files", {}).get(filename)
        if item is None or job["operation"] not in OPERATIONS or job["status"] != "running":
            abort(404)
        return send_file(artifact_path(root() / "runs" / item["run"], item["path"]))

    def complete(job, returned):
        destination = root() / "runs" / job["job_id"]
        target = job["options"]["target"]
        publish_bundle(staged(job), destination, job["operation"], target)
        result = json.loads((destination / "result.json").read_text())
        store = ledger()
        existing = store._get_record("curation_runs", job["job_id"])
        if existing:
            return existing["result"]
        location = control.get_location(job["location_id"])
        # Final output paths come from the supervisor, then are checked against the queued destination.
        if job["operation"] in DATASET_OPERATIONS:
            derived = returned.get("dataset_location") or {}
            if derived.get("root") != job["options"]["out_root"]:
                raise ValueError("Output location differs from the queued destination")
            location = {**derived, "node_id": job["node_id"], "location_id": "pending-" + job["job_id"]}
        with store.repository.transaction() as connection:
            if (destination / "snapshot.json").is_file():
                value = CurationSnapshot.from_dict(json.loads((destination / "snapshot.json").read_text()))
                if job["operation"] in DATASET_OPERATIONS and value.version["parent_version_ids"] != [
                    target["dataset_version_id"]
                ]:
                    raise ValueError("Output lineage does not match the selected source")
                version = register_snapshot(store, value, location, connection=connection)
                result["dataset_version_id"] = version.version_id
                if not target.get("location_id") and job["operation"] == "curation.snapshot":
                    store._put_record(
                        "curation_local_heads",
                        target["dataset_key"],
                        {"version_id": version.version_id},
                        connection=connection,
                    )
            if job["operation"] == "curation.materialize":
                report = result["materialization"]
                report.update(
                    materialization_id="mat_" + job["job_id"],
                    output_root=location["root"],
                    staging_root="",
                    status=MATERIALIZATION_COMMITTED,
                    output_dataset_version_id=result["dataset_version_id"],
                )
                run = MaterializationRun.from_dict(report)
                store._put_record(
                    "materializations",
                    run.materialization_id,
                    run.to_dict(),
                    immutable=True,
                    connection=connection,
                )
                result["materialization_id"] = run.materialization_id
            if job["operation"] in DATASET_OPERATIONS:
                result["dataset_location"] = returned["dataset_location"]
                store._put_record(
                    "curation_completions",
                    job["job_id"],
                    {
                        "job_id": job["job_id"],
                        "dataset_location": result["dataset_location"],
                        "dataset_version_id": result["dataset_version_id"],
                        "node_id": job["node_id"],
                    },
                    connection=connection,
                )
        publication = job["options"].get("publish_workspace")
        if publication:
            actor = next(
                (user for user in control.list_users() if user["user_id"] == job["requested_by"]), None
            )
            if (
                not actor
                or not actor.get("active")
                or actor["role"] not in {"operator", "data_manager", "admin"}
            ):
                raise PermissionError("Publication owner is no longer an active operator")
            workspace = store.get_workspace(publication["workspace_id"])
            if workspace.published_manifest_id:
                manifest = store.get_manifest(workspace.published_manifest_id)
            else:
                manifest = store.publish_workspace(
                    workspace.workspace_id,
                    expected_revision=publication["revision"],
                    reviewer=actor["username"],
                    reason=publication["reason"],
                    source_fingerprint=result["fingerprint"],
                )
            result["manifest_id"] = manifest.manifest_id
        if job["operation"] in DATASET_OPERATIONS:
            synced = control.sync_locations(job["node_id"], [result["dataset_location"]])[0]
            value = CurationSnapshot.from_dict(json.loads((destination / "snapshot.json").read_text()))
            register_snapshot(store, value, synced)
            result["output_location_id"] = synced["location_id"]
            result["viewer_url"] = (
                f"/?remote_location_id={synced['location_id']}&page=preprocessing&tab=cache"
            )
            if not target.get("location_id"):
                complete_local = app.extensions.get("data_platform_complete_local")
                if complete_local:
                    complete_local(
                        {
                            "local_registrations": [
                                {
                                    "root": location["root"],
                                    "repo_id": location["dataset_key"],
                                    "output_dir": location["output_dir"],
                                }
                            ]
                        }
                    )
        result["review_url"] = "/curation?" + (
            f"location_id={target['location_id']}"
            if target.get("location_id")
            else "dataset_key=" + target["dataset_key"]
        )
        record = {
            "run": job["job_id"],
            "target": target,
            "operation": job["operation"],
            "result": result,
            "created_at": job["created_at"],
        }
        store._put_record("curation_runs", job["job_id"], record, immutable=True)
        return result

    app.extensions["data_platform_complete_curation"] = complete
