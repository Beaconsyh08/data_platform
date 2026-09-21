"""Remote trims preserve source invariants; chart controls share visible state."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lerobot.data_platform import viewer
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from tests.datasets.test_data_platform_agent import _agent
from tests.datasets.test_episode_deletion_requests import setup_requests
from tests.datasets.test_preprocess_ops import _add_v21_stat_counts, _make_dataset


def test_trim_route_validates_role_capability_and_range(tmp_path):
    store, admin, operator, _, node, location, _ = setup_requests(tmp_path)
    url = f"/api/control/locations/{location['location_id']}/mutation-jobs"
    payload = {
        "op": "trim_episode",
        "options": {"episode_id": 1, "start_frame": 1, "end_frame": 2, "reason": "Remove idle frames"},
        "confirmation": f"MUTATE {location['dataset_key']}",
    }
    assert operator.post(url, json=payload).status_code == 403
    assert admin.post(url, json=payload).status_code == 409  # Old Agent lacks Trim.
    store.heartbeat(
        node["node_id"],
        capabilities={
            "source_mutations_enabled": True,
            "operations": ["mutation.trim_episode"],
            "job_protocol": 1,
            "data_profile_protocol": 1,
        },
    )
    for options in (
        {"reason": ""},
        {"start_frame": -1},
        {"end_frame": 0},
        {"episode_id": True},
        {"start_frame": 1.5},
    ):
        response = admin.post(url, json={**payload, "options": {**payload["options"], **options}})
        assert response.status_code == 400, response.get_json()
    assert not store.list_jobs()
    response = admin.post(url, json=payload)
    assert response.status_code == 202, response.get_json()
    assert response.get_json()["job"]["operation"] == "mutation.trim_episode"


@pytest.mark.parametrize("v3", [False, True])
def test_agent_trim_preserves_other_episode_and_backup(tmp_path, v3):
    from lerobot.data_platform.precompute.dataset_io import read_episode_table

    data = tmp_path / "datasets"
    source = data / "source"
    _make_dataset(source)
    _add_v21_stat_counts(source)
    if v3:
        source = run_convert_v3(source, data / "v3").out_root
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    agent = _agent(tmp_path, data, data, allow_source_mutations=True)
    static = data / "cache/static"
    (static / "csv").mkdir(parents=True)
    (static / "csv/episode_000001_ds1.csv").write_text("stale")
    job = {
        "job_id": "trim-test",
        "operation": "mutation.trim_episode",
        "options": {"episode_id": 1, "start_frame": 1, "end_frame": 2, "reason": "Idle frames"},
        "location": {"dataset_key": "test/source", "root": str(source), "output_dir": str(static.parent)},
    }
    result = agent.execute_job(job)
    assert result["trim"]["new_length"] == 2
    assert result["source_changed"]
    backup = Path(result["backup_root"]) / "dataset"
    assert all((backup / name).read_bytes() == value for name, value in before.items())
    ds = viewer.MetaOnlyDataset("test/source", root=source)
    other = read_episode_table(source, ds.meta, 0)
    trimmed = read_episode_table(source, ds.meta, 1)
    assert other["action"].to_pylist() == [[0.0] * 17, [1.0] * 17]
    assert trimmed["action"].to_pylist() == [[1.0] * 17, [2.0] * 17]
    assert trimmed["frame_index"].to_pylist() == [0, 1]
    assert trimmed["index"].to_pylist() == [2, 3]
    assert trimmed["timestamp"].to_pylist() == pytest.approx([0, 0.1])
    assert not (static / "csv/episode_000001_ds1.csv").exists()
    assert agent.execute_job(job) == result  # Retry does not trim twice.


def test_chart_zoom_and_remote_trim_submission():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    text = (Path(viewer.__file__).parent / "templates/visualize_dataset_template.html").read_text()
    methods = []
    for name in (
        "setChartWindow",
        "focusChart",
        "resetChartZoom",
        "extendGraphToDuration",
        "applyTrim",
        "saveTrimAnnotation",
    ):
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", text, re.MULTILINE)
        end = re.search(r"^                \},", text[match.end() :], re.MULTILINE)
        methods.append(text[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
(async () => {
    function graph() { return {range: [0, 10], updates: [], xAxisRange() {return this.range;},
        xAxisExtremes() {return [0, 10];}, updateOptions(options) {
            this.updates.push(options); if(options.dateWindow) this.range = options.dateWindow;
        }}; }
    Object.assign(app, {dygraphArmJoints: graph(), dygraphGripperFlag: graph(), fps: 10, episodeLength: 100,
        dygraphTime: 5, baseGraphData: [[0, 2], [0.2, 3], [9.9, 4]]});
    app.focusChart();
    assert.deepEqual(app.dygraphArmJoints.range, [2.5, 7.5]);
    assert.deepEqual(app.dygraphGripperFlag.range, [2.5, 7.5]);
    app.extendGraphToDuration(10.2);
    assert.deepEqual(app.dygraphArmJoints.range, [2.5, 7.5]);
    assert.deepEqual(app.baseGraphData, [[0, 2], [0.2, 3], [9.9, 4]]);
    assert.ok(app.dygraphArmJoints.updates.every(update => !('file' in update)));
    app.resetChartZoom();
    assert.deepEqual(app.dygraphArmJoints.range, [0, 10.2]);
    assert.deepEqual(app.dygraphGripperFlag.range, [0, 10.2]);
    const alerts = []; global.alert = value => alerts.push(value);
    global.confirm = () => true;
    global.prompt = text => text.startsWith('Reason') ? 'Idle frames' : 'TRIM 7';
    global.window = {location: {href: ''}};
    let sent;
    global.fetch = async (url, options) => { sent = {url, body: JSON.parse(options.body)};
        return {ok: true, json: async () => ({job: {job_id: 'trim'}})}; };
    Object.assign(app, {adminModeEnabled: false, remoteDeleteUrl: '/mutation', remoteDatasetKey: 'node/data',
        currentEpisodeId: 2, trimStart: 3, trimEnd: 5, maxTrimFrame: () => 9});
    await app.applyTrim();
    assert.equal(sent.url, '/mutation');
    assert.equal(sent.body.op, 'trim_episode');
    assert.deepEqual(sent.body.options, {episode_id: 2, start_frame: 3, end_frame: 5, reason: 'Idle frames'});
    assert.equal(sent.body.confirmation, 'MUTATE node/data');
    assert.equal(window.location.href, '/?page=runs');
    app.trimApplying = false; window.location.href = '';
    global.fetch = async () => ({ok: false, status: 409, json: async () => ({error: 'Upgrade Agent'})});
    await app.applyTrim();
    assert.equal(app.trimApplying, false);
    assert.match(alerts.at(-1), /Upgrade Agent/);
    assert.equal(window.location.href, '');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_empty_chart_highlight_does_not_clear_playback_time():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    text = (Path(viewer.__file__).parent / "templates/visualize_dataset_template.html").read_text()
    body = text.split("highlightCallback: (event, x, points, row, seriesName) => {", 1)[1].split(
        "                        },", 1
    )[0]
    script = "const assert = require('node:assert/strict'); let selected; const syncSelection = row => selected = row;"
    script += "const app = {dygraphTime: 5, updateTableValues(value) {this.tableTime = value;}};"
    script += "const highlight = function(event, x, points, row, seriesName) {" + body + "};"
    script += "highlight.call(app, null, null, [], -1); assert.equal(app.dygraphTime, 5);"
    script += (
        "highlight.call(app, null, 2, [], 20); assert.equal(app.dygraphTime, 2); assert.equal(selected, 20);"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("v3", [False, True])
def test_local_trim_applies_displayed_range_without_waiting_for_autosave(tmp_path, v3):
    from lerobot.data_platform.precompute.dataset_io import read_episode_table

    source = tmp_path / "source"
    _make_dataset(source)
    _add_v21_stat_counts(source)
    if v3:
        source = run_convert_v3(source, tmp_path / "v3").out_root
    static = tmp_path / "cache/static"
    static.mkdir(parents=True)
    app = viewer.run_server(
        dataset=viewer.MetaOnlyDataset("local/test", root=source),
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
        annotate=True,
        legacy_mutations_enabled=True,
        start_server=False,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    assert (
        client.post(
            "/api/admin/setup",
            json={
                "password": "trim-admin-password",
                "confirm_password": "trim-admin-password",
            },
        ).status_code
        == 200
    )
    response = client.post(
        "/local/test/trim_merge",
        json={
            "episode_id": 1,
            "trim_start_frame": 1,
            "trim_end_frame": 2,
            "reason": "Remove idle frame",
        },
        buffered=True,
    )
    assert response.status_code == 200
    assert b"event: done" in response.data, response.data
    meta = viewer.MetaOnlyDataset("local/test", root=source).meta
    assert read_episode_table(source, meta, 1)["action"].to_pylist() == [[1.0] * 17, [2.0] * 17]
    assert read_episode_table(source, meta, 0).num_rows == 2
