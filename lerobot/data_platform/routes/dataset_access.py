"""Enforce viewer/operator dataset scopes at authenticated request boundaries."""

from flask import jsonify, request

_KEY_FIELDS = {"dataset_key", "repo_id", "base_key", "dataset_key_a", "dataset_key_b", "compare_with"}
_KEY_LIST_FIELDS = {"dataset_keys", "src_keys", "repo_ids"}
_LOCATION_FIELDS = {"location_id", "source_location_id"}
_LOCATION_LIST_FIELDS = {"location_ids", "source_location_ids"}


def check_dataset_access(store, user):
    allowed_keys = store.restricted_dataset_keys(user["user_id"])
    if allowed_keys is None:
        return None
    allowed_locations = {item["location_id"] for item in store.accessible_locations(user["user_id"])}
    # Restricted users cannot discover or register arbitrary server filesystem datasets.
    if request.path in {
        "/api/source_roots",
        "/api/dataset_roots",
        "/api/datasets/register",
        "/api/operation_log",
    }:
        return jsonify(error="This endpoint requires unrestricted dataset access"), 403
    # Lifecycle versions/workspaces are not registered locations and have no location ACL mapping.
    if request.path.startswith(("/api/lifecycle/", "/api/curation/")):
        return jsonify(error="Lifecycle and curation endpoints require unrestricted dataset access"), 403
    keys, locations = set(), set()

    def collect(value):
        if isinstance(value, dict):
            namespace = value.get("dataset_namespace") or value.get("ns")
            name = value.get("dataset_name") or value.get("name")
            if namespace and name:
                keys.add(f"{namespace}/{name}")
            for field, item in value.items():
                if field in _KEY_FIELDS and isinstance(item, str) and item:
                    keys.add(item)
                elif field in _LOCATION_FIELDS and isinstance(item, str) and item:
                    locations.add(item)
                elif field in _KEY_LIST_FIELDS | _LOCATION_LIST_FIELDS:
                    values = item.replace("\n", ",").split(",") if isinstance(item, str) else item
                    if isinstance(values, list):
                        target = keys if field in _KEY_LIST_FIELDS else locations
                        target.update(v.strip() for v in values if isinstance(v, str) and v.strip())
                if isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(request.view_args or {})
    collect(request.args.to_dict())
    collect(request.get_json(silent=True) or {})
    collect(request.form.to_dict())
    job_id = (request.view_args or {}).get("job_id")
    if job_id:
        try:
            job = store.get_job(job_id)
        except KeyError:
            pass
        else:
            locations.add(job["location_id"])
            collect(job.get("options") or {})
    if not keys.issubset(allowed_keys) or not locations.issubset(allowed_locations):
        return jsonify(error="Dataset access is outside this account's data permissions"), 403
    return None
