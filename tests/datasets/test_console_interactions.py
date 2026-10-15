"""Behavior checks for shared console navigation, context and task feedback."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lerobot.data_platform import viewer

TEMPLATES = Path(viewer.__file__).parent / "templates"


def _run_methods(names: list[str], script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser-script behavior checks")
    source = (TEMPLATES / "visualize_dataset_homepage.html").read_text()
    methods = []
    for name in names:
        match = re.search(rf"^                (?:async )?{name}\([^\n]*\) \{{", source, re.MULTILINE)
        assert match, name
        end = re.search(r"^                \},", source[match.end() :], re.MULTILINE)
        assert end, name
        methods.append(source[match.start() : match.end() + end.end()])
    code = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    code += "(async () => {\n" + script + "\n})().catch(e => {console.error(e);process.exitCode=1;});"
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_workspace_and_page_switches_restore_the_last_subtab():
    groups = viewer._console_groups_for_tabs(
        viewer._CONSOLE_MODE_ALLOWED_TABS[viewer.CONSOLE_MODE_FULL],
        allowed_open_links=viewer._CONSOLE_MODE_ALLOWED_OPEN_LINKS[viewer.CONSOLE_MODE_FULL],
        legacy_mutations_enabled=False,
    )
    _run_methods(
        [
            "selectWorkspace",
            "selectPage",
            "selectTab",
            "findPage",
            "pageForTab",
            "currentPage",
            "currentSubTabs",
        ],
        "const groups = "
        + json.dumps(groups)
        + ";\n"
        + """
Object.assign(app, {availableGroups: () => groups, tabEnabled: () => true,
    unifiedCurationActive: () => false, updateHomeUrl: () => {}, syncSchemaOpForTab: () => {},
    loadPreprocessCapabilities: async () => {}, activePage: 'datasets', activeTab: ''});
await app.selectTab('temporal_caption');
await app.selectWorkspace('data_platform');
assert.equal(app.activePage, 'datasets');
await app.selectTab('split_merge');
await app.selectWorkspace('data_curation');
assert.equal(app.activePage, 'annotation');
assert.equal(app.activeTab, 'temporal_caption');
await app.selectPage('quality');
await app.selectPage('annotation');
assert.equal(app.activeTab, 'temporal_caption');
await app.selectWorkspace('data_platform');
assert.equal(app.activePage, 'preprocessing');
assert.equal(app.activeTab, 'split_merge');
""",
    )


def test_scan_directory_choices_follow_server_and_allow_custom_subdirectories():
    _run_methods(
        [
            "knownScanPaths",
            "scanPathPreset",
            "selectScanPath",
            "selectedServerNode",
            "onDatasetServerChange",
            "onScanPathInput",
            "remotePathAllowed",
            "storeDatasetBrowser",
            "datasetBrowserStorageKey",
        ],
        """
let focused = false;
Object.assign(app, {datasetServerFilter:'local', rootDir:'/local',
    sourceRoots:['/local/', '/protected', '', '/'], scanPath:'/local/',
    controlPlaneNodes:[{node_id:'first', allowed_roots:['/data/team', '/data/shared/']},
        {node_id:'second', allowed_roots:['/other']}],
    remoteScanSerial:0, $nextTick:fn=>fn(), $refs:{customScanPath:{focus:()=>{focused=true;}}}});
assert.deepEqual(app.knownScanPaths(), ['/local', '/protected', '/']);
assert.equal(app.scanPathPreset(), '/local');
app.datasetServerFilter = 'first';
app.onDatasetServerChange();
assert.deepEqual(app.knownScanPaths(), ['/data/team', '/data/shared']);
assert.equal(app.scanPathPreset(), '/data/team');
app.selectScanPath('/data/shared');
assert.equal(app.scanPath, '/data/shared');
app.scanPath = '/data/shared/subdirectory';
assert.equal(app.scanPathPreset(), '');
app.remoteError = 'previous error';
app.selectScanPath('');
assert.equal(app.scanPath, '');
assert.equal(app.remoteError, '');
assert.equal(focused, true);
app.datasetServerFilter = 'second';
app.onDatasetServerChange();
assert.deepEqual(app.knownScanPaths(), ['/other']);
assert.equal(app.scanPath, '/other');
app.datasetServerFilter = 'missing';
app.onDatasetServerChange();
assert.deepEqual(app.knownScanPaths(), []);
assert.equal(app.scanPath, '');
app.datasetServerFilter = 'local';
app.onDatasetServerChange();
assert.equal(app.scanPath, '/local');
""",
    )


def test_dataset_directory_browsing_restores_without_scanning():
    _run_methods(
        [
            "datasetBrowserStorageKey",
            "storeDatasetBrowser",
            "restoreDatasetBrowser",
            "onScanPathInput",
            "selectScanPath",
            "remotePathAllowed",
            "selectedServerNode",
            "remoteLocationsForSelectedServer",
        ],
        """
const storage = new Map();
global.window = {localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}};
Object.assign(app, {controlPlaneUser:{user_id:'owner'},datasetServerFilter:'agent',scanPath:'/data/team',
    remoteScanPath:'/data/team',datasetBrowseRevision:0,
    controlPlaneNodes:[{node_id:'agent',allowed_roots:['/data/team','/data/shared']}],
    remoteLocations:[{node_id:'agent',root:'/data/team/first'},
        {node_id:'agent',root:'/data/shared/second'},{node_id:'other',root:'/data/shared/third'}]});
app.selectScanPath('/data/shared');
assert.equal(app.remoteScanPath,'/data/shared');
assert.equal(app.remoteLocationsForSelectedServer().length,1);
assert.equal(app.remoteLocationsForSelectedServer()[0].root,'/data/shared/second');
Object.assign(app,{datasetServerFilter:'local',scanPath:'/local',remoteScanPath:''});
assert.equal(app.restoreDatasetBrowser(),true);
assert.equal(app.datasetServerFilter,'agent');
assert.equal(app.scanPath,'/data/shared');
assert.equal(app.remoteScanPath,'/data/shared');
assert.equal(app.remoteLocationsForSelectedServer().length,1);
app.scanPath='/data/shared/subdirectory';
app.onScanPathInput();
assert.equal(app.remoteScanPath,'/data/shared/subdirectory');
app.scanPath='/outside';
app.onScanPathInput();
assert.equal(app.remoteScanPath,'/data/shared/subdirectory');
assert.equal(JSON.parse(storage.get(app.datasetBrowserStorageKey())).path,'/data/shared/subdirectory');
app.controlPlaneUser={user_id:'other-user'};
assert.equal(app.restoreDatasetBrowser(),false);
app.controlPlaneUser={user_id:'owner'};
app.controlPlaneNodes[0].allowed_roots=['/data/team'];
assert.equal(app.restoreDatasetBrowser(),false);
app.controlPlaneNodes=[];
assert.equal(app.restoreDatasetBrowser(),false);
window.localStorage.getItem=()=>{throw new Error('storage blocked');};
window.localStorage.setItem=()=>{throw new Error('storage blocked');};
assert.equal(app.restoreDatasetBrowser(),false);
app.storeDatasetBrowser();
""",
    )


def test_working_dataset_storage_is_scoped_to_the_current_account():
    _run_methods(
        ["storedDatasetKey", "storeDatasetKey"],
        """
const storage = new Map([['dataPlatform.selectedDataset','legacy/local']]);
global.window={localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),
    removeItem:key=>storage.delete(key)}};
app.controlPlaneUser={user_id:'first'};
assert.equal(app.storedDatasetKey(),'');
app.storeDatasetKey('remote:first');
assert.equal(app.storedDatasetKey(),'remote:first');
app.controlPlaneUser={user_id:'second'};
assert.equal(app.storedDatasetKey(),'');
app.storeDatasetKey('remote:second');
app.controlPlaneUser={user_id:'first'};
assert.equal(app.storedDatasetKey(),'remote:first');
app.storeDatasetKey('');
assert.equal(app.storedDatasetKey(),'');
app.controlPlaneUser=null;
assert.equal(app.storedDatasetKey(),'legacy/local');
""",
    )


def test_dataset_refresh_reads_the_index_and_only_explicit_scan_discovers():
    _run_methods(
        ["loadDatasetRoots", "refreshSelectedServer", "scanSelectedServer"],
        """
const requests = [];
Object.assign(app, {datasetServerFilter:'local',rootDir:'/datasets',scanPath:'/datasets',
    datasetAccessRestricted:false,remoteScanSerial:1,scanBusy:false,
    requestJson:async url=>{requests.push({url,busy:app.scanBusy});
        return {root_dir:'/datasets',candidates:[]};},loadDatasets:async()=>{},loadAdminStatus:async()=>{}});
await app.loadDatasetRoots();
await app.refreshSelectedServer();
await app.scanSelectedServer();
assert.deepEqual(requests.map(row=>new URL(row.url,'http://console').searchParams.get('scan')),
    ['0','0','1']);
assert.deepEqual(requests.map(row=>row.busy),[false,false,true]);
assert.equal(app.scanBusy,false);
app.scanBusy=true;
await app.loadDatasetRoots();
assert.equal(app.scanBusy,true,'a passive refresh must preserve an active remote Scan');
assert.equal(app.error,'');
""",
    )


def test_cancelled_dataset_change_preserves_local_and_remote_context():
    _run_methods(
        ["selectDataset", "useRemoteDataset", "confirmDatasetChange"],
        """
const original = {key: 'local/working'};
Object.assign(app, {selectedDataset: original, selectedDatasetKey: original.key, selectionToken: 7});
global.CustomEvent = class {constructor(type, options) {this.type=type;this.cancelable=options.cancelable;}};
let requests = 0;
global.window = {dispatchEvent: event => {
    requests++; assert.equal(event.type, 'data-platform:before-dataset-change');
    assert.equal(event.cancelable, true); return false;
}};
await app.selectDataset({key: 'local/other'});
await app.useRemoteDataset({location_id: 'agent-dataset'});
assert.equal(app.selectedDataset, original);
assert.equal(app.selectedDatasetKey, original.key);
assert.equal(app.selectionToken, 7);
assert.equal(requests, 2);
assert.equal(app.confirmDatasetChange(original.key), true);
assert.equal(requests, 2);
""",
    )


def test_metadata_loading_does_not_claim_viewer_cache_is_ready():
    _run_methods(
        ["datasetStatusText", "datasetStatusClass"],
        """
for (const remote of [true, false]) {
    const dataset = {loaded: true, remote, cache: {status: 'missing'}};
    assert.equal(app.datasetStatusText(dataset), 'viewer missing');
    assert.ok(!app.datasetStatusClass(dataset).includes('emerald'));
    dataset.cache.status = 'cached';
    assert.equal(app.datasetStatusText(dataset), 'viewer ready');
    assert.ok(app.datasetStatusClass(dataset).includes('emerald'));
}
""",
    )


def test_job_scope_keeps_unrelated_details_out_and_distinguishes_queueing():
    _run_methods(
        [
            "filteredJobs",
            "selectedJob",
            "jobBelongsToSelectedDataset",
            "isRunningJob",
            "openJobDrawer",
            "runningJobs",
            "jobButtonSummary",
            "jobStateText",
        ],
        """
const other = {id:'other',dataset_key:'other',status:'running',progress:50};
const current = {id:'mine',dataset_key:'source',status:'queued',progress:0};
Object.assign(app, {jobs:[other,current], selectedDatasetKey:'source', selectedJobId:'other',
    runsFilter:{scope:'all',status:''}, scrollLogsSoon:()=>{}, selectJob(job){this.selectedJobId=job.id;}});
app.openJobDrawer(true);
assert.equal(app.selectedJob(), current);
assert.deepEqual(app.filteredJobs(), [current]);
app.runsFilter.status = 'error';
assert.deepEqual(app.filteredJobs(), []);
assert.equal(app.selectedJob(), null);
app.openJobDrawer();
assert.equal(app.selectedJob(), current);
assert.equal(app.runsFilter.scope, 'all');
assert.equal(app.jobStateText(current), 'Queued');
assert.equal(app.jobStateText(other), 'Running · 50%');
app.jobs = [current];
assert.equal(app.jobButtonSummary(), '1 queued');
""",
    )


def test_reason_without_a_decision_is_preserved_instead_of_silently_dropped():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser-script behavior checks")
    source = (TEMPLATES / "curation.js").read_text()
    save = source.split("  async function saveReview(event) {", 1)[1].split(
        "  async function loadResult()", 1
    )[0]
    script = (
        """
const assert = require('node:assert/strict');
const values = {decision:'',reason:'Inspect the camera'};
const form = {values};
const $ = id => id === 'review-form' ? form : {children:[]};
global.FormData = class {constructor(form){this.values=form.values;} get(key){return this.values[key] || '';}};
let reviewRevision=1, error='', saved=false, savedReview=null;
const action = async fn => {try{await fn();}catch(e){error=e.message;}};
const draft=()=>({workspace_id:'draft'}), episode=()=>0;
const api=async()=>{saved=true;}, refresh=async()=>{}, loadReview=()=>{}, notice=()=>{};
const reviewValue=()=>JSON.stringify(values), reviewStatus=()=>{};
async function saveReview(event) {
"""
        + save
    )
    script += """
(async()=>{
    await saveReview({preventDefault(){}});
    assert.equal(saved, false);
    assert.match(error, /Choose a review decision/);
    assert.equal(values.reason, 'Inspect the camera');
    values.decision = 'review'; error = '';
    await saveReview({preventDefault(){}});
    assert.equal(saved, true);
    assert.equal(error, '');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
