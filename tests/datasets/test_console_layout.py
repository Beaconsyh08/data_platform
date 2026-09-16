from pathlib import Path

import pytest

from lerobot.data_platform import viewer


@pytest.mark.parametrize("page", ["visualize_dataset_homepage.html", "data_platform_control_plane.html"])
def test_remote_lists_load_independently_when_jobs_fail(page):
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the page behavior check")
    template = (Path(viewer.__file__).parent / "templates" / page).read_text()
    method = "loadRemoteSources" if page.startswith("visualize") else "load"
    match = re.search(rf"^                async {method}\(\) \{{", template, re.MULTILINE)
    assert match is not None
    end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
    assert end is not None
    source = template[match.start() : match.end() + end.end()]
    script = "const assert = require('node:assert/strict'); const app = {" + source + "};\n"
    script += f"const method = '{method}';\n"
    script += r"""
(async () => {
    Object.assign(app, {controlPlaneEnabled: true, isAdmin: false, refreshing: false,
        remoteJobs: [{job_id: 'old', events: ['saved']}], jobs: [{job_id: 'old'}],
        controlPlaneNodes: [], nodes: [], remoteLocations: [], locations: [],
        selectedRemoteLocation: null, selectedDataset: null, selectedNodeId: '', jobPage: 1,
        loadDeletionRequests: async () => {}, syncRemoteViewerJob: () => {},
        mergeRemoteJobsIntoRuns: () => {}, clampLocationPage: () => {}, jobPageCount: () => 1});
    let failing = 'jobs';
    app.requestJson = app.request = async url => {
        if (failing && url.includes(failing)) throw new Error('HTTP 500: task database unavailable');
        return url.includes('nodes') ? {nodes: [{node_id: 'h100-05'}]}
            : url.includes('locations') ? {locations: [{location_id: 'dataset'}]}
            : {jobs: [{job_id: 'old', status: 'done'}]};
    };
    const homepage = method === 'loadRemoteSources';
    const nodesKey = homepage ? 'controlPlaneNodes' : 'nodes';
    const locationsKey = homepage ? 'remoteLocations' : 'locations';
    const jobsKey = homepage ? 'remoteJobs' : 'jobs';
    const errorKey = homepage ? 'remoteError' : 'error';
    await app[method]();
    assert.equal(app[nodesKey][0].node_id, 'h100-05');
    assert.equal(app[locationsKey][0].location_id, 'dataset');
    assert.equal(app[jobsKey][0].job_id, 'old');
    assert.match(app[errorKey], /Tasks:.*HTTP 500/);
    failing = '';
    await app[method]();
    assert.equal(app[jobsKey][0].status, 'done');
    if (homepage) assert.deepEqual(app.remoteJobs[0].events, ['saved']);
    assert.equal(app[errorKey], '');
    failing = 'nodes';
    await app[method]();
    assert.equal(app[nodesKey][0].node_id, 'h100-05');
    assert.equal(app[locationsKey][0].location_id, 'dataset');
    assert.match(app[errorKey], /Nodes:/);
    assert.equal(app.refreshing, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("robot", ["umi", "dvt1", "dvt2", "joint_named", "ee_named", "ee_positional"])
def test_viewer_signal_groups_remain_visible_after_refresh_and_toggles(robot):
    import json
    import re
    import shutil
    import subprocess

    from lerobot.data_platform.precompute.signal_columns import signal_columns
    from lerobot.data_platform.precompute.viewer_signals import viewer_signal_presentation

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the viewer behavior check")
    template = (Path(viewer.__file__).parent / "templates/visualize_dataset_template.html").read_text()
    if robot == "umi":
        features = {
            f"{part}_{kind}": {"dtype": "float32", "shape": [len(names)], "names": names}
            for part in ("head", "left_arm", "right_arm")
            for kind, names in (
                ("pose", ["x", "y", "z", "roll", "pitch", "yaw"]),
                ("quaternion_pose", ["x", "y", "z", "qw", "qx", "qy", "qz"]),
            )
        }
        features.update(
            {key: {"dtype": "float32", "shape": [1]} for key in ("left_gripper_pos", "right_gripper_pos")}
        )
    elif robot.startswith("ee_"):
        names = [
            f"{part}_{component}"
            for part in ("left", "right", "head")
            for component in (
                ["x", "y", "z", "qw", "qx", "qy", "qz", "gripper"]
                if part != "head"
                else ["x", "y", "z", "qw", "qx", "qy", "qz"]
            )
        ]
        features = {key: {"dtype": "float32", "shape": [23], "names": names} for key in ("state", "action")}
    elif robot == "joint_named":
        features = {
            key: {"dtype": "float32", "shape": [dim], "names": [f"index_{i + 1}" for i in range(dim)]}
            for key, dim in (("state", 19), ("action", 20))
        }
    else:
        features = {
            key: {"dtype": "float32", "shape": [19 if robot == "dvt2" else 17]} for key in ("state", "action")
        }
    columns = signal_columns(features, qualify=robot == "umi")
    if robot == "ee_positional":
        columns = [{"key": key, "value": [f"{key}_{i}" for i in range(23)]} for key in ("state", "action")]
    presentation = viewer_signal_presentation(
        features, columns, "UMI-GripperBody-Head" if robot.startswith("ee_") else "h10_w"
    )
    # Exercise the actual initialization and visibility code, including cache-only CSV labels.
    setup = template.split('                    const labels = ["timestamp",', 1)[1]
    setup = 'const labels = ["timestamp",' + setup.split("                    const syncSelection", 1)[0]
    methods = []
    for name in (
        "getSeriesLabels",
        "applyVisibility",
        "toggleGroup",
        "auxiliaryRows",
        "auxiliaryGroups",
        "signalShortLabel",
        "umiSignalColor",
        "stateActionRows",
    ):
        match = re.search(rf"^                (?:get )?{name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match is not None
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        assert end is not None
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += f"app.signalColumns = {json.dumps(columns)}; const robot = {json.dumps(robot)};\n"
    script += f"app.signalPresentation = {json.dumps(presentation)};\n"
    script += r"""
app.columns = app.signalColumns.flatMap(c => c.value.map(label => ({key: label, value: [label]})));
if (robot !== 'umi') app.columns = app.signalColumns.slice();
app.columns.push({key: 'stage', value: ['stage']});
app.hasBodyJoints = ['dvt2', 'joint_named'].includes(robot);
app.hasLegacyFlag = robot === 'dvt1';
"""
    script += "(function() {" + setup + "}).call(app);\n"
    script += r"""
const labels = app.labelsNoTime;
const graph = () => ({getLabels: () => ['timestamp', ...labels],
    setVisibility(mask) { this.mask = mask; }});
app.dygraphArmJoints = graph();
app.dygraphGripperFlag = graph();
app.currentFrameData = labels.map(label => ({label, checked: true}));
app.applyVisibility();
assert.ok(app.dygraphArmJoints.mask.some(Boolean), 'Pose/joint graph must contain visible curves');
assert.deepEqual(app.dygraphArmJoints.mask, app.armJointMask);
assert.deepEqual(app.dygraphGripperFlag.mask, app.gripperFlagMask);
for (let i = 0; i < labels.length; i++) {
    if (robot === 'umi' && /_quaternion_pose\.[xyz]$/.test(labels[i])) {
        assert.equal(app.dygraphArmJoints.mask[i], false);
        assert.equal(app.dygraphGripperFlag.mask[i], false);
    } else {
        assert.notEqual(app.dygraphArmJoints.mask[i], app.dygraphGripperFlag.mask[i], labels[i]);
    }
}
if (robot === 'umi') {
    assert.equal(app.usesUmiPlotGroups, true);
    assert.equal(app.jointGroups.filter(g => g.graph === 'arm').length, 6);
    assert.deepEqual(app.jointGroups.filter(g => g.graph === 'arm').map(g => g.shortLabel),
        ['Head3', 'Head4', 'Left3', 'Left4', 'Right3', 'Right4']);
    assert.equal(app.dygraphArmJoints.mask.filter(Boolean).length, 30);
    assert.equal(app.dygraphGripperFlag.mask.filter(Boolean).length, 3);
    const groups = app.auxiliaryGroups;
    assert.deepEqual(groups.map(g => g.key), ['head', 'left', 'right', 'other']);
    for (const group of groups.slice(0, 3)) {
        const shortLabels = group.cells.map(c => app.signalShortLabel(c.label));
        assert.deepEqual(shortLabels.slice(0, 10), ['x', 'y', 'z', 'roll', 'pitch', 'yaw', 'qw', 'qx', 'qy', 'qz']);
        assert.equal(new Set(shortLabels).size, shortLabels.length);
    }
    assert.equal(new Set(labels.filter((label, i) => app.armJointMask[i]).map(label => app.umiSignalColor(label))).size, 30);
    app.toggleGroup('head_rpy');
    assert.equal(app.dygraphArmJoints.mask[labels.indexOf('head_pose.x')], true);
    assert.equal(app.dygraphArmJoints.mask[labels.indexOf('head_pose.roll')], false);
    app.toggleGroup('head_quat');
    assert.equal(app.dygraphArmJoints.mask[labels.indexOf('head_pose.x')], false);
    app.toggleGroup('head_rpy');
    app.toggleGroup('head_quat');
}
if (robot === 'joint_named') {
    assert.equal(app.usesUmiPlotGroups, false);
    assert.equal(app.stateActionRows.length, 20);
    assert.equal(app.stateActionRows[0].stateCell.label, 'state.index_1');
    assert.equal(app.stateActionRows[19].stateCell, null);
    assert.equal(app.stateActionRows[19].actionCell.label, 'index_20');
    assert.deepEqual(app.labelToGroupIds['state.index_8'], ['gripper_flag']);
    assert.deepEqual(app.labelToGroupIds['state.index_17'], ['body']);
    assert.deepEqual(app.labelToGroupIds['index_20'], ['other_signals']);
    assert.equal(app.auxiliaryRows.length, 1); // Stage; vector entries belong to the paired table.
}
if (robot.startsWith('ee_')) {
    assert.equal(app.usesUmiPlotGroups, true);
    assert.deepEqual(app.jointGroups.filter(g => g.graph === 'arm').map(g => g.shortLabel), ['Head4', 'Left4', 'Right4']);
    assert.equal(app.dygraphArmJoints.mask.filter(Boolean).length, 42);
    assert.equal(app.dygraphGripperFlag.mask.filter(Boolean).length, 5);
    assert.equal(app.stateActionRows.length, 0);
    const left = app.auxiliaryGroups.find(g => g.key === 'left');
    assert.equal(left.cells.length, 16);
    assert.ok(left.cells.some(c => app.signalShortLabel(c.label) === 'qw · State'));
    assert.ok(left.cells.some(c => app.signalShortLabel(c.label) === 'qw · Action'));
}
for (const group of app.jointGroups) {
    app.toggleGroup(group.id);
    for (let i = 0; i < labels.length; i++) {
        if ((app.labelToGroupIds[labels[i]] || []).includes(group.id)) {
            const remaining = app.jointGroups.filter(g => g.enabled && app.labelToGroupIds[labels[i]].includes(g.id));
            assert.equal(app.dygraphArmJoints.mask[i], remaining.some(g => g.graph === 'arm'));
            assert.equal(app.dygraphGripperFlag.mask[i], remaining.some(g => g.graph === 'gripper'));
        }
    }
    app.toggleGroup(group.id);
}
app.currentFrameData[0].checked = false;
app.applyVisibility();
assert.equal(app.dygraphArmJoints.mask[0], false);
app.currentFrameData[0].checked = true;
app.applyVisibility();
assert.deepEqual(app.dygraphArmJoints.mask, app.armJointMask);
assert.deepEqual(app.dygraphGripperFlag.mask, app.gripperFlagMask);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_viewer_signal_labels_are_english():
    import re

    for path in (Path(viewer.__file__).parent / "templates").rglob("*.html"):
        assert not re.search(r"[\u4e00-\u9fff]", path.read_text()), path.name


def _page_map(groups: list[dict]) -> dict[str, dict]:
    return {page["key"]: page for group in groups for page in group["pages"]}


def test_viewer_frame_keys_pause_seek_repeat_and_ignore_editing():
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the viewer behavior check")
    template = (Path(viewer.__file__).parent / "templates/visualize_dataset_template.html").read_text()
    methods = []
    for name in (
        "handleFrameKey",
        "stepFrame",
        "maxTrimFrame",
        "clampTrimFrame",
        "frameToTime",
        "timeToFrame",
        "timeToGraphRow",
    ):
        match = re.search(rf"^                {name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match is not None
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        assert end is not None
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
const video = () => ({currentTime: 1, duration: 2, readyState: 4, paused: false,
    pause() { this.paused = true; }});
app.fps = 30; app.episodeLength = 60; app.dygraphTime = 1;
app.videos = [video(), video(), video()]; app.video = app.videos[0];
app.$refs = {zoomVideo: video()}; app.isZoomOpen = true;
const graph = () => ({rawData_: [[0], [1], [2]], setSelection(row) { this.row = row; }});
app.dygraphArmJoints = graph(); app.dygraphGripperFlag = graph();
app.updateTableValues = time => { app.tableTime = time; };
app.updateImages = frame => { app.imageFrame = frame; };
app._updateCurrentStage = () => {};
function press(key, extra = {}) {
    const event = {key, preventDefault() { this.defaultPrevented = true; }, ...extra};
    app.handleFrameKey(event);
    return event;
}
assert.equal(press('d').defaultPrevented, true);
assert.equal(app.imageFrame, 31);
assert.equal(app.tableTime, 31 / 30);
assert.equal(app.dygraphArmJoints.row, 1); // Downsampled graph uses its timestamps, not frame index.
for (const v of [...app.videos, app.$refs.zoomVideo]) {
    assert.equal(v.paused, true); assert.equal(v.currentTime, 31 / 30);
}
assert.deepEqual(app._pausedForZoom, [true, true, true]);
for (let i = 0; i < 3; i++) press('D', {repeat: true});
assert.equal(app.imageFrame, 34);
press('A'); assert.equal(app.imageFrame, 33);
for (const extra of [{ctrlKey: true}, {altKey: true}, {metaKey: true}, {isComposing: true},
    {target: {isContentEditable: true}}, {target: {closest: () => ({})}}]) {
    assert.equal(press('a', extra).defaultPrevented, undefined);
    assert.equal(app.imageFrame, 33);
}
for (let i = 0; i < 100; i++) press('a', {repeat: true});
assert.equal(app.imageFrame, 0);
for (let i = 0; i < 100; i++) press('d', {repeat: true});
assert.equal(app.imageFrame, 59);
app.isZoomOpen = false; app.video = null; app.videos = []; app.$refs = {};
press('a'); assert.equal(app.imageFrame, 58); // Image-only viewer still steps.
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_dataset_root_probe_skips_inaccessible_path(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "blocked"
    candidate.mkdir()
    blocked_info = candidate / "meta" / "info.json"
    original_is_file = Path.is_file

    def guarded_is_file(path: Path) -> bool:
        if path == blocked_info:
            raise PermissionError(path)
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)

    assert viewer._is_dataset_root(candidate) is False


def test_full_console_navigation_uses_workspace_page_tab_hierarchy():
    groups = viewer._console_groups_for_tabs(
        viewer._CONSOLE_MODE_ALLOWED_TABS[viewer.CONSOLE_MODE_FULL],
        allowed_open_links=viewer._CONSOLE_MODE_ALLOWED_OPEN_LINKS[viewer.CONSOLE_MODE_FULL],
        legacy_mutations_enabled=False,
    )

    assert [group["key"] for group in groups] == ["data_platform", "data_curation"]
    pages = _page_map(groups)
    assert list(pages) == [
        "datasets",
        "preprocessing",
        "versions",
        "runs",
        "explore",
        "quality",
        "annotation",
        "dataset_build",
    ]
    assert [tab["key"] for tab in pages["preprocessing"]["tabs"]] == [
        "cache",
        "standardize",
        "transform",
        "split_merge",
    ]
    assert [tab["key"] for tab in pages["explore"]["tabs"]] == [
        "explore_overview",
        "embedding",
        "compare",
    ]
    assert pages["explore"]["open_links"] == ["viewer", "analysis"]


def test_visualize_console_keeps_dataset_runs_explore_and_allowed_legacy_page():
    groups = viewer._console_groups_for_tabs(
        viewer._CONSOLE_MODE_ALLOWED_TABS[viewer.CONSOLE_MODE_VISUALIZE],
        allowed_open_links=viewer._CONSOLE_MODE_ALLOWED_OPEN_LINKS[viewer.CONSOLE_MODE_VISUALIZE],
        legacy_mutations_enabled=True,
    )

    pages = _page_map(groups)
    assert set(pages) == {
        "datasets",
        "preprocessing",
        "runs",
        "explore",
        "quality",
        "admin_operations",
    }
    assert [tab["key"] for tab in pages["preprocessing"]["tabs"]] == ["cache"]
    assert [tab["key"] for tab in pages["explore"]["tabs"]] == ["explore_overview"]
    assert [tab["key"] for tab in pages["admin_operations"]["tabs"]] == ["dataset_ops"]
    assert pages["explore"]["open_links"] == ["viewer", "analysis"]
    assert next(group for group in groups if group["key"] == "legacy_admin")["label"] == "Admin"


def test_homepage_uses_single_canvas_dataset_page_and_on_demand_job_drawer():
    template = (
        Path(__file__).parents[2]
        / "lerobot"
        / "data_platform"
        / "templates"
        / "visualize_dataset_homepage.html"
    ).read_text()

    assert "dp-console-grid" not in template
    assert "Job & Artifacts" not in template
    assert "dp-main-canvas" in template
    assert "x-show=\"activePage === 'datasets'\"" in template
    assert "x-show=\"activePage === 'runs'\"" in template
    assert 'x-show="jobDrawerOpen"' in template
    assert "Manage datasets" in template
    assert "View all runs" in template
    assert "params.set('page', this.activePage)" in template
    assert "initialPage: {{ initial_page|tojson }}" in template
    assert "Load selected" not in template
    assert "Load registered" not in template
    assert "Register / Load" not in template
    assert "datasetSelection" not in template
    assert 'x-show="dataset.cache_only"' not in template
    assert 'x-show="candidate.cache_only"' not in template
    assert "Mark root as source" in template
    assert "source protected" in template
    assert "Source Delivery" in template
    assert "Dataset Requirement" in template
    assert "Deterministic Data Recipe" in template
    assert "Training & Collection Feedback" not in template
    assert "Enter Admin" in template
    assert "Admin active" in template
    assert "Admin Mode enabled" in template
    assert "Administrator account" in template
    assert "controlPlaneUser" in template
    assert "'/api/auth/logout'" in template
    assert "dp-header-utilities" in template
    assert "adminMenuOpen" in template
    assert "Set administrator password" in template
    assert "/api/admin/setup" in template
    assert "/api/admin/login" in template
    assert "/api/admin/logout" in template
    assert "dataPlatform.adminModeUntil" not in template
    assert "legacyMutationsEnabled && adminModeEnabled" in template
    assert "Lifecycle stage" in template
    assert "source or original input" in template
    assert "preprocessing output" in template
    assert "reviewed construction or Manifest output" in template
    assert "datasetStageClass(item)" in template
    assert "datasetStageHint(item)" in template
    assert 'x-model="preprocess.delete_reason"' in template
    assert 'x-model="preprocess.flag_delete_reason"' in template


def test_homepage_uses_consistent_visual_hierarchy_and_context_states():
    template = (
        Path(__file__).parents[2]
        / "lerobot"
        / "data_platform"
        / "templates"
        / "visualize_dataset_homepage.html"
    ).read_text()

    assert "dp-app-header" in template
    assert "dp-page-surface" in template
    assert "dp-page-kicker" in template
    assert "dp-context-card" in template
    assert "dp-empty-state" in template
    assert "Working dataset" in template
    assert "...this.remoteLocations.map(location => this.remoteDatasetRecord(location))" in template
    assert "this.candidates.filter(item => !item.registered).map" in template
    assert "if (dataset?.remote)" in template
    assert "if (dataset?.local_candidate)" in template
    assert "this.storeDatasetKey(this.selectedDataset.key)" in template
    assert "options.overwrite_output = Boolean(this.preprocess.standardize_overwrite)" in template
    assert "if (outputRoot) options.out_root = outputRoot" in template
    assert "Registered datasets" not in template
    assert "Available datasets" not in template
    assert "datasetTab" not in template
    assert "candidateSelection" not in template
    assert "localRegisteredDatasets()" in template
    assert "localUncatalogedDatasets()" in template
    assert "await this.registerDataset(candidate.root)" in template
    assert "Catalog setup happens automatically." in template
    assert ">viewer</a>" in template
    assert ">prepare viewer</button>" in template
    assert "jobOperationLabel(job)" in template
    assert "jobOutputLabel(selectedJob())" in template
    assert "useJobOutput(selectedJob())" in template
    assert template.count('@click="prepareJobOutput(selectedJob())"') == 2
    assert "canPrepareJobOutput(job)" in template
    assert "jobViewerUrl(selectedJob())" in template
    assert "output_location_id" in template
    assert "Pipeline runs" in template
    assert "activeWorkspaceLabel()" in template
    assert "font-mono text-sm" not in template


def test_explore_overview_surfaces_visualizations_and_preparation_paths():
    template = (
        Path(__file__).parents[2]
        / "lerobot"
        / "data_platform"
        / "templates"
        / "visualize_dataset_homepage.html"
    ).read_text()

    assert "Visualization center" in template
    assert "visualizationItems()" in template
    assert "openVisualizationSetup(item.key)" in template
    for label in (
        "Episode Viewer",
        "Dataset Analysis",
        "Label Review",
        "Tag Review",
        "Construction Review",
        "Embedding Map",
        "Dataset Compare",
        "Smoothing Report",
    ):
        assert label in template
    assert ".filter(item => this.openLinkEnabled(item.key))" in template


def test_remote_dataset_can_open_curation_explore_without_exposing_unsupported_tools():
    template = (
        Path(__file__).parents[2]
        / "lerobot"
        / "data_platform"
        / "templates"
        / "visualize_dataset_homepage.html"
    ).read_text()

    assert "workspace.pages.filter(page => page.key === 'explore')" in template
    assert "page.tabs.filter(tab => tab.key === 'explore_overview')" in template
    assert "items.filter(item => ['viewer', 'analysis'].includes(item.key))" in template
    assert "await this.prepareRemoteViewer(this.selectedRemoteLocation)" in template
    assert "'runs', 'explore'].includes(pageKey)" in template
    assert "'dataset_ops', 'explore_overview'" in template


def test_robot_profile_and_viewer_signal_layout_are_presented_separately():
    templates_dir = Path(__file__).parents[2] / "lerobot" / "data_platform" / "templates"
    homepage = (templates_dir / "visualize_dataset_homepage.html").read_text()
    viewer_template = (templates_dir / "visualize_dataset_template.html").read_text()

    assert "DVT processing profile" in homepage
    assert "Output signal schema is Standard 16D" in homepage
    assert "hasBodyJoints" in viewer_template
    assert 'data_version == "DVT2"' not in viewer_template


def test_processing_defaults_and_umi_stage_form_behavior():
    import re
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (Path(viewer.__file__).parent / "templates/visualize_dataset_homepage.html").read_text()
    methods = []
    for name in (
        "operationAvailable",
        "operationReason",
        "normalizeDataVersion",
        "defaultProcessingDataVersion",
        "stagePolicyLabel",
        "applyDatasetDataVersion",
        "stageSubtaskOverrides",
        "startPrecompute",
    ):
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match is not None
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        assert end is not None
        methods.append(template[match.start() : match.end() + end.end()])
    script = (
        "const assert = require('node:assert/strict'); const app = {"
        + "\n".join(methods)
        + "};\n"
        + r"""
(async () => {
    app.options = {fallback_stage_count: 5};
    app.selectedDataset = {data_version: 'DVT1'};
    app.applyDatasetDataVersion(app.selectedDataset);
    assert.equal(app.options.data_version, 'DVT2');
    assert.match(app.stagePolicyLabel(), /DVT2/);
    app.options.data_version = 'DVT1';
    app.dataVersionManual = true;
    app.applyDatasetDataVersion(app.selectedDataset);
    assert.equal(app.options.data_version, 'DVT1');
    app.selectedDataset = {robot_type: 'UMI', data_version: null, remote: true,
        operation_capabilities: {auto_stage: {available: true}}};
    app.applyDatasetDataVersion(app.selectedDataset);
    assert.equal(app.options.data_version, null);
    assert.match(app.stagePolicyLabel(), /Equal time segments.*5 segments/);
    app.options.fallback_stage_count = 7;
    app.selectedRemoteLocation = {location_id: 'umi'};
    let sent;
    app.prepareRemoteViewer = async (location, options) => {sent = options;};
    await app.startPrecompute(app.stageSubtaskOverrides());
    assert.equal(sent.fallback_stage_count, 7);
    assert.equal(sent.force_recompute_stage, true);
    assert.equal(sent.prepare_videos, false);
    assert.equal(sent.data_version, null);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
