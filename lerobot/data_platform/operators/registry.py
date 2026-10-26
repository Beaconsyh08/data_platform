"""One allowlisted registry for legacy computations and new operators."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import threading

from .contracts import ExecutionContext, OperatorSpec

_SPECS: dict[str, OperatorSpec] = {}
_VERSIONS: dict[tuple[str, str], OperatorSpec] = {}
_LOADED = False
_LOCK = threading.RLock()


def register_operator(spec: OperatorSpec) -> OperatorSpec:
    _load()
    if (spec.operator_id, spec.version) in _VERSIONS:
        raise ValueError(f"Duplicate operator: {spec.operator_id}@{spec.version}")
    if not spec.operator_id or not spec.version or spec.effect not in {"read", "cache", "dataset", "source"}:
        raise ValueError("Invalid operator description")
    _SPECS[spec.operator_id] = spec
    _VERSIONS[(spec.operator_id, spec.version)] = spec
    return spec


def _load() -> None:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        from .builtins import builtins

        for spec in builtins():
            if spec.operator_id in _SPECS:
                raise ValueError(f"Duplicate operator: {spec.operator_id}")
            _SPECS[spec.operator_id] = spec
            _VERSIONS[(spec.operator_id, spec.version)] = spec
        _LOADED = True


def get_operator(operator_id: str, version: str | None = None) -> OperatorSpec:
    _load()
    if operator_id not in _SPECS:
        raise ValueError(f"Unknown operator: {operator_id}")
    spec = _SPECS[operator_id]
    if version and version != spec.version:
        if (operator_id, version) not in _VERSIONS:
            raise ValueError(f"Operator version unavailable: {operator_id}@{version}")
        return _VERSIONS[(operator_id, version)]
    return spec


def list_operators() -> list[OperatorSpec]:
    _load()
    return [_SPECS[key] for key in sorted(_SPECS)]


def execute_operator(
    operator_id: str, context: ExecutionContext, parameters: dict | None = None, *, version=None
):
    spec = get_operator(operator_id, version)
    if context.check_cancelled:
        context.check_cancelled()
    handler = context.handlers.get(spec.operator_id) or context.handlers.get(spec.family)
    if handler:
        return handler()
    if not spec.entry_point:
        raise ValueError(f"{operator_id} requires its registered execution adapter")
    module, name = spec.entry_point.split(":")
    function = getattr(importlib.import_module(module), name)
    signature = inspect.signature(function)
    if "context" in signature.parameters:
        return function(context, dict(parameters or {}))
    kwargs = dict(parameters or {})
    if isinstance(kwargs.get("episode_range"), list):
        kwargs["episode_range"] = tuple(kwargs["episode_range"])
    supplied = {
        "src_root": context.source_root,
        "root": context.source_root,
        "base_root": context.source_root,
        "src_roots": list(context.source_roots),
        "roots": list(context.source_roots),
        "subtract_roots": list(context.source_roots[1:]),
        "out_root": context.output_root,
        "output_dir": context.output_root,
        "repo_id": context.dataset_key,
        "progress_callback": context.emit,
    }
    for key, value in supplied.items():
        if key in signature.parameters and key not in kwargs:
            kwargs[key] = value
    return function(**kwargs)


def operation_ids(
    *,
    family: str | None = None,
    remote: bool | None = None,
    parallel: bool | None = None,
    effect: str | None = None,
    cancellable: bool | None = None,
) -> set[str]:
    return {
        spec.operator_id
        for spec in list_operators()
        if (family is None or spec.family == family)
        and (remote is None or spec.remote == remote)
        and (parallel is None or spec.parallel == parallel)
        and (effect is None or spec.effect == effect)
        and (cancellable is None or spec.cancellable == cancellable)
    }


def worker_manifest(operations=None) -> dict:
    versions, unavailable = {}, {}
    installed = set(operations) if operations is not None else operation_ids(remote=True)
    for spec in list_operators():
        if not spec.remote:
            continue
        missing = [name for name in spec.required_modules if importlib.util.find_spec(name) is None]
        if missing:
            unavailable[spec.operator_id] = "Missing dependencies: " + ", ".join(missing)
        elif spec.operator_id not in installed:
            unavailable[spec.operator_id] = "Execution adapter, model backend or permission is unavailable"
        else:
            versions[spec.operator_id] = spec.version
    history = {
        key: sorted({version for operator_id, version in _VERSIONS if operator_id == key}) for key in versions
    }
    return {
        "operator_protocol": 1,
        "operator_versions": versions,
        "operator_version_history": history,
        "operator_unavailable": unavailable,
    }
