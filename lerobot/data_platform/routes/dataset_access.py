"""Enforce viewer/operator dataset scopes at authenticated request boundaries."""

from flask import jsonify, request

from lerobot.data_platform.dataset_references import dataset_references


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
        found_keys, found_locations = dataset_references(value)
        keys.update(found_keys)
        locations.update(found_locations)

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
