"""Explicit browser release gate; a skipped test is not acceptance evidence."""

from pathlib import Path
from xml.etree import ElementTree

BROWSER_TESTS = (
    "tests/datasets/test_console_browser.py",
    "tests/datasets/test_processing_browser.py",
)
REQUIRED_CASES = {
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
    if not executable or not Path(executable).is_file():
        raise RuntimeError(
            "Pass --browser-executable with an installed Chrome/Chromium for release validation"
        )
    report = destination / "browser-validation.xml"
    browser_env = {**env, "DATA_PLATFORM_BROWSER_EXECUTABLE": str(Path(executable).resolve())}
    with (destination / "browser-validation.txt").open("w") as log:
        runner(
            [python, "-m", "pytest", *BROWSER_TESTS, "-q", f"--junitxml={report}"],
            cwd=source,
            env=browser_env,
            stdout=log,
        )
    return check_browser_report(report)
