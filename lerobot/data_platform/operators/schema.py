"""Read parameter metadata without importing computation or model dependencies."""

from __future__ import annotations

import ast
import copy
import json
import math
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

from .contracts import OperatorSpec

_INTERNAL = {
    "src_root",
    "src_roots",
    "subtract_roots",
    "src_static_dir",
    "source_root",
    "source_roots",
    "roots",
    "root",
    "base_root",
    "out_root",
    "output_dir",
    "repo_id",
    "progress_callback",
    "meta",
    "dataset",
    "static_dir",
    "in_place",
    "overwrite",
    "overwrite_output",
    "input_root",
    "state",
    "client",
    "src_static_dirs",
    "out_static_dir",
}


def _type(annotation: ast.AST | None) -> dict:
    if annotation is None:
        return {}
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left, right = _type(annotation.left), _type(annotation.right)
        if not left or not right:
            return {}
        kinds = []
        for value in (left, right):
            types = value["type"] if isinstance(value["type"], list) else [value["type"]]
            kinds.extend(item for item in types if item not in kinds)
        return {"type": kinds}
    text = ast.unparse(annotation)
    if text == "None":
        return {"type": "null"}
    if isinstance(annotation, ast.Subscript):
        if ast.unparse(annotation.value) in {"list", "tuple", "Sequence"}:
            return {"type": "array"}
        if ast.unparse(annotation.value) == "dict":
            return {"type": "object"}
    return (
        {
            "type": {
                "bool": "boolean",
                "int": "integer",
                "float": "number",
                "dict": "object",
                "str": "string",
                "Path": "string",
            }[text]
        }
        if text in {"bool", "int", "float", "dict", "str", "Path"}
        else {}
    )


@lru_cache(maxsize=256)
def parameter_schema(spec: OperatorSpec) -> dict:
    if spec.parameter_schema_override is not None:
        return copy.deepcopy(spec.parameter_schema_override)
    properties, required = {}, []
    if spec.entry_point:
        module, name = spec.entry_point.split(":")
        path = Path(__file__).parents[3] / Path(*module.split(".")).with_suffix(".py")
        if path.is_file():
            tree = ast.parse(path.read_text())
            function = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)
            if function:
                args = [*function.args.posonlyargs, *function.args.args]
                defaults = [None] * (len(args) - len(function.args.defaults)) + function.args.defaults
                for arg, default in zip(
                    [*args, *function.args.kwonlyargs], [*defaults, *function.args.kw_defaults], strict=True
                ):
                    if arg.arg in _INTERNAL or arg.arg.startswith("_"):
                        continue
                    value = _type(arg.annotation)
                    if default is None:
                        required.append(arg.arg)
                    else:
                        with suppress(ValueError, TypeError):
                            value["default"] = json.loads(json.dumps(ast.literal_eval(default)))
                    properties[arg.arg] = value
    for name in spec.parameter_names:
        properties.setdefault(name, {})
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _validate(value, schema, path):
    kinds = {
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "object": dict,
        "array": list,
        "string": str,
        "null": type(None),
    }
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if types and not any(
        isinstance(value, kinds[kind]) and not (kind in {"integer", "number"} and isinstance(value, bool))
        for kind in types
    ):
        raise ValueError(f"Invalid type for {path}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Invalid value for {path}")
    if type(value) in (int, float) and (
        "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]
    ):
        raise ValueError(f"{path} is outside the allowed range")
    if isinstance(value, str) and len(value) > schema.get("maxLength", 65536):
        raise ValueError(f"{path} is too long")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise ValueError(f"Invalid number of items for {path}")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError(
                f"Unsupported parameters for {path}: {', '.join(sorted(set(value) - set(props)))}"
            )
        if set(schema.get("required", [])) - set(value):
            raise ValueError(
                f"Missing parameters for {path}: {', '.join(sorted(set(schema['required']) - set(value)))}"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} requires string keys")
            _validate(
                item,
                props.get(
                    key,
                    schema.get("additionalProperties")
                    if isinstance(schema.get("additionalProperties"), dict)
                    else {},
                ),
                f"{path}.{key}",
            )


def validate_parameters(spec: OperatorSpec, parameters: dict) -> dict:
    if not isinstance(parameters, dict):
        raise ValueError("Operator parameters must be an object")
    schema = parameter_schema(spec)
    _validate(parameters, schema, spec.operator_id)
    result = copy.deepcopy(parameters)
    for name, prop in schema.get("properties", {}).items():
        if name not in result and "default" in prop:
            result[name] = copy.deepcopy(prop["default"])
    _validate(result, schema, spec.operator_id)
    return result
