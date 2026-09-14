"""Shared Qwen credentials and reviewed task onboarding without preparation caches."""

import io
import json
import threading
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from lerobot.data_platform import qwen
from lerobot.data_platform.lifecycle import LifecycleStore
from lerobot.data_platform.precompute.labeling.qwen_dashscope import QwenDashScopeDetector
from lerobot.data_platform.precompute.labeling.qwen_dashscope import get_capabilities as labeling_capabilities
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from lerobot.data_platform.precompute.tagging.vlm_backend import DashScopeVLMTagger
from lerobot.data_platform.precompute.tagging.vlm_backend import get_capabilities as tagging_capabilities
from lerobot.data_platform.routes import task_suggestions as suggestion_routes
from lerobot.data_platform.routes import tasks as task_routes
from lerobot.data_platform.routes.control_plane import register_control_plane_auth_routes
from lerobot.data_platform.task_catalog import TaskConfigSnapshot, TaskDefinition, builtin_catalog
from lerobot.data_platform.task_suggestions import accepted_definitions, generate_suggestions
from tests.datasets.test_control_plane import _bootstrap
from tests.datasets.test_control_plane import _store as control_store
from tests.datasets.test_lifecycle import _make_dataset, _snapshot, _write_jsonl

FRIDGE = {
    "task_id": "open_fridge_door",
    "label": "Open the refrigerator door",
    "family": "open_door",
    "attributes": {"object": "door", "appliance": "refrigerator"},
}
TEXTS = ["Open the fridge", "Move it there", "Open the door of the washing machine below"]


def _completion(rows):
    return {"choices": [{"message": {"content": json.dumps({"suggestions": rows})}, "finish_reason": "stop"}]}


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch):
    for name in qwen.DASHSCOPE_API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_shared_credentials_priority_override_and_capabilities(monkeypatch):
    monkeypatch.setenv("QWEN_DASHSCOPE_API_KEY", "fallback-test-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "  shared-test-key  ")
    assert qwen.QwenClient().api_key == "shared-test-key"
    assert QwenDashScopeDetector.load().api_key == "shared-test-key"
    assert DashScopeVLMTagger.load().api_key == "shared-test-key"
    assert QwenDashScopeDetector.load(api_key="explicit-test-key").api_key == "explicit-test-key"
    assert labeling_capabilities()["token_configured"]
    assert tagging_capabilities()["token_configured"]
    assert "shared-test-key" not in json.dumps(labeling_capabilities())
    assert "shared-test-key" not in repr(qwen.QwenClient())
    monkeypatch.setenv("DASHSCOPE_API_KEY", " ")
    assert qwen.QwenClient().api_key == "fallback-test-key"


def test_shared_key_is_not_sent_to_custom_endpoints_or_redirects(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "shared-test-key")
    with pytest.raises(ValueError, match="custom endpoint"):
        QwenDashScopeDetector.load(base_url="http://localhost:8000/v1")
    assert DashScopeVLMTagger.load(base_url="http://localhost:8000/v1", api_key="EMPTY").api_key == "EMPTY"
    assert qwen._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.org") is None


def test_shared_transport_payload_and_sanitized_provider_errors(monkeypatch):
    captured = []

    def open_request(request, timeout):
        captured.append((request, timeout))
        return io.BytesIO(json.dumps(_completion([])).encode())

    monkeypatch.setattr(qwen.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    client = qwen.QwenClient(api_key="shared-test-key")
    assert client.post_chat_completion({"model": "test-model"})["choices"]
    request, timeout = captured[0]
    assert request.get_header("Authorization") == "Bearer shared-test-key"
    assert json.loads(request.data) == {"model": "test-model"}
    assert request.full_url.endswith("/chat/completions") and timeout == 120

    def fail_request(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.org", 401, "shared-test-key", {}, io.BytesIO(b"secret"))

    monkeypatch.setattr(qwen.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fail_request))
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        client.post_chat_completion({})
    assert "shared-test-key" not in str(error.value) and "secret" not in str(error.value)


def test_suggestions_reuse_catalog_batch_candidates_and_force_neutral_stages():
    calls = []

    def complete(payload):
        calls.append(payload)
        context = json.loads(payload["messages"][1]["content"])
        assert "dataset_key" not in context and "episodes" not in context
        return _completion(
            [
                {
                    "instruction": text,
                    "action": "create",
                    "task": {
                        **FRIDGE,
                        "aliases": ["invented"],
                        "stage_strategy": "legacy_pick",
                        "stage_count": 6,
                    },
                }
                for text in context["instructions"]
            ]
        )

    progress = []
    result = generate_suggestions(
        [f"Open fridge instruction {index}" for index in range(21)],
        builtin_catalog(),
        client=SimpleNamespace(post_chat_completion=complete),
        model="test-model",
        progress=lambda *args: progress.append(args),
    )
    assert len(calls) == 2 and len(result) == 21
    assert progress == [(20, 21), (21, 21)]
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["enable_thinking"] is False
    assert all(row.action == "create" and row.task.stage_count == 5 for row in result)
    assert all(row.task.stage_strategy == "equal_time" and not row.task.aliases for row in result)
    assert FRIDGE["task_id"] in {
        row["task_id"] for row in json.loads(calls[1]["messages"][1]["content"])["catalog"]
    }


def test_invalid_or_ambiguous_model_rows_remain_unconfigured():
    client = SimpleNamespace(
        post_chat_completion=lambda _: _completion(
            [
                {"instruction": TEXTS[0], "action": "reuse", "task": {"task_id": "missing"}},
                {"instruction": TEXTS[1], "action": "review", "reason": "Which object and destination?"},
            ]
        )
    )
    rows = generate_suggestions(
        TEXTS, builtin_catalog(), client=client, model="test", progress=lambda *args: None
    )
    assert all(row.action == "review" and row.task is None for row in rows)
    with pytest.raises(ValueError, match="resolved"):
        accepted_definitions([rows[1].to_dict()], builtin_catalog(), TEXTS)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": "not json"}}]},
        _completion([{"instruction": "not in input"}]),
    ],
)
def test_invalid_model_responses_do_not_produce_publishable_results(response):
    with pytest.raises(ValueError, match="invalid suggestion JSON"):
        generate_suggestions(
            TEXTS,
            builtin_catalog(),
            client=SimpleNamespace(post_chat_completion=lambda _: response),
            model="test",
            progress=lambda *args: None,
        )


def test_acceptance_only_learns_selected_observed_aliases_and_preserves_conflicts():
    first = TaskDefinition.from_dict({**FRIDGE, "aliases": [TEXTS[0]]})
    other = replace(first, task_id="another_fridge")
    catalog = replace(builtin_catalog(), tasks=[first, other])
    definitions, mappings = accepted_definitions(
        [
            {"instruction": TEXTS[0], "action": "reuse", "task": {"task_id": first.task_id}},
            {"instruction": "Pull open the fridge", "action": "reuse", "task": {"task_id": first.task_id}},
        ],
        catalog,
        [TEXTS[0], "Pull open the fridge"],
    )
    snapshot = TaskConfigSnapshot(
        replace(catalog, tasks=[TaskDefinition.from_dict(row) for row in definitions])
    )
    assert snapshot.resolve(TEXTS[0]).status == "conflict"
    assert snapshot.resolve("Pull open the fridge").task_id == first.task_id
    assert snapshot.resolve("invented synonym").status == "unmapped"
    assert mappings["open the fridge"] == first.task_id


def _app(tmp_path, monkeypatch, mode="v2.1"):
    root = tmp_path / "source"
    _make_dataset(root)
    records = [{"task_index": index, "task": text} for index, text in enumerate(TEXTS)]
    records.append({"task_index": 3 if mode == "v3.0" else 8, "task": "  Open the fridge. "})
    episodes = [{"episode_index": index, "tasks": [TEXTS[index]], "length": index + 2} for index in range(2)]
    _write_jsonl(root / "meta" / "tasks.jsonl", records)
    _write_jsonl(root / "meta" / "episodes.jsonl", episodes)
    info_path = root / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["total_tasks"] = len(records)
    info_path.write_text(json.dumps(info))
    if mode == "v3.0":
        converted = tmp_path / "source-v3"
        run_convert_v3(root, out_root=converted, workers=1)
        root = converted
    lifecycle = LifecycleStore(tmp_path / "ledger")
    ctx = SimpleNamespace(
        lifecycle_store=lifecycle,
        control_plane_store=None,
        datasets_index={("local", "test"): {"root": str(root), "output_dir": str(tmp_path / "cache")}},
        repo_key=lambda key: tuple(key.split("/", 1)),
        jobs_registry={},
        jobs_lock=threading.Lock(),
        append_operation_log=lambda *args, **kwargs: None,
        update_job=lambda job, payload: job.update(payload),
        finish_job=lambda job, message: job.update(status="done", message=message),
        fail_job=lambda job, message, exc: job.update(status="error", error=str(exc)),
    )
    if mode == "remote":
        ctx.control_plane_store = SimpleNamespace(
            list_locations=lambda: [
                {
                    "dataset_key": "local/test",
                    "location_id": "test-location",
                    "metadata": {"tasks": records, "episodes": episodes},
                }
            ]
        )
        ctx.datasets_index = {}
    monkeypatch.setattr(
        suggestion_routes,
        "threading",
        SimpleNamespace(Thread=lambda target, **kwargs: SimpleNamespace(start=target)),
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "shared-test-key")
    app = Flask(__name__, template_folder=str(Path(task_routes.__file__).parents[1] / "templates"))
    task_routes.register_task_routes(app, ctx)
    return app, ctx, root


@pytest.mark.parametrize("mode", ["v2.1", "v3.0", "remote"])
def test_suggest_review_save_preview_without_cache_or_source_mutation(tmp_path, monkeypatch, mode):
    app, ctx, root = _app(tmp_path, monkeypatch, mode)
    before = _snapshot(root)
    calls = []

    def complete(client, payload):
        assert client.api_key == "shared-test-key"
        texts = json.loads(payload["messages"][1]["content"])["instructions"]
        calls.append(texts)
        assert len(texts) == 2 and TEXTS[2] not in texts
        return _completion(
            [
                {"instruction": TEXTS[0], "action": "create", "task": FRIDGE},
                {"instruction": TEXTS[1], "action": "review", "reason": "Unspecified object and destination"},
            ]
        )

    monkeypatch.setattr(qwen.QwenClient, "post_chat_completion", complete)
    client = app.test_client()
    assert client.get("/api/task-suggestions/capabilities").get_json()["token_configured"]
    response = client.post("/api/task-suggestions", json={"dataset_key": "local/test"})
    assert response.status_code == 202, response.get_json()
    data = response.get_json()
    job_id = data["job"]["id"]
    assert len(calls) == 1
    assert data["result"]["suggestions"][0]["episode_count"] == 1
    assert ctx.lifecycle_store.tasks.catalogs()[0].version == 0
    assert ctx.lifecycle_store.tasks.current("local/test") is None
    selected = [next(row for row in data["result"]["suggestions"] if row["action"] == "create")]
    selected[0]["task"]["label"] = "Open fridge door (reviewed)"
    accepted = client.post(f"/api/task-suggestions/{job_id}/accept", json={"suggestions": selected})
    assert accepted.status_code == 200, accepted.get_json()
    preview = accepted.get_json()["preview"]
    assert (
        next(row for row in preview["tasks"] if row["raw_task"] == TEXTS[0])["label"]
        == "Open fridge door (reviewed)"
    )
    assert next(row for row in preview["tasks"] if row["raw_task"] == TEXTS[1])["status"] == "unmapped"
    assert ctx.lifecycle_store.tasks.catalogs()[0].version == 1
    assert ctx.lifecycle_store.tasks.current("local/test") is None
    assert len(ctx.jobs_registry) == 1
    assert not (tmp_path / "cache").exists()
    assert _snapshot(root) == before
    assert "shared-test-key" not in json.dumps(data)
    assert (
        client.post(f"/api/task-suggestions/{job_id}/accept", json={"suggestions": selected}).status_code
        == 409
    )


def test_missing_key_failed_model_and_changed_inventory_keep_manual_setup_usable(tmp_path, monkeypatch):
    app, ctx, root = _app(tmp_path, monkeypatch)
    client = app.test_client()
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    assert not client.get("/api/task-suggestions/capabilities").get_json()["token_configured"]
    assert client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).status_code == 400
    assert not ctx.jobs_registry
    assert client.get("/api/task-mappings?dataset_key=local/test").status_code == 200
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(qwen.QwenClient, "post_chat_completion", lambda *args: {})
    failed = client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).get_json()
    assert failed["job"]["status"] == "error" and failed["result"] is None
    assert ctx.lifecycle_store.tasks.catalogs()[0].version == 0

    monkeypatch.setattr(
        qwen.QwenClient,
        "post_chat_completion",
        lambda *args: _completion(
            [
                {"instruction": TEXTS[0], "action": "create", "task": FRIDGE},
            ]
        ),
    )
    data = client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).get_json()
    _write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": "Changed instruction"}])
    assert (
        client.post(
            f"/api/task-suggestions/{data['job']['id']}/accept",
            json={"suggestions": [data["result"]["suggestions"][0]]},
        ).status_code
        == 409
    )
    assert ctx.lifecycle_store.tasks.catalogs()[0].version == 0


def test_suggestions_authentication_and_viewer_access(tmp_path, monkeypatch):
    app, ctx, _ = _app(tmp_path, monkeypatch)
    control = control_store(tmp_path)
    register_control_plane_auth_routes(
        app, control, bootstrap_token="bootstrap-secret", allow_registration=False
    )
    client = app.test_client()
    assert client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).status_code == 401
    assert _bootstrap(client).status_code == 200
    control.register_user(username="task-viewer", password="viewer-password", display_name="Viewer")
    client.post("/api/auth/logout", json={})
    assert (
        client.post(
            "/api/auth/login", json={"username": "task-viewer", "password": "viewer-password"}
        ).status_code
        == 200
    )
    assert client.get("/api/task-suggestions/capabilities").status_code == 200
    assert client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).status_code == 403
    assert not ctx.jobs_registry


def test_background_suggestion_polling_and_no_call_for_matched_instructions(tmp_path, monkeypatch):
    app, ctx, root = _app(tmp_path, monkeypatch)
    workers = []
    monkeypatch.setattr(
        suggestion_routes,
        "threading",
        SimpleNamespace(
            Thread=lambda target, **kwargs: SimpleNamespace(start=lambda: workers.append(target))
        ),
    )
    monkeypatch.setattr(qwen.QwenClient, "post_chat_completion", lambda *args: _completion([]))
    client = app.test_client()
    started = client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).get_json()
    url = f"/api/task-suggestions/{started['job']['id']}"
    assert started["job"]["status"] == "queued" and started["result"] is None
    assert client.post(url + "/accept", json={"suggestions": []}).status_code == 409
    workers[0]()
    polled = client.get(url).get_json()
    assert polled["job"]["status"] == "done"
    assert len(polled["result"]["suggestions"]) == 2
    assert client.get("/api/task-suggestions/missing").status_code == 404
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    _write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": TEXTS[2]}])
    matched = client.post("/api/task-suggestions", json={"dataset_key": "local/test"}).get_json()
    assert matched == {"job": None, "result": {"suggestions": []}}
    assert len(ctx.jobs_registry) == 1
