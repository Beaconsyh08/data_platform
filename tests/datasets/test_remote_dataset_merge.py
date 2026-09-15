import csv
import io
import json
import re
import shutil
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from lerobot.data_platform.precompute.dataset_io import V3DatasetMetadata, read_episode_table
from lerobot.data_platform.precompute.preprocess.dataset_version import run_convert_v3
from tests.datasets.test_control_plane import _app, _bootstrap, _store
from tests.datasets.test_data_platform_agent import _agent, _FakeClient
from tests.datasets.test_dataset_merge_alignment import _make_named_dataset, _snapshot


def _remote_setup(tmp_path: Path, *, v3: bool = False):
    roots = [tmp_path / "datasets/a", tmp_path / "datasets/b"]
    _make_named_dataset(roots[0], ["left", "right"], ["grip"])
    _make_named_dataset(roots[1], ["right", "body", "left"], ["grip"], offset=100)
    if v3:
        roots[1] = run_convert_v3(roots[1], tmp_path / "datasets/b_v3", workers=1).out_root
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    assert _bootstrap(client).status_code == 200
    agent = _agent(tmp_path, tmp_path / "datasets", tmp_path / "datasets")
    token, node = store.enroll_node(
        name="server-b",
        hostname="server-b",
        allowed_roots=[str(tmp_path / "datasets")],
        writable_roots=[str(tmp_path / "datasets")],
        # This fixture exercises the legacy inline client; protocol 2 runs through
        # the subprocess supervisor covered by test_execution_supervisor.py.
        capabilities={**agent.capabilities(), "job_protocol": 1},
        enrollment_token="enroll",
        expected_token="enroll",
    )
    agent.state = replace(agent.state, node_id=node["node_id"], node_token=token)
    locations = store.sync_locations(
        node["node_id"], [{"dataset_key": f"node-server-b/{root.name}", "root": str(root)} for root in roots]
    )
    return store, client, agent, locations, roots


class _HttpClient(_FakeClient):
    def __init__(self, client, token):
        super().__init__()
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def event(self, state, job_id, message, payload=None):
        super().event(state, job_id, message, payload)
        response = self.client.post(
            f"/api/agents/jobs/{job_id}/events",
            headers=self.headers,
            json={"message": message, "payload": payload or {}},
        )
        assert response.status_code == 200

    def upload_artifact(self, state, job_id, relative_path, path, *, derived=False):
        super().upload_artifact(state, job_id, relative_path, path, derived=derived)
        scope = "derived-artifacts" if derived else "artifacts"
        response = self.client.put(
            f"/api/agents/jobs/{job_id}/{scope}/{Path(relative_path).as_posix()}",
            headers=self.headers,
            data=Path(path).read_bytes(),
        )
        assert response.status_code == 200


@pytest.mark.parametrize("policy", ["min", "pad"])
@pytest.mark.parametrize("role", ["admin", "operator"])
@pytest.mark.parametrize("dry_run,v3", [(False, False), (False, True), (True, False)])
def test_remote_min_merge_executes_and_registers_aligned_viewer(tmp_path: Path, dry_run, v3, role, policy):
    store, client, agent, locations, roots = _remote_setup(tmp_path, v3=v3)
    if role == "operator":
        store.register_user(
            username="merge-operator", password="operator-password", display_name="Operator", role=role
        )
        assert (
            client.post(
                "/api/auth/login", json={"username": "merge-operator", "password": "operator-password"}
            ).status_code
            == 200
        )
    source_ids = [location["location_id"] for location in locations]
    output = tmp_path / "datasets/merged"
    response = client.post(
        f"/api/control/locations/{source_ids[0]}/preprocess-jobs",
        json={
            "op": "merge",
            "options": {
                "source_location_ids": source_ids,
                "dimension_policy": policy,
                "workers": 1,
                "out_root": str(output),
                "dry_run": dry_run,
                "exclude_episodes": [[1], "1"],
            },
        },
    )
    assert response.status_code == 202
    agent.client = _HttpClient(client, agent.state.node_token)
    claimed = client.post("/api/agents/jobs/claim", headers=agent.client.headers, json={}).get_json()["job"]
    assert [item["root"] for item in claimed["options"]["_source_locations"]] == [str(root) for root in roots]
    before = [_snapshot(root) for root in roots]
    result = agent.execute_job(claimed)
    assert result["preprocess"]["summary"]["dimension_alignment"][1]["fields"]["action"][
        "source_indices"
    ] == ([2, 0] if policy == "min" else [0, 1, 2])
    completed = client.post(
        f"/api/agents/jobs/{claimed['job_id']}/complete",
        headers=agent.client.headers,
        json={"status": "done", "result": result},
    )
    assert completed.status_code == 200
    assert completed.get_json()["job"]["status"] == "done"
    assert [_snapshot(root) for root in roots] == before
    if dry_run:
        assert not output.exists()
        assert not agent.client.uploads
        assert len(store.list_locations()) == 2
        assert "dataset_location" not in result
        return
    table = (
        read_episode_table(output, V3DatasetMetadata("local/merged", output), 1)
        if v3
        else pq.read_table(output / "data/chunk-000/episode_000001.parquet")
    )
    assert table["action"].to_pylist() == (
        [[110, 120], [111, 121]] if policy == "min" else [[120, 190, 110], [121, 191, 111]]
    )
    assert table["index"].to_pylist() == [2, 3]
    assert result["dataset_location"]["metadata"]["derived_from_location_ids"] == source_ids
    assert result["dataset_location"]["metadata"]["data_version"] == "DVT2"
    uploads = {str(item[1]): item[2] for item in agent.client.uploads}
    row = next(csv.DictReader(io.StringIO(uploads["csv/episode_000001_ds1.csv"].decode())))
    assert float(row["left"]) == 110
    assert float(row["right"]) == 120
    assert ("body" in row) == (policy == "pad")
    assert agent.client.uploads[-1][1] == Path("viewer_manifest.json")
    assert all(item[3] for item in agent.client.uploads)
    final = completed.get_json()["job"]["result"]
    assert final["synced_location"]["metadata"]["viewer_ready"] is True
    assert final["viewer_url"].endswith("/episode_0")
    assert len(store.list_locations()) == 3


@pytest.mark.parametrize(
    "problem",
    [
        "cross_node",
        "duplicate",
        "missing",
        "raw_paths",
        "injected_locations",
        "output_on_second_source",
        "old_agent",
        "bad_policy",
    ],
)
def test_control_rejects_invalid_remote_merge_before_queueing(tmp_path: Path, problem):
    store, client, agent, locations, roots = _remote_setup(tmp_path)
    source_ids = [location["location_id"] for location in locations]
    options = {"source_location_ids": source_ids, "dimension_policy": "min"}
    if problem == "cross_node":
        _, other = store.enroll_node(
            name="other",
            hostname="other",
            allowed_roots=["/data"],
            writable_roots=["/data"],
            capabilities={},
            enrollment_token="x",
            expected_token="x",
        )
        source_ids[1] = store.sync_locations(
            other["node_id"], [{"dataset_key": "other/source", "root": "/data/source"}]
        )[0]["location_id"]
    elif problem == "duplicate":
        source_ids[1] = source_ids[0]
    elif problem == "missing":
        source_ids[1] = "missing"
    elif problem == "raw_paths":
        options["src_roots"] = [str(root) for root in roots]
    elif problem == "injected_locations":
        options["_source_locations"] = locations
    elif problem == "output_on_second_source":
        options["out_root"] = str(roots[1] / "inside")
    elif problem == "old_agent":
        store.heartbeat(agent.state.node_id, capabilities={"operations": ["preprocess.split"]})
    elif problem == "bad_policy":
        options["dimension_policy"] = "truncate"
    response = client.post(
        f"/api/control/locations/{source_ids[0]}/preprocess-jobs", json={"op": "merge", "options": options}
    )
    assert response.status_code == (404 if problem == "missing" else 409 if problem == "old_agent" else 400)
    assert not store.list_jobs()


@pytest.mark.parametrize("problem", ["outside_root", "other_node", "unsafe_output", "raw_paths"])
def test_agent_independently_revalidates_merge_paths_and_locations(tmp_path: Path, problem):
    store, client, agent, locations, roots = _remote_setup(tmp_path)
    source_ids = [location["location_id"] for location in locations]
    job = {
        "job_id": "test",
        "operation": "preprocess.merge",
        "location_id": source_ids[0],
        "location": locations[0],
        "options": {
            "source_location_ids": source_ids,
            "_source_locations": deepcopy(locations),
            "dimension_policy": "min",
        },
    }
    if problem == "outside_root":
        job["options"]["_source_locations"][1]["root"] = str(tmp_path / "outside")
    elif problem == "other_node":
        job["options"]["_source_locations"][1]["node_id"] = "other"
    elif problem == "unsafe_output":
        job["options"]["out_root"] = str(roots[1] / "inside")
    else:
        job["options"]["src_roots"] = [str(root) for root in roots]
    snapshots = [_snapshot(root) for root in roots]
    with pytest.raises((PermissionError, ValueError)):
        agent.execute_job(job)
    assert [_snapshot(root) for root in roots] == snapshots
    assert not agent.client.uploads


def test_remote_merge_form_filters_sources_and_forwards_alignment_options():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (
        Path(__file__).parents[2] / "lerobot/data_platform/templates/visualize_dataset_homepage.html"
    ).read_text()
    methods = []
    for name in (
        "operationAvailable",
        "operationReason",
        "remoteMergeAvailable",
        "mergeCandidates",
        "mergeSelectionDisabledReason",
        "mergeOutputError",
        "splitMergeDisabledReason",
        "canStartSplitMerge",
        "startSplitMerge",
    ):
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match is not None
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        assert end is not None
        methods.append(template[match.start() : match.end() + end.end()])
    script = (
        "const assert = require('node:assert/strict');\nconst app = {"
        + "\n".join(methods)
        + "};\n"
        + r"""
        Object.assign(app, {
            preprocessCapabilities: {available: false, error: 'local dependencies missing'},
            selectedDataset: {remote: true, operation_capabilities: {merge: {available: false, reason: 'Unrecognized UMI signal layout'}}}, selectedRemoteLocation: {node_id: 'b'},
            remoteLocations: [
                {node_id: 'b', location_id: 'first', state: 'available'},
                {node_id: 'b', location_id: 'second', state: 'available'},
                {node_id: 'c', location_id: 'other-node', state: 'available'},
                {node_id: 'b', location_id: 'missing', state: 'missing'},
            ],
            canOperateRemote: () => true,
            selectedRemoteNode: () => ({writable_roots: ['/data'], capabilities: {operations: ['preprocess.merge']}}),
            remoteDatasetRecord: item => ({key: item.location_id, remote_location_id: item.location_id, operation_capabilities: item.operation_capabilities}),
            mergeDeleteEpisodesPayload: () => ({second: '1,3'}),
            mergeAlignmentOptions: () => ({}),
            preprocess: {split_merge_op: 'merge', merge_src_keys: ['second', 'first'],
                merge_dimension_policy: 'min', merge_workers: 3, merge_out_root: '', merge_dry_run: true},
            submitRemoteJob: async (url, payload) => { app.submitted = {url, payload}; },
        });
        (async () => {
            assert.deepEqual(app.mergeCandidates().map(item => item.key), ['first', 'second']);
            assert.equal(app.canStartSplitMerge(), true);
            await app.startSplitMerge();
            assert.equal(app.submitted.url, '/api/control/locations/second/preprocess-jobs');
            assert.deepEqual(app.submitted.payload.options.source_location_ids, ['second', 'first']);
            assert.equal(app.submitted.payload.options.dimension_policy, 'min');
            assert.deepEqual(app.submitted.payload.options.exclude_episodes, ['1,3', []]);
            app.remoteLocations[0].operation_capabilities = {merge: {available: false, reason: 'Unsupported source layout'}};
            assert.equal(app.canStartSplitMerge(), false);
            assert.match(app.splitMergeDisabledReason(), /first: Unsupported source layout/);
            delete app.remoteLocations[0].operation_capabilities;
            app.preprocess.merge_src_keys = ['first', 'other-node'];
            assert.equal(app.canStartSplitMerge(), false);
            assert.match(app.splitMergeDisabledReason(), /same Agent/);
            app.preprocess.merge_src_keys = ['first'];
            assert.match(app.splitMergeDisabledReason(), /at least two/);
            app.selectedDataset = {remote: false};
            assert.match(app.splitMergeDisabledReason(), /local dependencies missing/);
            app.preprocessCapabilities = {available: true};
            app.selectedDataset.operation_capabilities = {merge: {available: false}};
            app.datasets = [{key: 'first'}, {key: 'second'}];
            app.preprocess.merge_src_keys = ['first', 'second'];
            assert.equal(app.canStartSplitMerge(), true);
            app.datasets[1].operation_capabilities = {merge: {available: false, reason: 'Unsupported source layout'}};
            assert.equal(app.canStartSplitMerge(), false);
            assert.match(app.splitMergeDisabledReason(), /second: Unsupported source layout/);
            console.log(JSON.stringify(app.submitted.payload));
        })().catch(error => { console.error(error); process.exit(1); });
    """
    )
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout)["op"] == "merge"


def test_job_submission_feedback_reports_acceptance_errors_and_uncertain_delivery():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (
        Path(__file__).parents[2] / "lerobot/data_platform/templates/visualize_dataset_homepage.html"
    ).read_text()
    match = re.search(r"^                async requestJson\([^\n]*\) \{", template, re.MULTILINE)
    assert match is not None
    end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
    assert end is not None
    method = template[match.start() : match.end() + end.end()]
    script = "const assert = require('node:assert/strict'); const app = {" + method + "};\n"
    script += r"""
    global.window = {AbortController, setTimeout, clearTimeout};
    app.requestTimeoutMs = 1000;
    (async () => {
        global.fetch = async () => ({ok: true, text: async () => JSON.stringify({job: {
            job_id: 'merge-123', status: 'queued'
        }})});
        await app.requestJson('/api/control/locations/source/preprocess-jobs', {op: 'merge'});
        assert.match(app.submissionNotice, /Task accepted: merge-123.*queued/);
        global.fetch = async () => ({ok: false, status: 403,
            text: async () => JSON.stringify({error: 'operator not permitted'})});
        await assert.rejects(app.requestJson('/api/test', {}), /operator not permitted/);
        assert.match(app.error, /operator not permitted/);
        assert.equal(app.submissionNotice, '');
        global.fetch = async () => { throw new TypeError('Failed to fetch'); };
        await assert.rejects(app.requestJson('/api/test', {}), /Check Runs before retrying/);
        assert.match(app.error, /may have received/);
        global.fetch = async () => { const e = new Error('aborted'); e.name = 'AbortError'; throw e; };
        await assert.rejects(app.requestJson('/api/test', {}), /Check Runs before retrying/);
    })().catch(error => { console.error(error); process.exit(1); });
    """
    subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)


def test_operator_deletion_ui_submits_request_without_starting_mutation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (
        Path(__file__).parents[2] / "lerobot/data_platform/templates/visualize_dataset_homepage.html"
    ).read_text()
    methods = []
    for name in ("canRequestEpisodeDeletion", "startRemoteMutation"):
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match is not None
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        assert end is not None
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
    global.window = {confirm: () => true};
    Object.assign(app, {
        selectedDataset: {remote: true}, selectedRemoteLocation: {location_id: 'source'},
        controlPlaneUser: {role: 'operator'},
        requestJson: async (url, payload) => { app.request = {url, payload}; },
        loadDeletionRequests: async () => {},
        submitRemoteJob: () => { throw new Error('Operator must not submit mutation directly'); },
        confirmRemoteMutation: () => { throw new Error('Operator must request approval'); },
    });
    (async () => {
        await app.startRemoteMutation('delete_episodes', {episodes: [1, 3], reason: 'bad data'}, '');
        assert.equal(app.request.url, '/api/control/locations/source/episode-deletion-requests');
        assert.deepEqual(app.request.payload.episodes, [1, 3]);
        assert.equal(app.busy, false);
        app.controlPlaneUser.role = 'viewer';
        assert.equal(app.canRequestEpisodeDeletion(), false);
    })().catch(error => { console.error(error); process.exit(1); });
    """
    subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)


def test_prefix_tail_mapping_ui_preserves_source_order_and_requires_confirmation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (
        Path(__file__).parents[2] / "lerobot/data_platform/templates/visualize_dataset_homepage.html"
    ).read_text()
    methods = []
    for name in (
        "mergeMappingRows",
        "setMergeDimensionNames",
        "mergeMappingSignature",
        "applyMergeLayoutPreset",
        "mergeAlignmentOptions",
        "mergeAlignmentPreview",
    ):
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
    const feature = dimension => ({action: {shape: [dimension], names: ['actions']}, state: {shape: [dimension], names: ['state']}});
    Object.assign(app, {
        mergeCandidates: () => [{key: 'wide', features: feature(20)}, {key: 'short', features: feature(17)}],
        mergeDatasetLabel: key => key,
        preprocess: {merge_src_keys: ['short', 'wide'], merge_dimension_policy: 'pad', merge_mapping_mode: 'explicit',
            merge_prefix_dims: 16, merge_tail_dims: 1, merge_padding_value: 0, merge_dimension_names: {}, merge_mapping_confirmation: ''}
    });
    app.applyMergeLayoutPreset();
    assert.throws(() => app.mergeAlignmentOptions(), /confirm/);
    app.preprocess.merge_mapping_confirmation = app.mergeMappingSignature();
    let options = app.mergeAlignmentOptions();
    assert.equal(options.dimension_names[0].action.length, 17);
    assert.equal(options.dimension_names[1].action.length, 20);
    assert.equal(options.dimension_names[0].action[16], options.dimension_names[1].action[19]);
    assert.match(app.mergeAlignmentPreview()[0].text, /17→20D/);
    assert.match(app.mergeAlignmentPreview()[0].text, /17←fill\(0\), 18←fill\(0\), 19←fill\(0\), 20←17/);
    app.preprocess.merge_dimension_policy = 'min';
    assert.match(app.mergeAlignmentPreview().find(row => row.id === 'wide:action').text, /drop 17, 18, 19/);
    app.preprocess.merge_src_keys.reverse();
    assert.throws(() => app.mergeAlignmentOptions(), /confirm/);
    app.preprocess.merge_mapping_confirmation = app.mergeMappingSignature();
    assert.equal(app.mergeAlignmentOptions().dimension_names[0].action.length, 20);
    app.setMergeDimensionNames('short', 'state', 'x');
    assert.throws(() => app.mergeAlignmentOptions(), /confirm/);
    app.preprocess.merge_mapping_confirmation = app.mergeMappingSignature();
    assert.throws(() => app.mergeAlignmentOptions(), /17 unique names/);
    """
    subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)


@pytest.mark.parametrize(
    "policy,mappings",
    [("pad", None), ("min", [{"action": ["left", "right"]}, {"action": ["right", "body", "left"]}])],
)
def test_new_alignment_options_require_agent_capability(tmp_path, policy, mappings):
    from lerobot.data_platform.control_plane import ControlPlaneNode

    store, client, agent, locations, _ = _remote_setup(tmp_path)
    with store.sessions.begin() as session:
        node = session.get(ControlPlaneNode, agent.state.node_id)
        node.capabilities = {
            key: value for key, value in node.capabilities.items() if key != "merge_alignment_protocol"
        }
    response = client.post(
        f"/api/control/locations/{locations[0]['location_id']}/preprocess-jobs",
        json={
            "op": "merge",
            "options": {
                "source_location_ids": [item["location_id"] for item in locations],
                "dimension_policy": policy,
                "dimension_names": mappings,
            },
        },
    )
    assert response.status_code == 409
    assert "Upgrade" in response.get_json()["error"]
    assert store.list_jobs() == []


def test_merge_output_prefill_and_path_validation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the homepage behavior check")
    template = (
        Path(__file__).parents[2] / "lerobot/data_platform/templates/visualize_dataset_homepage.html"
    ).read_text()
    methods = []
    for name in ("prefillMergeOutput", "mergeOutputError"):
        match = re.search(rf"^                {name}\([^\n]*\) \{{", template, re.MULTILINE)
        assert match
        end = re.search(r"^                \},", template[match.end() :], re.MULTILINE)
        methods.append(template[match.start() : match.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    script += r"""
    Object.assign(app, {
        selectedDataset: {remote: true},
        selectedRemoteNode: () => ({writable_roots: ['/allowed/output/']}),
        mergeCandidates: () => [{key: 'a', root: '/allowed/output/source'}],
        preprocess: {merge_out_root: '', merge_src_keys: ['a']}
    });
    app.prefillMergeOutput();
    assert.match(app.preprocess.merge_out_root, /^\/allowed\/output\/merged_\d+$/);
    assert.equal(app.mergeOutputError(), '');
    for (const [path, reason] of [
        ['/allowed/output-other/new', /writable roots/],
        ['/allowed/output/../outside', /parent-directory/],
        ['relative/path', /absolute path/],
        ['/allowed/output', /new dataset folder/],
        ['/allowed/output/source/inside', /separate/],
        ['/allowed/output/source', /separate/]
    ]) {
        app.preprocess.merge_out_root = path;
        assert.match(app.mergeOutputError(), reason);
    }
    app.preprocess.merge_out_root = '/allowed/output/custom';
    app.prefillMergeOutput();
    assert.equal(app.preprocess.merge_out_root, '/allowed/output/custom');
    assert.equal(app.mergeOutputError(), '');
    app.selectedRemoteNode = () => ({writable_roots: []});
    assert.match(app.mergeOutputError(), /no writable/);
    app.selectedDataset.remote = false;
    app.preprocess.merge_out_root = 'merged';
    assert.equal(app.mergeOutputError(), '');
    """
    subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
