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
        start_server=False,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert _bootstrap(client).status_code == 200
    return client, store, static


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
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
    assert 'annotateToggleUrl: "/remote/test/toggle_annotate"' in html
    assert f"annotationEditable: {'false' if role == 'viewer' else 'true'}" in html
    assert ("/mutation-jobs" in html) == (role == "admin")
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
