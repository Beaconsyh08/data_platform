from pathlib import Path

from lerobot.data_platform.precompute.data_profile import require_dataset_operation

try:
    import pandas  # noqa: F401
    import pyarrow  # noqa: F401

    _AVAILABLE = True
    _ERROR = None
except Exception as exc:  # pragma: no cover - depends on optional runtime env
    _AVAILABLE = False
    _ERROR = str(exc)

from lerobot.data_platform.precompute.preprocess.common import PreprocessResult, ProgressCallback


def get_capabilities() -> dict:
    from lerobot.data_platform.operators import list_operators

    return {
        "available": _AVAILABLE,
        "error": _ERROR,
        "ops": [
            {
                "name": spec.operator_id.removeprefix("preprocess."),
                "available": _AVAILABLE,
                "description": spec.description,
            }
            for spec in list_operators()
            if spec.family == "preprocess"
        ],
    }


def run_preprocess_op(
    op: str,
    src_root: Path | None = None,
    out_root: Path | None = None,
    progress_callback: ProgressCallback = None,
    operator_version: str | None = None,
    **kwargs,
) -> PreprocessResult:
    if not _AVAILABLE:
        raise RuntimeError(f"Preprocess dependencies are unavailable: {_ERROR}")
    if src_root is not None:
        require_dataset_operation(src_root, op, data_version_override=kwargs.get("data_version"))
    from lerobot.data_platform.operators import ExecutionContext
    from lerobot.data_platform.operators.registry import execute_operator

    return execute_operator(
        f"preprocess.{op}",
        ExecutionContext(
            source_root=Path(src_root) if src_root is not None else Path("."),
            output_root=out_root,
            source_roots=tuple(Path(item) for item in kwargs.get("src_roots", [])),
            progress=progress_callback,
        ),
        kwargs,
        version=operator_version,
    )
