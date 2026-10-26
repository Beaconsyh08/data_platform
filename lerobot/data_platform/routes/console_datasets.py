# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dataset registration and browsing compatibility endpoints."""

import logging
from pathlib import Path

from flask import (
    g,
    jsonify,
    request,
)

from lerobot.data_platform.cli import get_default_output_dir
from lerobot.data_platform.data_protection import (
    evaluate_dataset_protection,
    normalized_path,
)
from lerobot.data_platform.services.dataset_paths import (
    _is_dataset_root,
)


def register_console_dataset_routes(app, ctx):
    catalog = ctx.dataset_catalog
    presentation = ctx.dataset_presentation

    @app.route("/api/datasets")
    def api_datasets():
        return jsonify(
            {
                "datasets": [
                    presentation.serialize_dataset_light(key)
                    for key in catalog.visible_dataset_keys(getattr(g, "control_plane_user", None))
                ]
            }
        )

    @app.route("/api/datasets/<string:dataset_namespace>/<string:dataset_name>")
    def api_dataset_detail(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        if request.args.get("full") not in {"1", "true", "yes"}:
            if dataset_key not in catalog.state.datasets_index:
                return jsonify(
                    {"error": f"dataset is not registered: {ctx.repo_id_from_key(dataset_key)}"}
                ), 404
            return jsonify({"dataset": presentation.serialize_dataset_fast_detail(dataset_key)})
        if dataset_key in catalog.state.datasets_index:
            entry = catalog.state.datasets_index[dataset_key]
            root_path = Path(entry["root"]).expanduser()
            if not _is_dataset_root(root_path):
                return jsonify({"dataset": presentation.serialize_dataset_fast_detail(dataset_key)})
        try:
            catalog.ensure_dataset_loaded(dataset_key)
        except KeyError:
            return jsonify({"error": f"dataset is not registered: {ctx.repo_id_from_key(dataset_key)}"}), 404
        except Exception as exc:
            logging.exception("Failed to load dataset %s", ctx.repo_id_from_key(dataset_key))
            return jsonify({"error": str(exc)}), 400
        return jsonify({"dataset": presentation.serialize_dataset(dataset_key)})

    @app.route("/api/datasets/<string:dataset_namespace>/<string:dataset_name>", methods=["DELETE"])
    def api_delete_dataset(dataset_namespace, dataset_name):
        if not ctx.admin_authenticated():
            return jsonify({"error": "administrator access is required to unregister datasets"}), 403
        dataset_key = (dataset_namespace, dataset_name)
        if (
            dataset_key not in catalog.state.datasets_index
            and dataset_key not in catalog.state.datasets_registry
        ):
            return jsonify({"error": f"dataset is not registered: {ctx.repo_id_from_key(dataset_key)}"}), 404

        catalog.unregister_dataset(dataset_key)
        return jsonify({"status": "ok", "dataset_key": ctx.repo_id_from_key(dataset_key)})

    @app.route(
        "/api/datasets/<string:dataset_namespace>/<string:dataset_name>/protection",
        methods=["PATCH"],
    )
    def api_dataset_protection(dataset_namespace, dataset_name):
        dataset_key = (dataset_namespace, dataset_name)
        entry = catalog.state.datasets_index.get(dataset_key)
        if entry is None:
            return jsonify({"error": f"dataset is not registered: {ctx.repo_id_from_key(dataset_key)}"}), 404
        body = request.get_json(silent=True) or {}
        if "protected" not in body:
            return jsonify({"error": "protected is required"}), 400
        manual_source = ctx.bool_option(body, "protected", False)
        entry["manual_source"] = manual_source
        entry["manual_source_reason"] = (
            str(body.get("reason") or "Manual source protection") if manual_source else ""
        )
        catalog.save_registry()
        protection = catalog.dataset_protection_for_key(dataset_key)
        ds_static = catalog.static_dir_for_key(dataset_key)
        if ds_static is not None:
            ctx.append_operation_log(
                ds_static,
                "dataset_protection_update",
                dataset_key=dataset_key,
                dataset_root=Path(entry["root"]),
                details={"protection": protection},
            )
        return jsonify({"dataset": presentation.serialize_dataset_fast_detail(dataset_key)})

    @app.route("/api/source_roots", methods=["GET", "PATCH"])
    def api_source_roots():
        if request.method == "GET":
            return jsonify({"source_roots": [str(path) for path in catalog.protected_source_roots()]})
        body = request.get_json(silent=True) or {}
        root_value = str(body.get("root") or "").strip()
        if not root_value:
            return jsonify({"error": "root is required"}), 400
        root_path = normalized_path(root_value)
        if not root_path.is_dir():
            return jsonify({"error": f"source root does not exist: {root_path}"}), 400
        protected = ctx.bool_option(body, "protected", True)
        value = str(root_path)
        if protected:
            catalog.state.registry_state["protected_source_roots"].add(value)
        else:
            if value in catalog.state.registry_state["configured_source_roots"]:
                return jsonify(
                    {"error": "source root is configured by the CLI and requires a restart to remove"}
                ), 409
            catalog.state.registry_state["protected_source_roots"].discard(value)
        catalog.save_registry()
        return jsonify(
            {
                "source_roots": [str(path) for path in catalog.protected_source_roots()],
                "datasets": [
                    presentation.serialize_dataset_light(key)
                    for key in catalog.visible_dataset_keys(getattr(g, "control_plane_user", None))
                ],
            }
        )

    @app.route("/api/dataset_roots")
    def api_dataset_roots():
        scan = request.args.get("scan", "1").lower()
        if scan not in {"0", "1", "false", "true", "no", "yes"}:
            return jsonify({"error": "scan must be 0 or 1"}), 400
        discover = scan in {"1", "true", "yes"}
        root_value = request.args.get("root_dir")
        root_dir = (
            Path(root_value).expanduser() if root_value else catalog.state.registry_state["datasets_root"]
        )
        if root_dir is not None and root_dir.exists():
            catalog.set_registry_root(
                root_dir,
                load=discover
                or catalog.registry_path_for_root(root_dir) != catalog.state.registry_state["path"],
            )
        return jsonify(
            {
                "root_dir": str(root_dir) if root_dir else "",
                "candidates": catalog.dataset_candidates(root_dir, discover=discover),
                "registry_path": str(catalog.state.registry_state["path"]),
                "source_roots": [str(path) for path in catalog.protected_source_roots()],
                "root_protected": bool(
                    root_dir
                    and evaluate_dataset_protection(
                        root_dir,
                        source_roots=catalog.protected_source_roots(),
                    ).protected
                ),
            }
        )

    @app.route("/api/datasets/register", methods=["POST"])
    def api_register_dataset():
        body = request.get_json(silent=True) or {}
        roots_value = body.get("roots")
        if roots_value is None:
            root_value = str(body.get("root", "")).strip()
            roots = [root_value] if root_value else []
        elif isinstance(roots_value, list):
            roots = [str(value).strip() for value in roots_value if str(value).strip()]
        else:
            roots = [str(roots_value).strip()] if str(roots_value).strip() else []
        if not roots:
            return jsonify({"error": "root or roots is required"}), 400

        registered = []
        errors = []
        for idx, root_value in enumerate(roots):
            root_path = Path(root_value).expanduser()
            try:
                output_dir = Path(
                    str(body.get("output_dir") or "").strip() or get_default_output_dir(root_path)
                ).expanduser()
                dataset_key = catalog.register_path(
                    root_path,
                    repo_id=str(body.get("repo_id") or "").strip() if len(roots) == 1 else "",
                    output_dir=str(output_dir),
                )
                registered.append(presentation.serialize_dataset_light(dataset_key))
                ctx.append_operation_log(
                    output_dir / "static",
                    "dataset_register",
                    dataset_key=dataset_key,
                    dataset_root=root_path,
                    details={"root": str(root_path), "batch_index": idx, "batch_size": len(roots)},
                )
            except Exception as exc:
                logging.exception("Failed to register dataset %s", root_path)
                errors.append({"root": str(root_path), "error": str(exc)})

        if registered:
            catalog.save_registry()
        if not registered:
            return jsonify(
                {"error": errors[0]["error"] if errors else "no dataset registered", "errors": errors}
            ), 400

        payload = {"datasets": registered, "errors": errors}
        if len(registered) == 1:
            payload["dataset"] = registered[0]
        return jsonify(payload)
