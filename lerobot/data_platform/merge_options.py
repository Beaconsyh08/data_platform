"""Dependency-free validation for merge options accepted by the control plane."""

import math

SIGNAL_FIELDS = ("action", "state", "observation.state")
DIMENSION_POLICIES = ("strict", "min", "pad")


def validate_alignment_options(policy, dimension_names, padding_value, source_count):
    if policy not in DIMENSION_POLICIES:
        raise ValueError(f"dimension_policy must be one of {DIMENSION_POLICIES}")
    if (
        isinstance(padding_value, bool)
        or not isinstance(padding_value, (int, float))
        or not math.isfinite(padding_value)
    ):
        raise ValueError("padding_value must be a finite number")
    if policy != "pad" and padding_value != 0:
        raise ValueError("padding_value is only supported for padding merge")
    if dimension_names is not None:
        if policy == "strict":
            raise ValueError("Explicit dimension mappings require minimum or padding merge")
        if not isinstance(dimension_names, list) or len(dimension_names) != source_count:
            raise ValueError("dimension_names must have one object per source, in source order")
        for mapping in dimension_names:
            if not isinstance(mapping, dict) or set(mapping) - set(SIGNAL_FIELDS):
                raise ValueError("dimension_names may only map action, state and observation.state")
            for names in mapping.values():
                if (
                    not isinstance(names, list)
                    or not names
                    or any(not isinstance(n, str) or not n.strip() for n in names)
                    or len(set(names)) != len(names)
                ):
                    raise ValueError("Explicit dimension names must be complete, nonempty and unique")
