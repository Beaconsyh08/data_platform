"""Behavioral checks for task setup and the analysis selection controls."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask, render_template

TEMPLATES = Path(__file__).parents[2] / "lerobot" / "data_platform" / "templates"


def _run_page_script(template: str, checks: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for template JavaScript checks")
    app = Flask(__name__, template_folder=str(TEMPLATES))
    with app.test_request_context():
        html = render_template(
            template,
            dataset_key="demo/laundry",
            dataset_namespace="demo",
            dataset_name="laundry",
            task_configuration_enabled=True,
        )
    script = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)[-1]
    subprocess.run(
        [node, "-e", "const assert = require('node:assert/strict');\n" + script + "\n" + checks],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_analysis_task_filters_keep_pick_variants_and_curation_handoff():
    _run_page_script(
        "visualize_dataset_analysis.html",
        """
        const app = analysisPage();
        app.episodes = [
            ...['pick', 'directional_pick', 'relational_pick'].map((scene, episode_id) => ({
                episode_id, scene, task_families:['pick'], task:'Pick up a toy',
                task_attributes:{object:['toy']}, review_reasons:[]
            })),
            {episode_id:3, task:'Open washer', task_families:['open_door'],
             task_ids:['washer'], task_attributes:{appliance:['washing_machine']},
             review_reasons:['missing_csv']},
            {episode_id:4, task:'Fold an unfamiliar towel', task_families:['unknown'],
             task_status:'pending', review_reasons:[]}
        ];
        app.setScene('pick_group');
        assert.equal(app.activeRows().length, 3);
        app.setPickSubtype('directional_pick');
        assert.deepEqual(app.activeRows().map(row => row.episode_id), [1]);
        app.setScene('open_door');
        app.taskDimension = 'task_attributes.appliance';
        app.addTaskFilter('washing_machine');
        app.instructionSearch = 'Open';
        assert.deepEqual(app.detailsRows().map(row => row.episode_id), [3]);
        const url = new URL(app.curationTaskUrl(), 'http://localhost');
        assert.equal(url.searchParams.get('dataset_key'), 'demo/laundry');
        assert.equal(url.searchParams.get('tab'), 'selection');
        assert.deepEqual(JSON.parse(url.searchParams.get('filters')), [
            {dimension:'task_attributes.appliance', value:'washing_machine'},
            {dimension:'task_family', value:'open_door'},
            {dimension:'task', value:'Open'}
        ]);
        app.resetFilters();
        app.instructionSearch = 'unfamiliar';
        assert.equal(app.detailsRows().length, 1);
        assert.equal(app.reviewRows().length, 0);
        assert.equal(app.pendingCount(), 1);
        app.resetFilters();
        assert.equal(app.hasFilters(), false);
        assert.equal(app.detailsRows().length, 5);
        """,
    )


def test_analysis_counts_multitask_episodes_once_per_value_and_paginates_all_rows():
    _run_page_script(
        "visualize_dataset_analysis.html",
        """
        const app = analysisPage();
        app.episodes = Array.from({length:130}, (_, episode_id) => ({
            episode_id, task:'Open washer; close dryer', task_families:['open_door','close_door'],
            task_attributes:{appliance:['washer','dryer','washer']},
            resolved_tasks:[{raw_task:'Open washer'}, {raw_task:'Close dryer'}],
            review_reasons:episode_id === 0 ? ['missing_csv', 'missing_stage'] : []
        }));
        app.taskDimension = 'task_attributes.appliance';
        const groups = app.taskDistribution();
        assert.equal(groups.length, 2);
        assert.ok(groups.every(row => row.count === 130 && row.percent === 100));
        app.taskDimension = 'task';
        assert.deepEqual(app.taskDistribution().map(row => row.key), ['Close dryer', 'Open washer']);
        app.episodePage = 3;
        assert.equal(app.pagedEpisodes().length, 30);
        assert.equal(app.pagedEpisodes().at(-1).episode_id, 129);
        app.reviewOnly = 'flagged';
        assert.equal(app.currentPage(), 1);
        assert.equal(app.pagedEpisodes()[0].episode_id, 0);
        app.reviewOnly = 'all';
        app.taskFilters = [{dimension:'task_family', value:'nonexistent'}];
        assert.equal(app.pageCount(), 1);
        assert.equal(app.pageRange(), '0 episodes');
        """,
    )


def test_analysis_missing_presence_filter_and_configuration_status():
    _run_page_script(
        "visualize_dataset_analysis.html",
        """
        const app = analysisPage();
        app.episodes = [{episode_id:0, exist_counts:{exist_label:{true:10}}},
                        {episode_id:1, exist_counts:{}},
                        {episode_id:2, exist_counts:{exist_label:{missing:10}}}];
        app.detailChartDimension = 'exist';
        const missing = app.detailChartRows().find(row => row.filter.existValue === 'missing');
        assert.equal(missing.count, 2);
        app.applyDetailChartFilter(missing);
        assert.deepEqual(app.detailsRows().map(row => row.episode_id), [1,2]);
        app.removeDetailFilter(app.detailFilterChips()[0]);
        assert.equal(app.detailsRows().length, 3);
        app.summary = {task_config:{catalog:{version:2}}, task_config_stale:true};
        assert.match(app.configurationLabel(), /v2.*Cached version/);
        """,
    )


def test_task_setup_edits_existing_definition_and_requires_loaded_dataset():
    _run_page_script(
        "data_platform_tasks.html",
        """
        const app = taskPage();
        const task = {task_id:'washer', family:'open_door', label:'Open washer',
                      aliases:['Open washer'], attributes:{appliance:'washer'},
                      stage_strategy:'equal_time', stage_count:5};
        app.catalogs = [{catalog_version_id:'catalog-1', version:1, tasks:[task]}];
        app.catalogId = 'catalog-1';
        app.defineDiscovered({...task, raw_task:'Open washer', status:'alias', mapping_key:'open washer'});
        assert.equal(app.editingId, 'washer');
        assert.equal(app.tab, 'catalog');
        app.form.label = 'Open washing machine';
        app.suggestTaskId();
        assert.equal(app.form.task_id, 'washer');
        app.newTask('');
        app.form.label = 'Open the refrigerator door';
        app.suggestTaskId();
        assert.equal(app.form.task_id, 'open_the_refrigerator_door');
        app.loadedDatasetKey = 'demo/laundry';
        app.appliedSnapshot = {catalog:app.catalogs[0], mappings:{}};
        assert.equal(app.curationReady(), true);
        app.datasetKey = 'demo/other';
        assert.equal(app.curationReady(), false);
        assert.throws(() => app.mappingBody(), /Load this dataset/);
        assert.equal(app.statusLabel('conflict'), 'Ambiguous alias');
        """,
    )


def test_ai_suggestions_require_selection_and_cannot_apply_to_a_changed_dataset():
    _run_page_script(
        "data_platform_tasks.html",
        """
        (async () => {
            const app = taskPage();
            app.loadedDatasetKey = app.datasetKey;
            app.catalogId = 'catalog-1';
            const task = {task_id:'fridge', label:'Open fridge', family:'open_door', attributes:{appliance:'fridge'}};
            app.catalogs = [{catalog_version_id:'catalog-1', tasks:[]}];
            app.api = async (url, body) => ({job:{id:'job-1', status:'done'}, result:{suggestions:[
                {instruction:'Open fridge', mapping_key:'open fridge', action:'create', task, episode_count:2}
            ]}});
            await app.suggestWithAI();
            assert.equal(app.aiSuggestions.length, 1);
            assert.equal(app.aiSuggestions[0].selected, false);
            assert.equal(app.previewResult, null);
            app.aiSuggestions[0].selected = true;
            let accepted = false;
            app.api = async (url, body) => {
                accepted = true;
                assert.equal(body.suggestions.length, 1);
                assert.deepEqual(body.suggestions[0].task.attributes, {appliance:'fridge'});
                return {catalog:{catalog_version_id:'catalog-2', tasks:[task]}, preview:{tasks:[],
                    task_inventory_digest:'inventory', snapshot:{mappings:{'open fridge':'fridge'}}}};
            };
            await app.saveAISuggestions();
            assert.equal(app.error, '');
            assert.equal(accepted, true);
            assert.equal(app.catalogId, 'catalog-2');
            assert.equal(app.previewResult.task_inventory_digest, 'inventory');
            assert.equal(app.appliedSnapshot, null);
            assert.equal(app.aiSuggestions.length, 0);
            app.aiContext = JSON.stringify(app.mappingBody());
            app.datasetKey = 'demo/changed';
            app.api = async () => { throw new Error('Should not send a request'); };
            await app.saveAISuggestions();
            assert.match(app.error, /Load this dataset/);
        })().catch(error => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_metadata_duration_is_filterable_without_csv_and_remote_episode_links_are_optional():
    _run_page_script(
        "visualize_dataset_analysis.html",
        """
        const app = analysisPage();
        app.episodes = [{episode_id:3, frames:60, duration_seconds:6, duration_source:'metadata',
                         frames_source:'metadata', cache_status:'missing_csv', task_families:['open_door']}];
        assert.equal(app.durationAvailable(app.episodes[0]), true);
        assert.equal(app.baseSummary().total_frames, 60);
        assert.equal(app.detailChartRows()[0].count, 1);
        app.applyDetailChartFilter(app.detailChartRows()[0]);
        assert.equal(app.detailsRows().length, 1);
        assert.equal(app.cacheStatusLabel('missing_csv'), 'Not generated');
        app.analysisRemote = true;
        assert.equal(app.episodeUrl(app.episodes[0]), '');
        app.episodes[0].viewer_url = '/node-test/laundry/episode_3';
        assert.equal(app.episodeUrl(app.episodes[0]), '/node-test/laundry/episode_3');
        """,
    )


def test_remote_explore_exposes_analysis_before_viewer_cache_exists():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for template JavaScript checks")
    template = (TEMPLATES / "visualize_dataset_homepage.html").read_text()
    methods = []
    for name in ("remoteDatasetRecord", "analysisUrl", "visualizationItems"):
        start = re.search(rf"^                {name}\([^\n]*\) \{{", template, re.MULTILINE)
        end = re.search(r"^                \},", template[start.end() :], re.MULTILINE)
        methods.append(template[start.start() : start.end() + end.end()])
    script = "const assert = require('node:assert/strict'); const app = {" + "\n".join(methods) + "};\n"
    # Other visualization providers are intentionally unavailable in this fixture.
    for name in set(re.findall(r"this\.(\w+)\(", "\n".join(methods))):
        script += f"app.{name} ??= () => false;\n"
    script += """
        app.openLinkEnabled = () => true;
        app.selectedDataset = app.remoteDatasetRecord({location_id:'location-123', dataset_key:'node/test',
                                                      node_id:'offline-node', metadata:{total_episodes:2}});
        assert.equal(app.analysisUrl(), '/remote/location-123/analysis');
        const items = app.visualizationItems();
        assert.deepEqual(items.map(item => item.key), ['viewer','analysis']);
        assert.equal(items.find(item => item.key === 'analysis').ready, true);
        assert.equal(items.find(item => item.key === 'viewer').ready, false);
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=20)
