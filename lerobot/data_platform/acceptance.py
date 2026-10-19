"""Release-bound browser evidence and compact matrices referring to existing Jobs/Attempts."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from lerobot.data_platform.artifact_store import ArtifactRef, atomic_json, configured_store, digest_file


def collect_matrix(control, artifacts, rows, release, *, changed_operators):
    """Collect completed real executions; this does not create a second execution history."""
    from lerobot.data_platform.control_plane import RemoteJob
    from lerobot.data_platform.management_storage import JobAttempt, JobControl

    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise ValueError("A nonempty bounded acceptance matrix is required")
    output, covered, identities = [], set(), set()
    with control.sessions() as session:
        for item in rows:
            identity = tuple(item[key] for key in ("operator", "version", "node_id", "format", "scenario"))
            if identity in identities or not all(isinstance(value, str) and value for value in identity):
                raise ValueError(
                    "Matrix entries must have unique operator/version/node/format/scenario identities"
                )
            identities.add(identity)
            job = session.get(RemoteJob, item["job_id"])
            state = session.get(JobControl, item["job_id"])
            attempt = session.get(JobAttempt, item["attempt_id"])
            if (
                not job
                or not state
                or not attempt
                or attempt.job_id != job.job_id
                or state.attempt_id != attempt.attempt_id
                or not state.stop_confirmed
                or job.status != "done"
                or attempt.status != "done"
                or not attempt.finished_at
            ):
                raise ValueError("Acceptance requires the acknowledged successful Job and current Attempt")
            snapshot = (job.options or {}).get("operator_snapshot", {})
            if (job.operation, snapshot.get("version"), job.node_id, snapshot.get("data_format")) != identity[
                :4
            ]:
                raise ValueError("Matrix identity does not match the execution snapshot")
            source_release = snapshot.get("release")
            if not source_release or source_release == "legacy":
                raise ValueError("Execution has no release provenance; rerun it with this release")
            if source_release != release and job.operation in changed_operators:
                raise ValueError("An affected operator requires fresh real execution evidence")
            attachments = []
            for value in item.get("attachments", []):
                ref = ArtifactRef.parse(value)
                if not artifacts.verified(ref):
                    raise ValueError("Acceptance attachment has not been verified in OSS")
                attachments.append(value)
            output.append(
                {
                    key: item[key]
                    for key in (
                        "operator",
                        "version",
                        "node_id",
                        "format",
                        "scenario",
                        "job_id",
                        "attempt_id",
                    )
                }
                | {
                    "source_release": source_release,
                    "reused": source_release != release,
                    "attachments": attachments,
                }
            )
            covered.add(job.operation)
    if not set(changed_operators) <= covered:
        raise ValueError("Acceptance matrix does not cover all affected operators")
    return {
        "schema_version": 1,
        "release": release,
        "changed_operators": sorted(changed_operators),
        "cases": output,
    }


def installed_browser(base_url, release_root, executable, username, password):
    """Real development login with normal TLS, an installed release and protected list requests."""
    from playwright.sync_api import sync_playwright

    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use the public HTTPS origin with valid TLS and no embedded credentials")
    manifest = json.loads((release_root / "release.json").read_text())
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=str(executable), args=["--no-sandbox"])
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda _error: errors.append(True))
            health = page.request.get(base_url.rstrip("/") + "/healthz")
            if not health.ok:
                raise RuntimeError("Installed health request failed")
            identity = health.json()
            if (
                identity.get("release") != manifest["release"]
                or identity.get("environment") != "dev"
                or identity.get("maintenance")
            ):
                raise ValueError("Browser acceptance must target the installed development release")
            page.goto(base_url.rstrip("/") + "/login")
            page.locator("#username").fill(username)
            page.locator("#password").fill(password)
            with page.expect_response(lambda response: response.url.endswith("/api/auth/login")) as response:
                page.locator("#submit").click()
            if not response.value.ok:
                raise RuntimeError("Installed browser login failed")
            page.wait_for_url(lambda url: urlsplit(url).path != "/login")
            for route, field in (
                ("/api/control/jobs?summary=1&page_size=50", "jobs"),
                ("/api/pipelines?page_size=50", "pipelines"),
                ("/api/pipeline-runs?page_size=50", "runs"),
            ):
                result = page.request.get(base_url.rstrip("/") + route)
                if not result.ok or not isinstance(result.json().get(field), list):
                    raise RuntimeError("Installed protected list acceptance failed")
            page.goto(base_url.rstrip("/") + "/pipelines")
            page.locator("#steps").wait_for(state="attached")
            if errors:
                raise RuntimeError("Installed browser produced JavaScript errors")
            return {
                "schema_version": 1,
                "release": manifest["release"],
                "environment": "dev",
                "instance_id": identity["instance_id"],
                "manifest_sha256": digest_file(release_root / "release.json"),
                "origin": base_url,
                "passed": True,
                "checked_at": time.time(),
                "scenarios": ["login", "protected_lists", "pipeline_page"],
            }
        except Exception:
            raise RuntimeError(
                "Installed browser acceptance failed; inspect login, TLS, release identity and protected pages"
            ) from None
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    browser = sub.add_parser("browser")
    browser.add_argument("--origin", required=True)
    browser.add_argument("--release-root", type=Path, required=True)
    browser.add_argument("--browser-executable", type=Path, required=True)
    browser.add_argument("--username", required=True)
    browser.add_argument("--output", type=Path, required=True)
    matrix = sub.add_parser("matrix")
    matrix.add_argument("--input", type=Path, required=True)
    matrix.add_argument("--release", required=True)
    matrix.add_argument("--output", type=Path, required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--pipeline-id", required=True)
    submit.add_argument("--revision", type=int, required=True)
    submit.add_argument("--location-id", required=True)
    submit.add_argument("--owner", required=True)
    submit.add_argument("--delivery", required=True, help="Stable acceptance idempotency key")
    submit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "browser":
        result = installed_browser(
            args.origin,
            args.release_root,
            args.browser_executable,
            args.username,
            getpass.getpass("Platform password: "),
        )
    elif args.command == "submit":
        from lerobot.data_platform.processing.coordinator import configured_manager

        configured_store().check_space()
        result = configured_manager().start(
            args.owner, args.pipeline_id, args.revision, args.location_id, args.delivery
        )
        result = {key: result[key] for key in ("run_id", "status", "revision")}
    else:
        from lerobot.data_platform.control_plane import ControlPlaneStore

        spec = json.loads(args.input.read_text())
        artifacts = configured_store()
        artifacts.check_space()
        result = collect_matrix(
            ControlPlaneStore(os.environ["DATA_PLATFORM_DATABASE_URL"]),
            artifacts,
            spec["cases"],
            args.release,
            changed_operators=spec["changed_operators"],
        )
    atomic_json(args.output, result)
    print(f"Acceptance evidence saved: {args.output}")


if __name__ == "__main__":
    main()
