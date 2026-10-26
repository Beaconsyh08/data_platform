"""The split console retains one registry and ignores superseded UI reads."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lerobot.data_platform import viewer
from tests.datasets.test_value_edit import _make_dataset


def console_app(tmp_path, **kwargs):
    static = tmp_path / "console/static"
    static.mkdir(parents=True, exist_ok=True)
    return viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=static,
        template_folder=Path(viewer.__file__).parent / "templates",
        start_server=False,
        **kwargs,
    )


def test_catalog_and_completion_share_registry_and_invalidate_cached_results(tmp_path):
    app = console_app(tmp_path)
    catalog = app.extensions["data_platform_catalog"]
    ctx = app.extensions["data_platform_route_context"]
    source = tmp_path / "source"
    _make_dataset(source)
    output = tmp_path / "viewer"
    response = app.test_client().post(
        "/api/datasets/register", json={"root": str(source), "output_dir": str(output)}
    )
    assert response.status_code == 200, response.json
    key = tuple(response.json["dataset"]["key"].split("/", 1))
    assert catalog.state.datasets_index is ctx.datasets_index
    catalog.state.task_caches[key] = {"stale": True}
    catalog.state.columns_info_cache[key] = ([{"old": True}], [], [])
    catalog.state.light_cache_status_cache[(str(source), str(output))] = (0, {"old": True})
    # Publication/result handling can be called outside any Flask request context.
    catalog.register_precomputed(
        SimpleNamespace(repo_id="/".join(key), root=source, output_dir=output, episodes=[0, 1])
    )
    assert catalog.state.episodes_by_key[key] == [0, 1]
    assert key not in catalog.state.task_caches
    assert not catalog.state.light_cache_status_cache
    app.extensions["data_platform_complete_local"](
        {"local_registrations": [{"repo_id": "/".join(key), "root": str(source), "output_dir": str(output)}]}
    )
    assert not catalog.state.columns_info_cache
    assert ctx.ensure_dataset_loaded(key)[0].root == source
    catalog.unregister_dataset(key)
    assert key not in ctx.datasets_index
    assert app.test_client().get("/api/datasets").json == {"datasets": []}
    assert (source / "meta/info.json").is_file()
    assert json.loads(Path(catalog.state.registry_state["path"]).read_text())["datasets"] == []


def test_console_instances_do_not_share_registry_or_cache(tmp_path):
    left = console_app(tmp_path / "left").extensions["data_platform_catalog"]
    right = console_app(tmp_path / "right").extensions["data_platform_catalog"]
    left.state.datasets_index[("left", "only")] = {"root": "left"}
    left.state.task_caches[("left", "only")] = {"result": "left"}
    assert right.state.datasets_index == {}
    assert right.state.task_caches == {}


def test_preload_route_retains_normalized_dataset_url(tmp_path, monkeypatch):
    from lerobot.data_platform.processing.console_submissions import SubmissionResult

    app = console_app(tmp_path)
    service = app.extensions["data_platform_route_context"].console_submissions
    monkeypatch.setattr(service, "preload_viewer", lambda body, url: SubmissionResult({"viewer_url": url}))
    response = app.test_client().post(
        "/api/viewer/preload", json={"dataset_key": "  local/example  ", "episode_id": 2}
    )
    assert response.status_code == 200
    assert response.json["viewer_url"].startswith("/local/example/episode_2?")


def test_composed_console_rejects_old_dataset_account_and_environment_responses(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for composed UI behavior checks")
    html = console_app(tmp_path).test_client().get("/").get_data(as_text=True)
    # The composed page registers its shared assistant entry point on the browser global.
    script = (
        "global.window = {addEventListener:()=>{}}; global.document={body:{dataset:{}}};\n"
        + re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    )
    checks = r"""
const assert = require('node:assert/strict');
(async () => {
 const app = precomputeConsole();
 const other = precomputeConsole();
 app.preprocess.value_edits.push({id:99});
 assert.equal(other.preprocess.value_edits.length, 1);
 global.location = {origin:'https://dev.example'};
 app.controlPlaneUser = {user_id:'alice',role:'operator'};
 app.selectedDatasetKey='a'; app.selectedDataset={key:'a'};
 let replyA, replyB;
 app.requestJson = () => new Promise(resolve=>{replyA=resolve;});
 const old = app.loadCompareCandidates();
 app.selectedDatasetKey='b';app.selectedDataset={key:'b'};app.selectionToken++;
 app.requestJson = () => new Promise(resolve=>{replyB=resolve;});
 const fresh = app.loadCompareCandidates();
 replyB({datasets:[{key:'b-result'}]});await fresh;
 replyA({datasets:[{key:'a-result'}]});await old;
 assert.equal(app.compareCandidates[0].key,'b-result');
 assert.equal(app.compare.dataset_key_b,'b-result');
 app.datasets=[{key:'previous'}];
 app.requestJson=()=>new Promise(resolve=>{replyA=resolve;});
 const accountRead=app.loadDatasets();app.controlPlaneUser={user_id:'bob',role:'viewer'};
 replyA({datasets:[{key:'alice-private'}]});await accountRead;
 assert.equal(app.datasets[0].key,'previous');
 const environmentRead=app.loadDatasets();global.location.origin='https://prod.example';
 replyA({datasets:[{key:'dev-private'}]});await environmentRead;
 assert.equal(app.datasets[0].key,'previous');
 app.rootDir='/old';app.datasetServerFilter='local';app.candidates=[];
 const browseRead=app.loadDatasetRoots();app.datasetBrowseRevision++;app.rootDir='/new';
 replyA({root_dir:'/old',candidates:[{root:'/old/private'}]});await browseRead;
 assert.equal(app.rootDir,'/new');assert.deepEqual(app.candidates,[]);
 // A superseded failure cannot overwrite the currently selected input's feedback.
 let fail; app.error='new selection';app.requestJson=()=>new Promise((resolve,reject)=>{fail=reject;});
 const failedRead=app.loadCompareCandidates();app.selectedDatasetKey='c';
 fail(new Error('old failure'));await failedRead;assert.equal(app.error,'new selection');
})().catch(error=>{console.error(error);process.exit(1)});
"""
    result = subprocess.run(["node"], input=script + checks, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
