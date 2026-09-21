"""Viewer annotations work without a source dataset and retain account permissions."""

import json
from pathlib import Path

import pytest

from lerobot.data_platform import viewer
from tests.datasets.test_control_plane import _bootstrap, _store


@pytest.fixture(params=[1, 5])
def cached_viewer(tmp_path, monkeypatch, request):
    store = _store(tmp_path)
    _, node = store.enroll_node(
        name="test",
        hostname="test",
        allowed_roots=["/datasets"],
        writable_roots=["/datasets"],
        capabilities={},
        enrollment_token="test",
        expected_token="test",
    )
    location = store.sync_locations(
        node["node_id"],
        [
            {
                "dataset_key": "remote/test",
                "root": "/datasets/test",
                "output_dir": "/datasets/cache",
            }
        ],
    )[0]
    cache = tmp_path / "cache"
    static = cache / "static"
    (static / "csv").mkdir(parents=True)
    (static / "viewer_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "repo_id": "remote/test",
                "fps": 10,
                "total_frames": 3,
                "total_episodes": 1,
                "features": {},
                "image_keys": [],
                "episodes": [{"episode_index": 0, "length": 3, "tasks": ["Fold towel"]}],
            }
        )
    )
    (static / f"csv/episode_000000_ds{request.param}.csv").write_text("timestamp,stage\n0,0\n0.1,0\n0.2,0\n")
    store.mark_viewer_ready(
        location["location_id"], viewer_url="/remote/test/episode_0", cache_root=str(cache)
    )
    monkeypatch.setenv("DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-secret")
    console = tmp_path / "console/static"
    console.mkdir(parents=True)
    app = viewer.run_server(
        dataset=None,
        episodes=None,
        max_frames=None,
        prepare_videos=False,
        downsample=None,
        precompute_csv=False,
        precomputed_only=True,
        host="127.0.0.1",
        port=0,
        static_folder=console,
        template_folder=Path(viewer.__file__).parent / "templates",
        database_url=f"sqlite:///{tmp_path / 'control-plane.db'}",
        remote_cache_root=tmp_path,
        start_server=False,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    return client, store, static


@pytest.mark.parametrize("role", ["admin", "data_manager", "operator", "viewer"])
def test_cache_annotations_and_reload(cached_viewer, role):
    client, store, static = cached_viewer
    if role != "admin":
        store.register_user(username=role, password="password-123", role=role)
        client.post("/api/auth/logout", json={})
        assert (
            client.post("/api/auth/login", json={"username": role, "password": "password-123"}).status_code
            == 200
        )
    page = client.get("/remote/test/episode_0?direct=1")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="dataset-result-sync"' in html
    assert 'annotateToggleUrl: "/remote/test/toggle_annotate"' in html
    assert f"annotationEditable: {'false' if role == 'viewer' else 'true'}" in html
    assert ("/mutation-jobs" in html) == (role in {"admin", "data_manager"})
    response = client.post("/remote/test/toggle_annotate", json={"annotate": True})
    assert response.status_code == (403 if role == "viewer" else 200)
    transitions = [{"time": 0, "state": 0}, {"time": 0.1, "state": 1}]
    for endpoint, payload in (
        ("subtask_annotations", {"transitions": transitions, "update_csv": True}),
        ("trim_annotations", {"trim_start_frame": 0, "trim_end_frame": 1}),
    ):
        url = f"/remote/test/{endpoint}"
        response = client.post(url, json={"episode_id": 0, **payload})
        assert response.status_code == (403 if role == "viewer" else 200), response.get_data(as_text=True)
        saved = client.get(url).get_json()["annotations"]
        assert ("0" in saved) == (role != "viewer")
        if role != "viewer":
            assert client.post(url, json={"episode_id": 99, **payload}).status_code == 404
    if role != "viewer":
        assert json.loads((static / "subtask_annotations.json").read_text())["0"] == transitions
        assert "stageEditEnabled: true" in client.get("/remote/test/episode_0?direct=1").get_data(
            as_text=True
        )
        assert "subtask_state" in next((static / "csv").glob("*.csv")).read_text()


def test_remote_flag_save_reports_pending_and_has_persistent_status(cached_viewer):
    client, _, static = cached_viewer
    response = client.post(
        "/remote/test/flagged_episodes", json={"episode_id": 0, "flagged": True, "reason": "wrong_prompt"}
    )
    assert response.status_code == 200
    assert response.json["result_sync"]["state"] == "pending"
    assert json.loads((static / "manual_flagged_episodes.json").read_text())["flagged_episodes"] == [0]
    page = client.get("/remote/test/episode_0?direct=1").text
    assert 'id="dataset-result-sync"' in page
    assert "Waiting to sync to the dataset server" in page
    assert client.get("/api/dataset-results/status?dataset_key=remote/test").json["state"] == "unsupported"


def test_viewer_remote_delete_and_edit_errors_are_visible():
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Viewer behavior checks")
    template = (Path(viewer.__file__).parent / "templates/visualize_dataset_template.html").read_text()
    methods = []
    for name in ("deleteEpisode", "toggleAnnotateMode"):
        match = re.search(rf"^                async {name}\(\) \{{", template, re.MULTILINE)
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
(async () => {
    const alerts = [];
    global.alert = text => alerts.push(text);
    global.window = {location: {href: ''}};
    global.confirm = () => true;
    global.prompt = text => text.startsWith('Reason') ? 'Invalid demonstration' : 'DELETE 1';
    Object.assign(app, {remoteDeleteUrl: '/api/control/locations/one/mutation-jobs',
        remoteDatasetKey: 'node/data', currentEpisodeId: 3, adminModeEnabled: false});
    let submitted;
    global.fetch = async (url, options) => {
        submitted = {url, body: JSON.parse(options.body)};
        return {ok: true, json: async () => ({job: {job_id: 'delete-job'}})};
    };
    await app.deleteEpisode();
    assert.equal(submitted.url, app.remoteDeleteUrl);
    assert.deepEqual(submitted.body.options.episodes, [3]);
    assert.equal(submitted.body.confirmation, 'MUTATE node/data');
    assert.equal(submitted.body.options.reason, 'Invalid demonstration');
    assert.equal(window.location.href, '/?page=runs');
    window.location.href = '';
    global.fetch = async () => ({ok: false, status: 403,
        json: async () => ({error: 'Source mutations are disabled'})});
    await app.deleteEpisode();
    assert.match(alerts.at(-1), /Source mutations are disabled/);
    assert.equal(window.location.href, '');
    assert.equal(app.trimApplying, false);
    Object.assign(app, {annotationEditable: true, annotateEnabled: false, stageEditEnabled: false});
    await app.toggleAnnotateMode();
    assert.match(alerts.at(-1), /Source mutations are disabled/);
    assert.equal(app.annotateEnabled, false);
    assert.equal(app.stageEditEnabled, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_scoped_data_manager_cached_viewer_delete_button(cached_viewer):
    client, store, _ = cached_viewer
    user = store.register_user(username="scoped-manager", password="password-123", role="data_manager")
    location = store.list_locations()[0]
    endpoint = f"/api/auth/users/{user['user_id']}/data-scopes"
    assert client.put(endpoint, json={"location_ids": [location["location_id"]]}).status_code == 200
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"username": "scoped-manager", "password": "password-123"})
    assert "/mutation-jobs" in client.get("/remote/test/episode_0?direct=1").get_data(as_text=True)
    store.update_user(user["user_id"], role="operator")
    assert "/mutation-jobs" not in client.get("/remote/test/episode_0?direct=1").get_data(as_text=True)


def test_remote_flag_types_use_location_cache(cached_viewer):
    client, store, _ = cached_viewer
    location = store.list_locations()[0]
    static = Path(location["metadata"]["cache_root"]) / "static"
    (static / "flagged_episodes.json").write_text(json.dumps({"flagged_episodes": [0]}))
    (static / "quality_flagged_episodes.json").write_text(
        json.dumps(
            {
                "flagged_episodes": [0],
                "flag_reasons": {"0": [{"type": "quality_flag", "reason": "motion_jump"}]},
            }
        )
    )
    response = client.get(f"/api/control/locations/{location['location_id']}/flagged-episodes")
    assert response.status_code == 200
    assert response.get_json()["flagged_episodes"] == [0]
    assert any(item["reason"] == "motion_jump" for item in response.get_json()["flag_reasons"]["0"])
    assert client.get("/api/control/locations/missing/flagged-episodes").status_code == 404
    store.mark_viewer_stale(location["location_id"])
    response = client.get(f"/api/control/locations/{location['location_id']}/flagged-episodes")
    assert response.status_code == 409
    assert "Prepare viewer" in response.get_json()["error"]


def test_delete_by_flag_type_frontend_uses_remote_location(cached_viewer):
    import re
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("Node.js required")
    client, _, _ = cached_viewer
    html = client.get("/").get_data(as_text=True)
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    checks = r"""
const assert = require('node:assert/strict');
(async () => {
 const app = precomputeConsole();
 Object.defineProperty(app, 'selectedDataset', {get: () => ({remote:true, remote_location_id:'location-one'})});
 app.ensureSelectedDatasetLoaded = async () => 'remote:location-one';
 app.requestJson = async (url, body, method) => {
   assert.equal(url, '/api/control/locations/location-one/flagged-episodes');
   assert.equal(method, 'GET');
   return {flagged_episodes:[3], flag_reasons:{'3':[{type:'quality_flag',reason:'motion_jump'}]}};
 };
 await app.loadDeleteFlagReasonOptions();
 assert.equal(app.error, '');
 assert.deepEqual(app.deleteFlagReasonEpisodes.motion_jump, [3]);
 app.preprocess.delete_flag_reason = 'motion_jump';
 app.applyDeleteFlagReasonSelection();
 assert.equal(app.preprocess.delete_episode_ids, '3');
 app.controlPlaneUser = {role:'data_manager'};
 app.remoteSourceMutationsEnabled = true;
 app.selectedRemoteLocation = {location_id:'location-one', source_mutation_allowed:true};
 const node = {capabilities:{source_mutations_enabled:true}};
 app.selectedRemoteNode = () => node;
 app.preprocess.schema_op = 'delete_episodes';
 app.preprocess.delete_reason = 'Remove invalid demonstration';
 assert.equal(app.deleteEpisodesDisabledReason(), '');
 assert(app.canStartSchemaFix());
 app.selectedRemoteLocation.dataset_key = 'node/data';
 let submitted;
 app.submitRemoteJob = async (url, body) => {submitted = {url, body};};
 global.window = {confirm: () => true, prompt: message => {
   assert.equal(message, 'Type exactly to confirm:\nMUTATE');
   return 'MUTATE';
 }};
 await app.startRemoteSchemaFix();
 assert.equal(submitted.url, '/api/control/locations/location-one/mutation-jobs');
 assert.equal(submitted.body.confirmation, 'MUTATE node/data');
 assert.deepEqual(submitted.body.options.episodes, [3]);
 for (const answer of [null, '', 'mutate', 'MUTATE node/data']) {
   submitted = null;
   window.prompt = () => answer;
   await app.startRemoteSchemaFix();
   assert.equal(submitted, null);
 }
 window.confirm = () => false;
 window.prompt = () => {throw new Error('Cancelled confirmation must stop');};
 await app.startRemoteSchemaFix();
 assert.equal(submitted, null);
 app.remoteSourceMutationsEnabled = false;
 assert(!app.canStartSchemaFix());
 assert.match(app.deleteEpisodesDisabledReason(), /Server A/);
 app.remoteSourceMutationsEnabled = true;
 node.capabilities.source_mutations_enabled = false;
 assert(!app.canStartSchemaFix());
 assert.match(app.deleteEpisodesDisabledReason(), /this Agent/);
 node.capabilities.source_mutations_enabled = true;
 app.selectedRemoteLocation.source_mutation_allowed = false;
 assert(!app.canStartSchemaFix());
 assert.match(app.deleteEpisodesDisabledReason(), /permission/);
 app.selectedRemoteLocation.source_mutation_allowed = true;
 app.preprocess.delete_episode_ids = '';
 assert.match(app.deleteEpisodesDisabledReason(), /episode IDs/);
 app.preprocess.delete_episode_ids = '3';
 app.preprocess.delete_reason = '  ';
 assert.match(app.deleteEpisodesDisabledReason(), /deletion reason/);
 app.preprocess.delete_reason = 'Remove invalid demonstration';
 app.deleteFlagReasonLoading = true;
 assert(!app.canStartSchemaFix());
 app.deleteFlagReasonLoading = false;
 app.controlPlaneUser.role = 'operator';
 app.remoteSourceMutationsEnabled = false;
 assert(app.canStartSchemaFix()); // Operator submits a request, not a direct deletion.
})().catch(err => {console.error(err);process.exit(1);});
"""
    subprocess.run(["node"], input=script + checks, check=True, capture_output=True, text=True, timeout=10)
