"""Dataset identifiers in legacy and current task payloads, shared by read authorization."""

KEY_FIELDS = {"dataset_key", "repo_id", "base_key", "dataset_key_a", "dataset_key_b", "compare_with"}
KEY_LIST_FIELDS = {"dataset_keys", "src_keys", "repo_ids"}
LOCATION_FIELDS = {"location_id", "source_location_id"}
LOCATION_LIST_FIELDS = {"location_ids", "source_location_ids"}


def dataset_references(value) -> tuple[set[str], set[str]]:
    keys, locations = set(), set()

    def collect(item):
        if isinstance(item, dict):
            namespace = item.get("dataset_namespace") or item.get("ns")
            name = item.get("dataset_name") or item.get("name")
            if namespace and name:
                keys.add(f"{namespace}/{name}")
            for field, child in item.items():
                if field in KEY_FIELDS and isinstance(child, str) and child:
                    keys.add(child)
                elif field in LOCATION_FIELDS and isinstance(child, str) and child:
                    locations.add(child)
                elif field in KEY_LIST_FIELDS | LOCATION_LIST_FIELDS:
                    values = child.replace("\n", ",").split(",") if isinstance(child, str) else child
                    if isinstance(values, list):
                        target = keys if field in KEY_LIST_FIELDS else locations
                        target.update(v.strip() for v in values if isinstance(v, str) and v.strip())
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return keys, locations
