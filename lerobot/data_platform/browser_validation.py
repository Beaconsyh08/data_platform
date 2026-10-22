"""Explicit browser release gate; a skipped test is not acceptance evidence."""

import hashlib
import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree

BROWSER_TESTS = (
    "tests/datasets/test_ai_assistant_browser.py",
    "tests/datasets/test_ai_workspace_browser.py",
    "tests/datasets/test_console_browser.py",
    "tests/datasets/test_processing_browser.py",
)
REQUIRED_CASES = {
    "test_browser_memory_edit_delete_and_durable_readonly_updates",
    "test_async_assistant_browser_restores_pending_and_requires_confirmation",
    "test_qwen_browser_draft_restore_scope_and_explicit_run",
    "test_console_browse_forms_pipeline_switching_and_viewer_role",
    "test_pipeline_browser_edit_run_restore_review_and_role",
}


def check_browser_report(path: Path) -> dict:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise RuntimeError("Browser acceptance report is missing or invalid") from exc
    cases = list(root.iter("testcase"))
    names = {case.get("name") for case in cases}
    if not names >= REQUIRED_CASES or any(
        case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")
    ):
        raise RuntimeError(
            "Every required browser scenario must execute and pass; skipping is not acceptance"
        )
    return {"passed": len(cases), "required": sorted(REQUIRED_CASES)}


def run_browser_validation(python, source, executable, destination, env, runner):
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(
            "Pass --browser-executable with an installed Chrome/Chromium for release validation"
        )
    report = destination / "browser-validation.xml"
    browser_env = {**env, "DATA_PLATFORM_BROWSER_EXECUTABLE": str(Path(executable).resolve())}
    assets = os.environ.get("DATA_PLATFORM_BROWSER_ASSETS")
    asset_hashes = {}
    if assets:
        directory = Path(assets).resolve()
        for name in ("ui-tailwind.js", "ui-alpine.js"):
            payload = (directory / name).read_bytes()
            if not payload:
                raise RuntimeError("Browser fixture assets must be nonempty JavaScript files")
            asset_hashes[name] = hashlib.sha256(payload).hexdigest()
        browser_env["DATA_PLATFORM_BROWSER_ASSETS"] = str(directory)
    with (destination / "browser-validation.txt").open("w") as log:
        runner(
            [python, "-m", "pytest", *BROWSER_TESTS, "-q", f"--junitxml={report}"],
            cwd=source,
            env=browser_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    result = check_browser_report(report)
    if assets:
        if any(
            hashlib.sha256((directory / name).read_bytes()).hexdigest() != sha
            for name, sha in asset_hashes.items()
        ):
            raise RuntimeError("Browser fixture assets changed during validation")
        result["assets_sha256"] = asset_hashes
    return result
