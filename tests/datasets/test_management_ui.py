"""Management navigation and filters operate on real rendered template JavaScript."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask, render_template

TEMPLATES = Path(__file__).parents[2] / "lerobot/data_platform/templates"


def render_page(role):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    with app.test_request_context():
        return render_template(
            "data_platform_control_plane.html",
            user={
                "role": role,
                "user_id": "owner",
                "display_name": "Test user",
                "username": "test",
            },
        )


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
def test_management_navigation_and_filters(role):
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    html = render_page(role)
    assert ('id="admin-usage-panel"' in html) == (role == "admin")
    assert ('id="admin-database-panel"' in html) == (role == "admin")
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    checks = """
const assert = require('node:assert/strict');
const app = controlPlane();
global.window = {history: {replaceState() {}}};
assert.equal(app.sections().some(item => item.id === 'users'), app.isAdmin);
app.jobs = Array.from({length: 25}, (_, i) => ({job_id: String(i), operation: 'preprocess.merge',
    status: i < 22 ? 'queued' : 'error', requested_by: i % 2 ? 'owner' : 'other', location_id: 'data'}));
app.locations = [{location_id: 'data', dataset_key: 'demo/kitchen'}];
assert.equal(app.paginatedJobs().length, 20);
app.jobPage = 2; assert.equal(app.paginatedJobs().length, 5);
app.openJobs('attention'); assert.equal(app.section, 'jobs');
assert.equal(app.jobPage, 1); assert.equal(app.filteredJobs().length, 3);
app.jobSearch = 'KITCHEN'; assert.equal(app.filteredJobs().length, 3);
app.myJobsOnly = true; assert.equal(app.filteredJobs().length, 1);
app.jobSearch = 'missing'; assert.equal(app.filteredJobs().length, 0);
assert.equal(app.jobPageCount(), 1);
app.users = [{user_id:'owner', username:'alice', active:false},
    {user_id:'other', username:'bob', active:true}];
app.userSearch = 'alice'; assert.equal(app.filteredUsers().length, 1);
app.userStatus = 'active'; assert.equal(app.filteredUsers().length, 0);
app.databaseRows = [{old:'row'}]; app.databaseTable = ''; app.loadDatabase();
assert.equal(app.databaseRows.length, 0);
if (!app.isAdmin) { app.selectSection('database'); assert.equal(app.section, 'overview'); }
assert.equal(app.formatTime(null), '—');
"""
    subprocess.run(["node"], input=script + checks, check=True, capture_output=True, text=True, timeout=10)


def test_pipeline_run_controls_use_owner_endpoint_and_preserve_capabilities():
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    app = Flask(__name__, template_folder=str(TEMPLATES))
    with app.test_request_context():
        html = render_template(
            "visualize_dataset_homepage.html",
            console_mode="full",
            legacy_mutations_enabled=False,
            control_plane_enabled=True,
            control_plane_user={"user_id": "owner", "username": "alice", "role": "operator"},
            admin_authenticated=False,
            allowed_tabs=[],
            allowed_open_links=[],
            console_groups=[],
            datasets_root="/data",
            initial_page="runs",
            initial_selected_dataset="",
            initial_tab="",
        )
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    checks = """
const assert = require('node:assert/strict');
(async () => {
 const app = precomputeConsole();
 const own = {id:'job', control_job_id:'job', requested_by:'owner', requested_by_username:'alice',
   status:'error', revision:4, available_actions:['retry']};
 assert(app.canControlRun(own,'retry'));
 const other = {...own, requested_by:'other'};
 assert(!app.canControlRun(other,'retry'));
 app.controlPlaneUser.role='admin'; app.adminModeEnabled=true;
 assert(!app.canControlRun(other,'retry'));
 assert(app.runControlReason(other).includes('Platform management'));
 app.controlPlaneUser.role='viewer'; assert(!app.canControlRun(own,'retry'));
 app.controlPlaneUser.role='operator';
 let request;
 app.requestJson = async (url, body) => {request={url,body}; return {job:{status:'queued'}};};
 app.loadJobs = async () => {};
 await app.controlRun(own,'retry');
 assert.equal(request.url,'/api/jobs/job/retry'); assert.equal(request.body.revision,4);
 assert(request.body.idempotency_key); assert(!request.body.body);
 assert.equal(app.jobControlBusy,''); assert.equal(app.selectedJobId,'job');
 assert(app.jobControlNotice.includes('queued'));
 app.remoteLocations=[]; app.controlPlaneNodes=[];
 const remote=app.remoteJobForRuns({...own,job_id:'job',operation:'viewer.prepare',location_id:'location'});
 assert.equal(remote.requested_by_username,'alice'); assert.equal(remote.control_job_id,'job');
 assert.deepEqual(remote.available_actions,['retry']);
 app.selectedJobId='job'; app.remoteJobs=[{job_id:'job',status:'cancel_requested'}];
 global.window={clearTimeout(){},setTimeout(){return 123;}};
 app.scheduleSelectedRemoteJobRefresh('job'); assert.equal(app.selectedRemoteJobTimer,123);
 app.selectedRemoteLocation=null;
 app.scheduleRemoteRefresh=()=>{};
 app.mergeRemoteJob=()=>{};
 app.remoteJobForRuns=job=>job;
 await app.submitRemoteJob('/api/control/locations/source/preprocess-jobs', {op:'merge'});
 assert.equal(request.url,'/api/control/locations/source/preprocess-jobs');
 app.requestJson=async()=>{throw new Error('Submission denied')};
 await app.submitRemoteJob('/api/control/locations/source/preprocess-jobs', {op:'merge'});
 assert.equal(app.error,'Submission denied');
 app.jobs=[own]; app.remoteJobs=[{job_id:'job', operation:'local.request.preprocess'}];
 app.mergeRemoteJobsIntoRuns(); assert.equal(app.jobs.length,1);
})().catch(error=>{console.error(error);process.exit(1)});
"""
    subprocess.run(["node"], input=script + checks, check=True, capture_output=True, text=True, timeout=10)
