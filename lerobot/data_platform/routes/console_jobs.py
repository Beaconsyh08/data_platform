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

"""Compatibility routes for the original console computation endpoints."""

from flask import jsonify, request, url_for

from lerobot.data_platform.services.dataset_paths import _normalize_data_version


def register_console_job_routes(app, ctx):
    service = ctx.console_submissions

    @app.route("/api/precompute/start", methods=["POST"])
    def api_start_precompute():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="request body must be an object"), 400
        result = service.start_precompute(body)
        return jsonify(result.payload), result.status

    @app.route("/api/labeling/start", methods=["POST"])
    def api_start_labeling():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="request body must be an object"), 400
        result = service.start_labeling(body)
        return jsonify(result.payload), result.status

    @app.route("/api/viewer/preload", methods=["POST"])
    def api_viewer_preload():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="request body must be an object"), 400
        key = ctx.repo_key(str(body.get("dataset_key") or "").strip())
        target_url = url_for(
            "show_episode",
            dataset_namespace=key[0],
            dataset_name=key[1],
            episode_id=int(body.get("episode_id") or 0),
            data_version=_normalize_data_version(body.get("data_version")),
            direct=1,
        )
        result = service.preload_viewer(body, target_url)
        return jsonify(result.payload), result.status

    @app.route("/api/preprocess/delete_episodes/start", methods=["POST"])
    def api_preprocess_delete_episodes_start():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="request body must be an object"), 400
        result = service.delete_episodes(body)
        return jsonify(result.payload), result.status
