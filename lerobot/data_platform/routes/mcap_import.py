"""MCAP import submission; filesystem checks and conversion run on the owning Agent."""

from __future__ import annotations

from flask import jsonify, request

from lerobot.data_platform.mcap_contract import McapImportConfig


def register_mcap_import_routes(app, store):
    from lerobot.data_platform.job_management import JobConflictError
    from lerobot.data_platform.routes.control_plane import (
        _current_user,
        _role_denied,
        _validate_remote_output_path,
    )

    @app.post("/api/control/locations/<string:location_id>/mcap-jobs")
    def control_create_mcap_job(location_id):
        denied = _role_denied("admin", "data_manager", "operator")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or set(body) - {"operation", "config", "out_root", "inspection_job_id"}:
            return jsonify(error="Invalid MCAP import request"), 400
        try:
            config = McapImportConfig.from_dict(body.get("config") or {})
            operation = body.get("operation", "inspect")
            if operation not in {"inspect", "convert"}:
                raise ValueError("operation must be inspect or convert")
            location = store.get_location(location_id)
            if location.get("metadata", {}).get("source_format") != "mcap":
                raise ValueError("Select a registered raw MCAP directory")
            node = next(row for row in store.list_nodes() if row["node_id"] == location["node_id"])
            capabilities = node.get("capabilities") or {}
            if node["status"] != "online":
                raise JobConflictError("Agent is offline")
            if capabilities.get("job_protocol", 1) < 2:
                raise JobConflictError("MCAP imports require the durable job protocol")
            if capabilities.get("mcap_import_protocol") != 1 or not capabilities.get("mcap_import", {}).get(
                "ready"
            ):
                raise JobConflictError(
                    capabilities.get("mcap_import", {}).get("reason") or "Upgrade the MCAP Agent runtime"
                )
            if f"import.mcap.{operation}" not in capabilities.get("operations", []):
                raise JobConflictError("Agent does not support this import operation")
            options = {"config": config.to_dict()}
            if operation == "convert":
                probe = store.get_job(str(body.get("inspection_job_id") or ""))
                if (
                    probe["location_id"] != location_id
                    or probe["operation"] != "import.mcap.inspect"
                    or probe["status"] != "done"
                    or probe["requested_by"] != _current_user()["user_id"]
                ):
                    raise JobConflictError("Complete an inspection of this source first")
                report = (probe.get("result") or {}).get("mcap_import") or {}
                if report.get("config_digest") != config.digest or not report.get("files"):
                    raise JobConflictError("Settings changed; inspect again before converting")
                options.update(
                    out_root=_validate_remote_output_path(store, location_id, body.get("out_root")),
                    input_fingerprint=report["input_fingerprint"],
                    inspection_job_id=probe["job_id"],
                )
            job = store.create_job(
                location_id=location_id,
                requested_by=_current_user()["user_id"],
                operation=f"import.mcap.{operation}",
                options=options,
            )
            return jsonify(job=job), 202
        except KeyError:
            return jsonify(error="Source or inspection job not found"), 404
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except JobConflictError as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
