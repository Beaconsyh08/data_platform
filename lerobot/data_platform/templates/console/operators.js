// Operators state and behavior for the legacy console.
function consoleOperatorsState() {
    return {
                dataVersionManual: false,
                deleteFlagReasonDatasetKey: '',
                deleteFlagReasonLoading: false,
                deleteFlagReasonOptions: [],
                deleteFlagReasonEpisodes: {},
                deleteFlagReasonLabels: {
                    wrong_prompt: 'Prompt error',
                    arm_shaking: 'Arm shaking',
                    unstable_placement: 'Unstable placement',
                    camera_issue: 'Camera issue',
                    manual_flag: 'Manual flag',
                    unknown_flag_reason: 'Unknown flag reason',
                    early_gripper_transition: 'Early gripper transition',
                    stuck_closed_gripper_no_action: 'Stuck closed gripper',
                    state_gripper_transition_without_action: 'State gripper changed without action',
                    joint_zero_reset_spike: 'Joint zero reset spike',
                    missing_subtask_state: 'Missing subtask_state',
                    unsegmented_subtask_state: 'Unsegmented subtask_state',
                    multi_sentence_prompt: 'Multi-sentence prompt',
                    multiple_task_assignments: 'Multiple task assignments',
                    prompt_action_mismatch: 'Prompt/action mismatch',
                    missing_parquet: 'Missing parquet',
                    missing_target_detection: 'Missing target detection',
                    bad_stage: 'Bad stage/subtask',
                    early_joint_spike: 'Joint spike',
                    missing_stage: 'Missing stage',
                    bad_image_or_video: 'Bad image/video'
                },
                labelingCapabilities: null,
                constructionPreview: null,
                constructionLoading: false,
                preprocessCapabilities: null,
                taggingCapabilities: null,
                taggingSchema: [],
                embeddingCapabilities: null,
                compareCandidates: [],
                valueEditNextId: 2,
                preprocess: {
                    schema_op: 'convert_action',
                    target_dim: 16,
                    v3_data_file_size_mb: 100,
                    v3_video_file_size_mb: 200,
                    v3_workers: 8,
                    v3_image_video_mode: 'lerobot_official',
                    field_name: '',
                    smooth_window: 5,
                    smooth_workers: 0,
                    smooth_state: true,
                    standardize_overwrite: false,
                    standardize_data_version: 'DVT2',
                    standardize_delete_episode_ids: '',
                    prompt_pattern: '\\bpick up the (.+?) to the (left|right)\\b',
                    prompt_replacement: 'pick up the \\1 on the \\2',
                    quality_data_version: 'DVT2',
                    quality_workers: 8,
                    quality_episode_ids: '',
                    quality_overwrite: false,
                    quality_clear_manual_flags: false,
                    delete_flag_reason: '',
                    delete_episode_ids: '',
                    delete_reason: '',
                    remote_mutation_reason: '',
                    flag_delete_reason: '',
                    apply_prompt_assignment_episode_ids: '',
                    value_edits: [{id: 1, field: 'action', dimension: 0, value: 0}],
                    value_episode_scope: 'selected',
                    value_episode_ids: '',
                    value_output_mode: 'new_dataset',
                    value_out_root: '',
                    schema_out_root: '',
                    schema_overwrite: false,
                    schema_dry_run: false,
                    split_merge_op: 'split',
                    episode_range: '',
                    task_filter: '',
                    split_out_root: '',
                    split_dry_run: false,
                    merge_src_keys: [],
                    merge_dataset_search: '',
                    merge_delete_episodes: {},
                    merge_out_root: '',
                    merge_workers: 8,
                    merge_dimension_policy: 'strict',
                    merge_mapping_mode: 'indices',
                    merge_index_positions: {min: {}, pad: {}},
                    merge_index_shared: false,
                    merge_dimension_names: {},
                    merge_prefix_dims: 0,
                    merge_tail_dims: 0,
                    merge_padding_value: 0,
                    merge_mapping_confirmation: '',
                    merge_dry_run: false,
                    subtract_src_keys: [],
                    subtract_dataset_search: '',
                    subtract_out_root: '',
                    subtract_workers: 8,
                    subtract_dry_run: false
                },
                cacheFields: [
                    {key: 'overwrite', label: 'Recompute all cache', help: 'Overwrite existing video and CSV cache.'},
                    {key: 'overwrite_csv', label: 'Recompute CSV only', help: 'Overwrite CSV cache, keep videos.'}
                ],
                mutationFields: [
                    {key: 'fix_episode_indices', label: 'Repair episode indices', help: 'Fix frame_index/timestamp/index in source parquet.'},
                    {key: 'force_recompute_stage', label: 'Force stage recompute', help: 'Ignore existing stage and recompute stage/subtask annotations.'},
                    {key: 'write_parquet', label: 'Write stage to parquet', help: 'Persist computed subtask_state.'},
                    {key: 'write_subtask', label: 'Write subtask text', help: 'Persist text labels from subtask_state.'},
                    {key: 'overwrite_subtask_text', label: 'Overwrite subtask text', help: 'Replace existing subtask text columns.'},
                    {key: 'overwrite_parquet', label: 'Overwrite parquet labels', help: 'Implies stage recompute, parquet write, subtask write, CSV overwrite.'}
                ],
                options: {
                    data_version: 'DVT2',
                    episodes: '',
                    downsample: '',
                    prepare_videos: true,
                    prepare_csv: true,
                    prepare_workers: 8,
                    visualize_only: true,
                    precomputed_only: true,
                    fix_episode_indices: false,
                    overwrite: false,
                    overwrite_csv: false,
                    overwrite_parquet: false,
                    write_parquet: false,
                    write_subtask: false,
                    force_recompute_stage: false,
                    fallback_stage_count: 5,
                    overwrite_subtask_text: false
                },
                labeling: {
                    backend: 'qwen_dashscope',
                    model_id: 'IDEA-Research/grounding-dino-base',
                    devices: '',
                    endpoint: '',
                    qwen_model: 'qwen3.6-plus',
                    qwen_token: '',
                    min_pixels: 1024,
                    max_pixels: 9800,
                    workers: 8,
                    overwrite: false,
                    trial: false,
                    trial_per_type: 20,
                    trial_seed: '',
                    box_threshold: 0.25,
                    text_threshold: 0.25,
                    episodes: '',
                    run_mode: 'missing',
                    save_vis: true,
                    annotate: true
                },
                construction: {
                    uncertainty_threshold: 50,
                    include_positives: false,
                    allow_pick_to_give: false,
                    oversample_factor: 2,
                    out_root: '',
                    per_scenario_counts: {
                        single_pick: 0,
                        directional_pick: 0,
                        relative_pick: 0,
                        give: 0
                    }
                },
                tagging: {
                    tag_names: ['background', 'background_color', 'object_count', 'prompt_action_match', 'arm', 'grasp_xy'],
                    episodes: '',
                    vlm_backend: 'qwen_dashscope',
                    vlm_endpoint: '',
                    vlm_token: '',
                    vlm_model: 'qwen3.6-plus',
                    workers: 8,
                    trial: false,
                    trial_per_type: 20,
                    trial_seed: ''
                },
                embedding: {
                    ckpt_path: '/DATA/disk0/yuhao.song/checkpoints/pi05_h10w_dual_full_finetune_0417_ALL/0417_ALL/29999',
                    openpi_config: '',
                    layer_hook: 'pi_prefix',
                    episodes: '',
                    workers: '',
                    devices: '',
                    refit: false
                },
                compare: {
                    dataset_key_b: ''
                },
                applyOptionRules(target, respectVisualizeOnly = true) {
                    if (respectVisualizeOnly && target.visualize_only) {
                        for (const field of this.mutationFields) {
                            if (field.key === 'force_recompute_stage') continue;
                            target[field.key] = false;
                        }
                        return;
                    }
                    if (target.overwrite) {
                        target.overwrite_csv = true;
                    }
                    if (target.overwrite_parquet) {
                        target.force_recompute_stage = true;
                        target.write_parquet = true;
                        target.write_subtask = true;
                        target.overwrite_csv = true;
                    }
                },
                syncOptionRules() {
                    this.applyOptionRules(this.options, this.activeTab === 'cache');
                },
                cacheOnlyOverrides() {
                    const isDvt2 = this.normalizeDataVersion(this.options.data_version) === 'DVT2';
                    return {
                        visualize_only: true,
                        fix_episode_indices: false,
                        force_recompute_stage: isDvt2,
                        overwrite_csv: isDvt2 || this.options.overwrite_csv,
                        write_parquet: false,
                        write_subtask: false,
                        overwrite_parquet: false,
                        overwrite_subtask_text: false
                    };
                },
                stageSubtaskOverrides() {
                    return {
                        prepare_videos: false,
                        prepare_csv: true,
                        visualize_only: false,
                        overwrite: false,
                        overwrite_csv: true,
                        force_recompute_stage: true
                    };
                },
                async loadPreprocessCapabilities() {
                    try {
                        this.preprocessCapabilities = await this.requestJson('/api/preprocess/capabilities');
                    } catch (err) {
                        this.preprocessCapabilities = {available: false, error: err.message, ops: []};
                    }
                },
                async loadLabelingCapabilities() {
                    try {
                        const data = await this.requestJson('/api/labeling/capabilities');
                        this.labelingCapabilities = data;
                        if (data.default_model && !this.labeling.model_id) this.labeling.model_id = data.default_model;
                        const grounding = data.backends && data.backends.grounding_dino ? data.backends.grounding_dino : data;
                        if (grounding && grounding.cuda_device_count && this.labeling.workers === 8) {
                            this.labeling.workers = Math.min(8, Number(grounding.cuda_device_count));
                        }
                        this.applyLabelingBackendDefaults(false);
                    } catch (err) {
                        this.labelingCapabilities = {available: false, error: err.message, default_model: this.labeling.model_id};
                    }
                },
                async loadTaggingCapabilities() {
                    try {
                        this.taggingCapabilities = await this.requestJson('/api/tagging/capabilities');
                        this.applyTaggingBackendDefaults(false);
                    } catch (err) {
                        this.taggingCapabilities = {available: false, error: err.message};
                    }
                },
                async loadTaggingSchema() {
                    try {
                        const data = await this.requestJson('/api/tagging/schema');
                        this.taggingSchema = data.tags || [];
                        if ((!this.tagging.tag_names || this.tagging.tag_names.length === 0) && data.default_selected_tags) {
                            this.tagging.tag_names = data.default_selected_tags.slice();
                        }
                    } catch (err) {
                        this.taggingSchema = [];
                        if (!this.error) this.error = err.message;
                    }
                },
                async loadEmbeddingCapabilities() {
                    try {
                        this.embeddingCapabilities = await this.requestJson('/api/embedding/capabilities');
                        if (this.embeddingCapabilities.default_checkpoint_path && !this.embedding.ckpt_path) {
                            this.embedding.ckpt_path = this.embeddingCapabilities.default_checkpoint_path;
                        }
                        if (this.embeddingCapabilities.default_workers && !this.embedding.workers) {
                            this.embedding.workers = this.embeddingCapabilities.default_workers;
                        }
                        if (this.embeddingCapabilities.default_devices && !this.embedding.devices) {
                            this.embedding.devices = this.embeddingCapabilities.default_devices;
                        }
                        if (this.embeddingCapabilities.default_layer && !this.embedding.layer_hook) {
                            this.embedding.layer_hook = this.embeddingCapabilities.default_layer;
                        }
                    } catch (err) {
                        this.embeddingCapabilities = {error: err.message};
                    }
                },
                async loadCompareCandidates() {
                    const current = consoleReadScope(this, 'loadCompareCandidates', 'dataset');
                    try {
                        const data = await this.requestJson('/api/compare/candidates');
                        if (!current()) return;
                        this.compareCandidates = data.datasets || [];
                        if (!this.compare.dataset_key_b && this.selectedDataset) {
                            const first = this.compareCandidates.find(item => item.key !== this.selectedDataset.key);
                            if (first) this.compare.dataset_key_b = first.key;
                        }
                    } catch (err) {
                        if (!current()) return;
                        this.compareCandidates = [];
                        if (!this.error) this.error = err.message;
                    }
                },
                canOperateRemote() {
                    return Boolean(
                        this.controlPlaneUser
                        && ['admin', 'data_manager', 'operator'].includes(this.controlPlaneUser.role)
                    );
                },
                selectedRemoteNode() {
                    const nodeId = this.selectedRemoteLocation?.node_id || this.selectedDataset?.remote_node_id;
                    return this.controlPlaneNodes.find(node => node.node_id === nodeId) || null;
                },
                remoteMergeAvailable() {
                    return Boolean(this.canOperateRemote()
                        && this.selectedRemoteNode()?.capabilities?.operations?.includes('preprocess.merge'));
                },
                remoteSourceMutationsAvailable() {
                    return Boolean(
                        this.selectedDataset?.remote
                        && this.remoteSourceMutationsEnabled
                        && this.selectedRemoteLocation?.source_mutation_allowed
                        && this.selectedRemoteNode()?.capabilities?.source_mutations_enabled
                    );
                },
                remoteMutationDisabledReason() {
                    if (!this.selectedRemoteLocation) return 'Select an Agent dataset first.';
                    if (!this.selectedRemoteLocation?.source_mutation_allowed) return 'Source mutation permission is required for this dataset. Ask an administrator to configure Data permissions.';
                    if (!this.remoteSourceMutationsEnabled) return 'Source deletion is disabled on Server A. Enable --enable-legacy-mutations 1 in the server configuration.';
                    if (!this.selectedRemoteNode()) return 'Agent information is unavailable. Refresh the dataset list.';
                    if (!this.selectedRemoteNode()?.capabilities?.source_mutations_enabled) return 'Source deletion is disabled on this Agent. Enable --allow-source-mutations in its configuration.';
                    return '';
                },
                async prepareRemoteViewer(location, options = {}) {
                    if (!location || !this.canOperateRemote()) return null;
                    this.selectedRemoteLocation = location;
                    this.remoteActionBusy = true;
                    this.remoteError = '';
                    try {
                        const data = await this.requestJson(
                            `/api/control/locations/${this.selectedRemoteLocation.location_id}/viewer-jobs`,
                            options
                        );
                        this.mergeRemoteJob(data.job);
                        this.scheduleRemoteRefresh();
                        return data.job;
                    } catch (err) {
                        this.remoteError = err.message;
                        return null;
                    } finally {
                        this.remoteActionBusy = false;
                    }
                },
                confirmOverwrite(kind, count, detail = '') {
                    const value = Number(count || 0);
                    if (value <= 0) return true;
                    const suffix = detail ? `\n${detail}` : '';
                    return window.confirm(
                        `Existing ${kind} results: ${value}.\n\nRunning again will overwrite this result set.${suffix}\n\nContinue?`
                    );
                },
                choosePrecomputeOverwrite(requestOptions) {
                    const cache = this.selectedDataset && this.selectedDataset.cache ? this.selectedDataset.cache : null;
                    if (!cache) return true;
                    const prompts = [];
                    if (requestOptions.prepare_videos && !requestOptions.overwrite && Number(cache.videos?.cached || 0) > 0) {
                        prompts.push(`video cache ${cache.videos.cached}/${cache.videos.total}`);
                    }
                    if (requestOptions.prepare_csv && !requestOptions.overwrite && !requestOptions.overwrite_csv && Number(cache.csv?.cached || 0) > 0) {
                        prompts.push(`CSV cache ${cache.csv.cached}/${cache.csv.total}`);
                    }
                    if (prompts.length === 0) return true;
                    const overwrite = window.confirm(
                        `Existing ${prompts.join(' and ')} found.\n\nOK = overwrite existing cached files.\nCancel = keep existing files and only compute missing files.`
                    );
                    if (overwrite) {
                        if (requestOptions.prepare_videos) requestOptions.overwrite = true;
                        if (requestOptions.prepare_csv) requestOptions.overwrite_csv = true;
                    }
                    return true;
                },
                currentLabelVariantStatus() {
                    const labeling = this.selectedDataset && this.selectedDataset.labeling ? this.selectedDataset.labeling : null;
                    if (!labeling) return null;
                    const variants = labeling.variants || [];
                    return variants.find(item => item.id === this.currentLabelVariantId()) || null;
                },
                currentLabelVariantId() {
                    return `${this.labeling.backend}${this.labeling.trial ? '_trial' : ''}`;
                },
                resetDeleteFlagReasonState() {
                    this.deleteFlagReasonDatasetKey = '';
                    this.deleteFlagReasonLoading = false;
                    this.deleteFlagReasonOptions = [];
                    this.deleteFlagReasonEpisodes = {};
                    this.preprocess.delete_flag_reason = '';
                },
                routePathForDatasetKey(datasetKey) {
                    return String(datasetKey || '')
                        .split('/')
                        .map(part => encodeURIComponent(part))
                        .join('/');
                },
                deleteFlagReasonLabel(reason) {
                    const value = String(reason || '');
                    if (!value) return '';
                    if (this.deleteFlagReasonLabels && this.deleteFlagReasonLabels[value]) {
                        return this.deleteFlagReasonLabels[value];
                    }
                    return value.replace(/_/g, ' ');
                },
                deleteFlagReasonValues(items) {
                    const values = (items || [])
                        .map(item => item && item.reason ? String(item.reason) : 'unknown_flag_reason')
                        .filter(Boolean);
                    const unique = [...new Set(values)];
                    if (!unique.length) return ['unknown_flag_reason'];
                    return unique.length > 1 ? unique.filter(reason => reason !== 'manual_flag') : unique;
                },
                deleteFlagReasonCount(reason) {
                    const episodes = this.deleteFlagReasonEpisodes[String(reason || '')] || [];
                    return episodes.length;
                },
                flaggedDeleteCount() {
                    const ids = new Set();
                    Object.values(this.deleteFlagReasonEpisodes || {}).forEach(
                        episodes => (episodes || []).forEach(episode => ids.add(Number(episode)))
                    );
                    return ids.size;
                },
                parsedDeleteEpisodeIds() {
                    return this.parseEpisodeIdsText(this.preprocess.delete_episode_ids);
                },
                parseEpisodeIdsText(value) {
                    const text = String(value || '').trim();
                    if (!text) return [];
                    const ids = new Set();
                    const normalized = text.replace(/\s*-\s*/g, '-');
                    for (const token of normalized.split(/[\s,]+/).filter(Boolean)) {
                        if (/^\d+$/.test(token)) {
                            ids.add(Number(token));
                            continue;
                        }
                        const match = token.match(/^(\d+)-(\d+)$/);
                        if (!match || Number(match[2]) < Number(match[1])) return [];
                        for (let value = Number(match[1]); value <= Number(match[2]); value += 1) {
                            ids.add(value);
                        }
                    }
                    return [...ids].sort((a, b) => a - b);
                },
                deleteEpisodeImpactLabel() {
                    const count = this.parsedDeleteEpisodeIds().length;
                    const total = Number(this.selectedDataset && this.selectedDataset.episode_count || 0);
                    if (!count) return 'Enter valid episode ids or ranges to preview the impact.';
                    const remaining = Math.max(0, total - count);
                    return `${count} episodes will be deleted in-place; approximately ${remaining} will remain.`;
                },
                applyDeleteFlagReasonSelection() {
                    const reason = String(this.preprocess.delete_flag_reason || '');
                    if (!reason) return;
                    const episodes = this.deleteFlagReasonEpisodes[reason] || [];
                    this.preprocess.delete_episode_ids = episodes.join(',');
                },
                async loadDeleteFlagReasonOptions(force = false) {
                    if (this.deleteFlagReasonLoading) return;
                    let datasetKey = '';
                    try {
                        datasetKey = await this.ensureSelectedDatasetLoaded();
                        if (!datasetKey) return;
                        if (!force && this.deleteFlagReasonDatasetKey === datasetKey) {
                            return;
                        }
                        this.deleteFlagReasonLoading = true;
                        const locationId = this.selectedDataset?.remote_location_id;
                        const url = this.selectedDataset?.remote
                            ? `/api/control/locations/${encodeURIComponent(locationId)}/flagged-episodes`
                            : `/${this.routePathForDatasetKey(datasetKey)}/flagged_episodes?reasons=1`;
                        const data = await this.requestJson(url, null, 'GET');
                        const flaggedEpisodes = (data.flagged_episodes || [])
                            .map(value => Number(value))
                            .filter(Number.isFinite);
                        const reasonsByEpisode = data.flag_reasons || {};
                        const episodeMap = {};
                        flaggedEpisodes.forEach(episode => {
                            const values = this.deleteFlagReasonValues(reasonsByEpisode[String(episode)] || []);
                            values.forEach(reason => {
                                if (!episodeMap[reason]) episodeMap[reason] = [];
                                episodeMap[reason].push(episode);
                            });
                        });
                        Object.keys(episodeMap).forEach(reason => {
                            episodeMap[reason] = [...new Set(episodeMap[reason])].sort((a, b) => a - b);
                        });
                        this.deleteFlagReasonEpisodes = episodeMap;
                        this.deleteFlagReasonOptions = Object.keys(episodeMap)
                            .map(reason => ({
                                reason,
                                label: this.deleteFlagReasonLabel(reason),
                                count: episodeMap[reason].length
                            }))
                            .sort((a, b) => a.label.localeCompare(b.label, undefined, {numeric: true}));
                        this.deleteFlagReasonDatasetKey = datasetKey;
                        if (this.preprocess.delete_flag_reason && !episodeMap[this.preprocess.delete_flag_reason]) {
                            this.preprocess.delete_flag_reason = '';
                        }
                        this.applyDeleteFlagReasonSelection();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.deleteFlagReasonLoading = false;
                    }
                },
                editableFeatureOptions() {
                    return (this.selectedDataset && this.selectedDataset.editable_features) || [];
                },
                syncValueEditFeatures() {
                    const features = this.editableFeatureOptions();
                    if (!features.length) return;
                    for (const edit of this.preprocess.value_edits) {
                        let feature = features.find(item => item.key === edit.field);
                        if (!feature) {
                            feature = features[0];
                            edit.field = feature.key;
                            edit.dimension = 0;
                        }
                        if (Number(edit.dimension) >= Number(feature.dimension || 0)) {
                            edit.dimension = 0;
                        }
                    }
                },
                editableDimensionOptions(fieldName) {
                    const feature = this.editableFeatureOptions().find(item => item.key === fieldName);
                    if (!feature) return [];
                    const names = Array.isArray(feature.names) ? feature.names : [];
                    return Array.from({length: Number(feature.dimension || 0)}, (_, index) => ({
                        index,
                        label: names[index] ? `${index} · ${names[index]}` : String(index)
                    }));
                },
                addValueEdit() {
                    const firstFeature = this.editableFeatureOptions()[0];
                    this.preprocess.value_edits.push({
                        id: this.valueEditNextId++,
                        field: firstFeature ? firstFeature.key : 'action',
                        dimension: 0,
                        value: 0
                    });
                },
                removeValueEdit(index) {
                    if (this.preprocess.value_edits.length <= 1) return;
                    this.preprocess.value_edits.splice(index, 1);
                },
                normalizedValueEdits() {
                    return this.preprocess.value_edits.map(edit => ({
                        field: edit.field,
                        dimension: Number(edit.dimension),
                        value: Number(edit.value)
                    }));
                },
                deleteEpisodesDisabledReason() {
                    if (!this.selectedDataset) return 'Select a dataset first.';
                    if (this.busy) return 'Wait for the current request to finish.';
                    if (!this.operationAvailable('delete_episodes')) return this.operationReason('delete_episodes') || 'Episode deletion is unavailable for this dataset.';
                    if (!this.canRequestEpisodeDeletion()) {
                        if (this.selectedDataset?.remote) {
                            const reason = this.remoteMutationDisabledReason();
                            if (reason) return reason;
                        } else if (!this.canStartLegacyMutation()) {
                            return 'Source deletion requires Admin Mode and an unprotected dataset.';
                        }
                        if (this.selectedDataset.source_protected) return 'This source dataset is protected from deletion.';
                    }
                    if (this.deleteFlagReasonLoading) return 'Wait for flag types to finish loading.';
                    if (!this.parsedDeleteEpisodeIds().length) return 'Select a flag type with matching episodes or enter valid episode IDs.';
                    if (!this.preprocess.delete_reason.trim()) return 'Enter a deletion reason.';
                    return '';
                },
                canStartSchemaFix() {
                    if (this.preprocess.schema_op === 'delete_episodes') return !this.deleteEpisodesDisabledReason();
                    if (!this.operationAvailable(this.preprocess.schema_op)) return false;
                    if (!this.selectedDataset || this.busy) return false;
                    if (this.schemaOperationMutatesSource() && !this.canStartLegacyMutation()) return false;
                    if (this.selectedDataset.source_protected && this.schemaOperationMutatesSource()) return false;
                    if (this.preprocessCapabilities && !this.preprocessCapabilities.available) return false;
                    if (this.preprocess.schema_op === 'value_edit') {
                        if (
                            this.selectedDataset?.remote
                            && this.preprocess.value_output_mode === 'in_place'
                            && !this.preprocess.remote_mutation_reason.trim()
                        ) return false;
                        const features = new Map(this.editableFeatureOptions().map(item => [item.key, item]));
                        const edits = this.normalizedValueEdits();
                        const keys = new Set();
                        if (!edits.length) return false;
                        for (const edit of edits) {
                            const feature = features.get(edit.field);
                            const key = `${edit.field}:${edit.dimension}`;
                            if (
                                !feature
                                || !Number.isInteger(edit.dimension)
                                || edit.dimension < 0
                                || edit.dimension >= Number(feature.dimension || 0)
                                || !Number.isFinite(edit.value)
                                || keys.has(key)
                            ) return false;
                            keys.add(key);
                        }
                        return this.preprocess.value_episode_scope === 'all'
                            || this.parseEpisodeIdsText(this.preprocess.value_episode_ids).length > 0;
                    }
                    if (this.preprocess.schema_op === 'standardize') return true;
                    if (this.preprocess.schema_op === 'fix_prompt_prepositions') return true;
                    if (this.preprocess.schema_op === 'apply_prompt_assignments') return true;
                    if (this.preprocess.schema_op === 'rewrite_prompts') {
                        return Boolean(this.preprocess.prompt_pattern.trim() && this.preprocess.prompt_replacement.trim());
                    }
                    if (this.preprocess.schema_op === 'drop_field') return Boolean(this.preprocess.field_name.trim());
                    if (this.preprocess.schema_op === 'convert_v3') {
                        return !this.selectedDatasetIsV3()
                            && Number(this.preprocess.v3_data_file_size_mb || 0) > 0
                            && Number(this.preprocess.v3_video_file_size_mb || 0) > 0
                            && Number(this.preprocess.v3_workers || 0) > 0;
                    }
                    if (this.preprocess.schema_op === 'repair_v3_video_timestamps') {
                        return this.selectedDatasetIsV3();
                    }
                    if (this.preprocess.schema_op === 'smooth_action') {
                        const window = Number(this.preprocess.smooth_window || 0);
                        const workers = Number(this.preprocess.smooth_workers || 0);
                        return window > 0 && window % 2 === 1 && workers >= 0;
                    }
                    return Number(this.preprocess.target_dim || 0) > 0;
                },
                preprocessPanelTitle() {
                    if (this.activeTab === 'data_modification') return 'Data modification';
                    if (this.activeTab === 'standardize') return 'Standardize dataset';
                    if (this.activeTab === 'transform') return 'Feature transforms';
                    if (this.activeTab === 'dataset_ops') return 'Dataset operations';
                    return 'Preprocess';
                },
                preprocessPanelDescription() {
                    if (this.activeTab === 'data_modification') return 'Set one or more Action/State joint values for selected or all episodes.';
                    if (this.activeTab === 'standardize') return 'Build a training-ready sibling dataset with normalized 16D action/state and stage/subtask fields.';
                    if (this.activeTab === 'transform') return 'Generate sibling datasets with smaller schema/feature transformations.';
                    if (this.activeTab === 'dataset_ops') return 'Run dataset-level repairs or in-place maintenance actions.';
                    return '';
                },
                schemaActionLabel() {
                    if (this.preprocess.schema_op === 'value_edit') return this.preprocess.schema_dry_run ? 'Preview value edits' : 'Apply value edits';
                    if (this.preprocess.schema_op === 'delete_episodes') return this.canRequestEpisodeDeletion() ? 'Request episode deletion' : 'Delete episodes';
                    if (this.preprocess.schema_op === 'standardize') return this.preprocess.schema_dry_run ? 'Preview standardize' : 'Standardize dataset';
                    if (this.preprocess.schema_op === 'convert_action') return this.preprocess.schema_dry_run ? 'Preview conversion' : 'Convert action/state dim';
                    if (this.preprocess.schema_op === 'convert_v3') return this.preprocess.schema_dry_run ? 'Preview v3.0 conversion' : 'Convert dataset to v3.0';
                    if (this.preprocess.schema_op === 'repair_v3_video_timestamps') return this.preprocess.schema_dry_run ? 'Preview timestamp repair' : 'Repair v3 video timestamps';
                    if (this.preprocess.schema_op === 'drop_field') return this.preprocess.schema_dry_run ? 'Preview drop field' : 'Drop field';
                    if (this.preprocess.schema_op === 'smooth_action') return this.preprocess.schema_dry_run ? 'Preview smoothing' : 'Smooth action/state';
                    if (this.preprocess.schema_op === 'rewrite_prompts') return this.preprocess.schema_dry_run ? 'Preview prompt rewrite' : 'Rewrite prompts';
                    if (this.preprocess.schema_op === 'fix_prompt_prepositions') return this.preprocess.schema_dry_run ? 'Preview preposition fix' : 'Fix prepositions';
                    if (this.preprocess.schema_op === 'apply_prompt_assignments') return 'Apply pending prompts';
                    return 'Run preprocess op';
                },
                canStartQualityFlags() {
                    if (!this.operationAvailable('quality_flags')) return false;
                    if (!this.selectedDataset || this.busy) return false;
                    if (this.preprocessCapabilities && !this.preprocessCapabilities.available) return false;
                    return Number(this.preprocess.quality_workers || 0) > 0;
                },
                canStartFlagFix() {
                    if (!this.operationAvailable('flag_fixes')) return false;
                    if (!this.selectedDataset || this.busy) return false;
                    if (this.preprocessCapabilities && !this.preprocessCapabilities.available) return false;
                    return true;
                },
                canStartLegacyMutation() {
                    if (this.selectedDataset?.remote) return this.remoteSourceMutationsAvailable();
                    return Boolean(
                        this.legacyMutationsEnabled
                        && this.adminModeEnabled
                        && this.selectedDataset
                        && !this.selectedDataset.source_protected
                    );
                },
                schemaOperationMutatesSource() {
                    const op = this.preprocess.schema_op;
                    if (op === 'value_edit') return this.preprocess.value_output_mode === 'in_place';
                    return [
                        'delete_episodes',
                        'repair_v3_video_timestamps',
                        'rewrite_prompts',
                        'fix_prompt_prepositions',
                        'apply_prompt_assignments'
                    ].includes(op);
                },
                mergeMappingRows() {
                    const candidates = new Map(this.mergeCandidates().map(d => [d.key, d]));
                    return (this.preprocess.merge_src_keys || []).map(key => candidates.get(key)).filter(Boolean).flatMap(dataset => {
                        const features = dataset.merge_signal_features || dataset.features || {};
                        return ['action', 'state', 'observation.state'].filter(field => features[field]).map(field => {
                            const feature = features[field];
                            let names = feature.names;
                            while (names && !Array.isArray(names) && typeof names === 'object' && Object.keys(names).length === 1) names = Object.values(names)[0];
                            const dimension = Array.isArray(feature.shape) ? Number(feature.shape[0]) : Number(feature.shape);
                            const metadataText = Array.isArray(names) && names.length === dimension ? names.join(', ') : '';
                            return {id: `${dataset.key}:${field}`, key: dataset.key, field, dimension,
                                text: this.preprocess.merge_mapping_mode === 'explicit'
                                    ? (this.preprocess.merge_dimension_names?.[dataset.key]?.[field] ?? metadataText) : metadataText};
                        });
                    });
                },
                mergeIndexUsesAction(row) {
                    return this.preprocess.merge_index_shared && row.field !== 'action'
                        && this.mergeMappingRows().some(item => item.key === row.key && item.field === 'action');
                },
                mergeIndexText(row) {
                    const field = this.mergeIndexUsesAction(row) ? 'action' : row.field;
                    return this.preprocess.merge_index_positions?.[this.preprocess.merge_dimension_policy]?.[`${row.key}:${field}`] || '';
                },
                setMergeIndexText(row, text) {
                    this.preprocess.merge_index_positions[this.preprocess.merge_dimension_policy][row.id] = text;
                },
                parseMergeIndexPositions(text) {
                    if (!String(text).trim()) return [];
                    const result = new Set();
                    for (const token of String(text).split(',')) {
                        const match = token.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
                        if (!match) throw new Error('Use dimension numbers or ranges, e.g. 3,7,17-19.');
                        const start = Number(match[1]), end = Number(match[2] || match[1]);
                        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start || end - start + 1 > 10000) throw new Error('Use increasing ranges starting at 1 (at most 10000 positions per range).');
                        for (let i = start; i <= end; i++) {
                            if (result.has(i)) throw new Error(`Dimension ${i} is listed more than once.`);
                            result.add(i);
                        }
                    }
                    return [...result].sort((a, b) => a - b);
                },
                mergeIndexProjection(row) {
                    if (!Number.isSafeInteger(row.dimension) || row.dimension < 1) throw new Error('Signal dimension metadata is unavailable.');
                    const positions = this.parseMergeIndexPositions(this.mergeIndexText(row));
                    const pad = this.preprocess.merge_dimension_policy === 'pad';
                    const size = row.dimension + (pad ? positions.length : 0);
                    if (positions.some(position => position > size)) throw new Error(`Position exceeds ${size} ${pad ? 'output' : 'source'} dimensions.`);
                    const selected = new Set(positions);
                    let source = 0;
                    const indices = pad
                        ? Array.from({length: size}, (_, i) => selected.has(i + 1) ? null : source++)
                        : Array.from({length: size}, (_, i) => i).filter(i => !selected.has(i + 1));
                    if (!indices.length) throw new Error('Keep at least one dimension.');
                    return indices;
                },
                mergeIndexRowError(row) {
                    try { this.mergeIndexProjection(row); return ''; } catch (err) { return err.message; }
                },
                mergeIndexSummary(row, detailed = false) {
                    try {
                        const indices = this.mergeIndexProjection(row);
                        const positions = this.parseMergeIndexPositions(this.mergeIndexText(row));
                        const verb = this.preprocess.merge_dimension_policy === 'pad' ? 'pad output positions' : 'delete source dimensions';
                        const summary = `${row.dimension}D → ${indices.length}D; ${positions.length ? `${verb} ${positions.join(', ')}` : 'unchanged'}`;
                        return detailed ? `${summary}; output ← source: ${indices.map((index, i) => `${i + 1}←${index === null ? `fill(${this.preprocess.merge_padding_value})` : index + 1}`).join(', ')}` : summary;
                    } catch (err) { return err.message; }
                },
                mergeIndexMappings() {
                    const rows = this.mergeMappingRows();
                    const mappings = this.preprocess.merge_src_keys.map(key => {
                        const selected = rows.filter(row => row.key === key);
                        if (!selected.length) throw new Error('Signal metadata is unavailable; refresh the selected datasets.');
                        return Object.fromEntries(selected.map(row => [row.field, this.mergeIndexProjection(row)]));
                    });
                    for (const field of new Set(rows.map(row => row.field))) {
                        const group = mappings.map(mapping => mapping[field]);
                        if (group.some(indices => !indices) || new Set(group.map(indices => indices.length)).size !== 1) throw new Error(`${field}: all sources must have the same output dimension count.`);
                        if (group[0].some((_, i) => group.every(indices => indices[i] === null))) throw new Error(`${field}: at least one source must provide a real signal at every output position.`);
                    }
                    return mappings;
                },
                mergeIndexError() {
                    try { this.mergeIndexMappings(); return ''; } catch (err) { return err.message; }
                },
                mergeIndexCells(row) {
                    let positions;
                    try { positions = this.parseMergeIndexPositions(this.mergeIndexText(row)); } catch { return []; }
                    const pad = this.preprocess.merge_dimension_policy === 'pad';
                    const size = pad ? Math.max(row.dimension + positions.length + 1, ...this.mergeMappingRows().filter(item => item.field === row.field).map(item => item.dimension)) : row.dimension;
                    // Large signals remain editable through the text input without rendering thousands of buttons.
                    return Array.from({length: Math.min(size, 256)}, (_, i) => ({position: i + 1, selected: positions.includes(i + 1), label: `${pad ? 'Toggle padding at output' : 'Toggle deletion of source'} dimension ${i + 1}`}));
                },
                toggleMergeIndex(row, position) {
                    const selected = new Set(this.parseMergeIndexPositions(this.mergeIndexText(row)));
                    if (selected.has(position)) selected.delete(position); else selected.add(position);
                    this.setMergeIndexText(row, [...selected].sort((a, b) => a - b).join(', '));
                },
                setMergeDimensionNames(key, field, value) {
                    this.preprocess.merge_dimension_names[key] = {...this.preprocess.merge_dimension_names[key], [field]: value};
                },
                mergeMappingSignature() {
                    return JSON.stringify([(this.preprocess.merge_src_keys || []), this.mergeMappingRows()]);
                },
                applyMergeLayoutPreset() {
                    const prefix = Number(this.preprocess.merge_prefix_dims);
                    const tail = Number(this.preprocess.merge_tail_dims);
                    const rows = this.mergeMappingRows();
                    if (!Number.isInteger(prefix) || !Number.isInteger(tail) || prefix < 0 || tail < 0 || prefix + tail < 1 || !rows.length || rows.some(r => !Number.isInteger(r.dimension) || r.dimension < prefix + tail)) {
                        this.error = 'The shared prefix and tail must fit every selected signal. Check source metadata and dimension counts.';
                        return;
                    }
                    this.preprocess.merge_mapping_confirmation = '';
                    for (const row of rows) {
                        const names = [
                            ...Array.from({length: prefix}, (_, i) => `shared_${i + 1}`),
                            ...Array.from({length: row.dimension - prefix - tail}, (_, i) => `middle_${i + 1}`),
                            ...Array.from({length: tail}, (_, i) => `tail_${i + 1}`)
                        ];
                        this.setMergeDimensionNames(row.key, row.field, names.join(', '));
                    }
                },
                mergeAlignmentOptions() {
                    const policy = this.preprocess.merge_dimension_policy;
                    if (policy === 'strict') return {};
                    const rows = this.mergeMappingRows();
                    const options = {};
                    if (policy === 'pad') {
                        const value = this.preprocess.merge_padding_value;
                        if (value === '' || !Number.isFinite(Number(value))) throw new Error('Padding value must be a finite number.');
                        options.padding_value = Number(value);
                    }
                    if (this.preprocess.merge_mapping_mode === 'indices') options.dimension_indices = this.mergeIndexMappings();
                    if (this.preprocess.merge_mapping_mode === 'explicit') {
                        if (!rows.length) throw new Error('Signal metadata is unavailable; refresh the selected datasets.');
                        if (this.preprocess.merge_mapping_confirmation !== this.mergeMappingSignature()) throw new Error('Review and confirm the explicit dimension correspondence.');
                        options.dimension_names = this.preprocess.merge_src_keys.map(key => {
                            const selected = rows.filter(r => r.key === key);
                            if (!selected.length) throw new Error(`Missing signal metadata for ${key}`);
                            return Object.fromEntries(selected.map(row => {
                                const names = row.text.split(',').map(n => n.trim());
                                if (names.length !== row.dimension || names.some(n => !n) || new Set(names).size !== names.length) throw new Error(`${row.field}: enter ${row.dimension} unique names for ${key}.`);
                                return [row.field, names];
                            }));
                        });
                    }
                    return options;
                },
                mergeAlignmentPreview() {
                    if (this.preprocess.merge_mapping_mode === 'indices') return this.mergeMappingRows().map(row => ({id: row.id, text: `${this.mergeDatasetLabel(row.key)} · ${row.field}: ${this.mergeIndexSummary(row)}`}));
                    const rows = this.mergeMappingRows();
                    const result = [];
                    for (const field of ['action', 'state', 'observation.state']) {
                        const group = rows.filter(r => r.field === field).map(r => ({...r, names: r.text.split(',').map(n => n.trim())}));
                        if (!group.length) continue;
                        if (group.some(r => r.names.length !== r.dimension || r.names.some(n => !n) || new Set(r.names).size !== r.names.length)) {
                            result.push({id: field, text: `${field}: complete unique names are missing; configure explicit mapping.`});
                            continue;
                        }
                        const widest = group.reduce((a, b) => a.dimension >= b.dimension ? a : b);
                        const shortest = group.reduce((a, b) => a.dimension <= b.dimension ? a : b);
                        const target = this.preprocess.merge_dimension_policy === 'pad'
                            ? [...new Set([...widest.names, ...group.flatMap(r => r.names)])] : shortest.names;
                        for (const row of group) {
                            const mapping = target.map((name, i) => `${i + 1}←${row.names.includes(name) ? row.names.indexOf(name) + 1 : (this.preprocess.merge_dimension_policy === 'pad' ? `fill(${this.preprocess.merge_padding_value})` : 'MISSING')}`);
                            const dropped = row.names.map((name, i) => target.includes(name) ? null : i + 1).filter(i => i !== null);
                            result.push({id: row.id, text: `${this.mergeDatasetLabel(row.key)} · ${field}: ${row.dimension}→${target.length}D; ${mapping.join(', ')}${dropped.length ? `; drop ${dropped.join(', ')}` : ''}`});
                        }
                    }
                    return result;
                },
                mergeSelectionDisabledReason() {
                    const keys = this.preprocess.merge_src_keys || [];
                    if (keys.length < 2) return 'Select at least two source datasets.';
                    const candidates = new Map(this.mergeCandidates().map(item => [item.key, item]));
                    if (!keys.every(key => candidates.has(key))) {
                        return this.selectedDataset?.remote
                            ? 'All sources must be available on the same Agent.'
                            : 'One or more selected source datasets are no longer available.';
                    }
                    const unsupported = keys.map(key => candidates.get(key))
                        .find(dataset => !this.operationAvailable('merge', dataset));
                    if (unsupported) {
                        const reason = unsupported.operation_capabilities?.merge?.reason || 'Merge is unavailable for this dataset.';
                        return `${unsupported.repo_id || unsupported.key}: ${reason}`;
                    }
                    return '';
                },
                splitMergeDisabledReason() {
                    const op = this.preprocess.split_merge_op;
                    if (op === 'merge' && this.mergeOutputError()) return this.mergeOutputError();
                    if (op === 'merge' && this.preprocess.merge_dimension_policy !== 'strict' && (this.preprocess.merge_dimension_policy === 'pad' || ['explicit', 'indices'].includes(this.preprocess.merge_mapping_mode))) {
                        if (this.selectedDataset?.remote && this.preprocess.merge_mapping_mode === 'indices' && (this.selectedRemoteNode()?.capabilities?.merge_alignment_protocol || 0) < 3) return 'Upgrade this Agent to use dimension positions (alignment protocol 3).';
                        if (this.selectedDataset?.remote && (this.selectedRemoteNode()?.capabilities?.merge_alignment_protocol || 0) < 2) return 'Upgrade this Agent for explicit dimension mapping and padding (alignment protocol 2).';
                        try { this.mergeAlignmentOptions(); } catch (err) { return err.message; }
                    }
                    if (this.busy) return 'A request is in progress.';
                    if (!this.selectedDataset) return 'Select a working dataset first.';
                    if (op !== 'merge' && !this.operationAvailable(op)) return this.operationReason(op) || 'This operation is unavailable for this dataset.';
                    if (this.selectedDataset.remote) {
                        if (!this.canOperateRemote()) return 'An admin or operator account is required.';
                        if (op === 'merge') {
                            if (!this.remoteMergeAvailable()) return 'The selected Agent does not advertise merge support. Check its connection and version.';
                            return this.mergeSelectionDisabledReason();
                        }
                        if (op !== 'split') return 'This operation is unavailable on remote datasets.';
                        if (!this.selectedRemoteLocation) return 'Select an available Agent dataset.';
                    } else if (this.preprocessCapabilities && !this.preprocessCapabilities.available) {
                        return this.preprocessCapabilities?.error || 'Local preprocessing dependencies are unavailable.';
                    }
                    if (op === 'split' && !(this.preprocess.episode_range.trim() || this.preprocess.task_filter.trim())) return 'Enter an episode range or task filter.';
                    if (op === 'subtract' && !(this.preprocess.subtract_src_keys || []).length) return 'Select at least one dataset to subtract.';
                    if (op === 'merge') return this.mergeSelectionDisabledReason();
                    return '';
                },
                canStartSplitMerge() {
                    return !this.splitMergeDisabledReason();
                },
                async startPreprocessJob(url, payload, datasetKey = null, confirmOverwrite = false) {
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson(url, payload);
                        this.upsertJob(data.job);
                        if (!datasetKey || this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        if (
                            confirmOverwrite
                            && err.status === 409
                            && err.payload
                            && err.payload.requires_overwrite_confirmation
                        ) {
                            const outputRoot = err.payload.output_root || 'the output directory';
                            if (confirm(`Output dataset already exists:\n${outputRoot}\n\nOverwrite it?`)) {
                                payload.options = payload.options || {};
                                payload.options.overwrite = true;
                                return await this.startPreprocessJob(url, payload, datasetKey, false);
                            }
                            return;
                        }
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async submitRemoteJob(endpoint, payload) {
                    const current = consoleReadScope(this, null, "dataset");
                    this.error = '';
                    this.remoteError = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson(endpoint, payload);
                        this.mergeRemoteJob(data.job);
                        if (!current()) {this.syncActiveJob(); return;}
                        this.remoteViewerJob = data.job;
                        this.activeJob = this.remoteJobForRuns(data.job);
                        this.selectedJobId = data.job.job_id;
                        this.scheduleRemoteRefresh();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                confirmRemoteMutation(description) {
                    if (!this.selectedRemoteLocation || !this.remoteSourceMutationsAvailable()) return null;
                    const expected = `MUTATE ${this.selectedRemoteLocation.dataset_key}`;
                    const accepted = window.confirm(
                        `${description}\n\nThis modifies the Agent source dataset in-place. `
                        + 'The Agent creates a persistent recovery backup before writing.'
                    );
                    if (!accepted) return null;
                    return window.prompt('Type exactly to confirm:\nMUTATE') === 'MUTATE' ? expected : null;
                },
                async startRemoteMutation(op, options, description) {
                    if (op === 'delete_episodes' && this.canRequestEpisodeDeletion()) {
                        if (!window.confirm(`Request administrator approval to delete episodes ${options.episodes.join(', ')}?`)) return;
                        this.busy = true;
                        try {
                            await this.requestJson(`/api/control/locations/${this.selectedRemoteLocation.location_id}/episode-deletion-requests`, options);
                            await this.loadDeletionRequests();
                        } catch (err) {
                            this.error = err.message;
                        } finally {
                            this.busy = false;
                        }
                        return;
                    }
                    const confirmation = this.confirmRemoteMutation(description);
                    if (!confirmation) return;
                    await this.submitRemoteJob(
                        `/api/control/locations/${this.selectedRemoteLocation.location_id}/mutation-jobs`,
                        {op, options, confirmation}
                    );
                },
                async startRemoteSchemaFix() {
                    if (!this.selectedRemoteLocation || !this.selectedDataset?.remote) return;
                    const op = this.preprocess.schema_op;
                    if (op === 'delete_episodes') {
                        const episodeIds = this.parsedDeleteEpisodeIds();
                        if (!episodeIds.length || !this.preprocess.delete_reason.trim()) return;
                        await this.startRemoteMutation(
                            'delete_episodes',
                            {
                                episodes: episodeIds,
                                reason: this.preprocess.delete_reason.trim()
                            },
                            `Delete ${episodeIds.length} episode(s) (${episodeIds.join(', ')}) from ${this.selectedRemoteLocation.dataset_key}?`
                        );
                        return;
                    }
                    if (op === 'repair_v3_video_timestamps') {
                        await this.startRemoteMutation(
                            'repair_v3_video_timestamps',
                            {
                                dry_run: this.preprocess.schema_dry_run,
                                reason: this.preprocess.remote_mutation_reason.trim()
                            },
                            `Repair v3 video timestamps in ${this.selectedRemoteLocation.dataset_key}?`
                        );
                        return;
                    }
                    if (op === 'value_edit' && this.preprocess.value_output_mode === 'in_place') {
                        const episodeIds = this.preprocess.value_episode_scope === 'all'
                            ? null
                            : this.parseEpisodeIdsText(this.preprocess.value_episode_ids);
                        if (this.preprocess.value_episode_scope !== 'all' && !episodeIds.length) return;
                        await this.startRemoteMutation(
                            'value_edit',
                            {
                                edits: this.normalizedValueEdits(),
                                episode_ids: episodeIds,
                                dry_run: this.preprocess.schema_dry_run,
                                reason: this.preprocess.remote_mutation_reason.trim()
                            },
                            `Apply ${this.preprocess.value_edits.length} value edit(s) to ${this.selectedRemoteLocation.dataset_key}?`
                        );
                        return;
                    }
                    const options = {dry_run: this.preprocess.schema_dry_run};
                    if (op === 'value_edit') {
                        options.edits = this.normalizedValueEdits();
                        options.episode_ids = this.preprocess.value_episode_scope === 'all'
                            ? null
                            : this.parseEpisodeIdsText(this.preprocess.value_episode_ids);
                        if (this.preprocess.value_episode_scope !== 'all' && !options.episode_ids.length) return;
                    } else if (op === 'convert_v3') {
                        options.data_file_size_in_mb = Number(this.preprocess.v3_data_file_size_mb);
                        options.video_file_size_in_mb = Number(this.preprocess.v3_video_file_size_mb);
                        options.workers = Number(this.preprocess.v3_workers);
                        options.image_video_mode = this.preprocess.v3_image_video_mode;
                        options.overwrite = Boolean(this.preprocess.schema_overwrite);
                    } else if (op === 'drop_field') {
                        options.field_name = this.preprocess.field_name.trim();
                    } else if (op === 'smooth_action') {
                        options.window = Number(this.preprocess.smooth_window);
                        options.workers = Number(this.preprocess.smooth_workers);
                        options.smooth_state = Boolean(this.preprocess.smooth_state);
                    } else if (op === 'standardize') {
                        options.data_version = this.preprocess.standardize_data_version;
                        options.workers = Number(this.options.prepare_workers || 8);
                        options.overwrite_output = Boolean(this.preprocess.standardize_overwrite);
                        options.delete_episodes = this.preprocess.standardize_delete_episode_ids.trim();
                    } else if (op === 'convert_action') {
                        options.target_dim = Number(this.preprocess.target_dim);
                    } else {
                        this.error = `Remote operation is not supported: ${op}`;
                        return;
                    }
                    const outputRoot = op === 'value_edit'
                        ? this.preprocess.value_out_root.trim()
                        : this.preprocess.schema_out_root.trim();
                    if (outputRoot) options.out_root = outputRoot;
                    await this.submitRemoteJob(
                        `/api/control/locations/${this.selectedRemoteLocation.location_id}/preprocess-jobs`,
                        {op, options}
                    );
                },
                async ensureSelectedDatasetLoaded() {
                    if (!this.selectedDataset) return null;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (this.selectedDatasetKey !== datasetKey) return null;
                    }
                    return datasetKey;
                },
                async startSchemaFix() {
                    if (this.selectedDataset?.remote) {
                        await this.startRemoteSchemaFix();
                        return;
                    }
                    const datasetKey = this.preprocess.schema_op === 'convert_v3'
                        ? (this.selectedDataset && this.selectedDataset.key)
                        : await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    if (this.preprocess.schema_op === 'value_edit') {
                        const edits = this.normalizedValueEdits();
                        const scopeText = this.preprocess.value_episode_scope === 'all'
                            ? 'all episodes'
                            : `episodes ${this.preprocess.value_episode_ids.trim()}`;
                        if (
                            !this.preprocess.schema_dry_run
                            && this.preprocess.value_output_mode === 'in_place'
                            && !confirm(
                                `Apply ${edits.length} value edit(s) to ${scopeText} in ${datasetKey}?\n\n`
                                + 'This modifies the source dataset in-place and cannot be undone by Data Platform.'
                            )
                        ) return;
                        await this.startPreprocessJob('/api/preprocess/value_edit/start', {
                            dataset_key: datasetKey,
                            options: {
                                edits,
                                episode_scope: this.preprocess.value_episode_scope,
                                episodes: this.preprocess.value_episode_ids,
                                output_mode: this.preprocess.value_output_mode,
                                out_root: this.preprocess.value_out_root,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'convert_v3') {
                        await this.startPreprocessJob('/api/preprocess/convert_v3/start', {
                            dataset_key: datasetKey,
                            options: {
                                out_root: this.preprocess.schema_out_root,
                                data_file_size_in_mb: this.preprocess.v3_data_file_size_mb,
                                video_file_size_in_mb: this.preprocess.v3_video_file_size_mb,
                                workers: this.preprocess.v3_workers,
                                image_video_mode: this.preprocess.v3_image_video_mode,
                                overwrite: this.preprocess.schema_overwrite,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey, true);
                        return;
                    }
                    if (this.preprocess.schema_op === 'repair_v3_video_timestamps') {
                        if (
                            !this.preprocess.schema_dry_run
                            && !confirm(
                                `Repair v3 video timestamps for ${datasetKey}?\n\n`
                                + 'MP4 containers and meta/episodes timestamps will be rewritten. Videos are not re-encoded.'
                            )
                        ) return;
                        await this.startPreprocessJob(
                            '/api/preprocess/repair_v3_video_timestamps/start',
                            {
                                dataset_key: datasetKey,
                                options: {dry_run: this.preprocess.schema_dry_run}
                            },
                            datasetKey
                        );
                        return;
                    }
                    if (this.preprocess.schema_op === 'delete_episodes') {
                        if (!this.canStartLegacyMutation()) return;
                        if (this.preprocess.delete_flag_reason && this.deleteFlagReasonDatasetKey !== datasetKey) {
                            await this.loadDeleteFlagReasonOptions(true);
                        } else {
                            this.applyDeleteFlagReasonSelection();
                        }
                        const ids = this.preprocess.delete_episode_ids.trim();
                        const parsedIds = this.parsedDeleteEpisodeIds();
                        const deletionReason = this.preprocess.delete_reason.trim();
                        if (!ids || !parsedIds.length || !deletionReason) return;
                        const reason = String(this.preprocess.delete_flag_reason || '');
                        const reasonText = reason
                            ? `\n\nFlag type: ${this.deleteFlagReasonLabel(reason)} (${this.deleteFlagReasonCount(reason)} episodes)`
                            : '';
                        const confirmed = confirm(
                            `Delete ${parsedIds.length} episodes (${ids}) from ${datasetKey}?`
                            + reasonText
                            + `\n\nReason: ${deletionReason}`
                            + '\n\nThis modifies the source dataset and cannot be undone.'
                        );
                        if (!confirmed) return;
                        if (prompt(`Type DELETE ${parsedIds.length} to confirm.`) !== `DELETE ${parsedIds.length}`) return;
                        await this.startPreprocessJob('/api/preprocess/delete_episodes/start', {
                            dataset_key: datasetKey,
                            options: {
                                episodes: ids,
                                reason: deletionReason
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'drop_field') {
                        await this.startPreprocessJob('/api/preprocess/drop_field/start', {
                            dataset_key: datasetKey,
                            options: {
                                field_name: this.preprocess.field_name,
                                out_root: this.preprocess.schema_out_root,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'smooth_action') {
                        await this.startPreprocessJob('/api/preprocess/smooth_action/start', {
                            dataset_key: datasetKey,
                            options: {
                                window: this.preprocess.smooth_window,
                                workers: this.preprocess.smooth_workers,
                                smooth_state: this.preprocess.smooth_state,
                                out_root: this.preprocess.schema_out_root,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'standardize') {
                        await this.startPreprocessJob('/api/preprocess/standardize/start', {
                            dataset_key: datasetKey,
                            options: {
                                out_root: this.preprocess.schema_out_root,
                                overwrite_output: this.preprocess.standardize_overwrite,
                                data_version: this.preprocess.standardize_data_version,
                                delete_episodes: this.preprocess.standardize_delete_episode_ids,
                                prepare_workers: this.options.prepare_workers,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'rewrite_prompts') {
                        if (!this.preprocess.schema_dry_run) {
                            const ok = confirm(`Rewrite prompts in ${datasetKey}?\n\nThis modifies task and episode metadata (v2.1 JSONL or v3 Parquet) in-place. A backup will be created.`);
                            if (!ok) return;
                        }
                        await this.startPreprocessJob('/api/preprocess/rewrite_prompts/start', {
                            dataset_key: datasetKey,
                            options: {
                                pattern: this.preprocess.prompt_pattern,
                                replacement: this.preprocess.prompt_replacement,
                                backup: true,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'fix_prompt_prepositions') {
                        if (!this.preprocess.schema_dry_run) {
                            const ok = confirm(`Fix prompt prepositions in ${datasetKey}?\n\nAbsolute: on the left/right.\nRelative: to the left/right of ...\n\nThis modifies task and episode metadata (v2.1 JSONL or v3 Parquet) in-place. A backup will be created.`);
                            if (!ok) return;
                        }
                        await this.startPreprocessJob('/api/preprocess/fix_prompt_prepositions/start', {
                            dataset_key: datasetKey,
                            options: {
                                backup: true,
                                dry_run: this.preprocess.schema_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.schema_op === 'apply_prompt_assignments') {
                        const episodes = this.preprocess.apply_prompt_assignment_episode_ids.trim();
                        const message = episodes
                            ? `Apply pending prompt repairs for episodes ${episodes} in ${datasetKey}?\n\nThis modifies episode metadata and Parquet task_index in-place.`
                            : `Apply all pending prompt repairs in ${datasetKey}?\n\nThis modifies episode metadata and Parquet task_index in-place.`;
                        if (!confirm(message)) return;
                        await this.startPreprocessJob('/api/preprocess/apply_prompt_assignments/start', {
                            dataset_key: datasetKey,
                            options: {
                                episodes: episodes
                            }
                        }, datasetKey);
                        return;
                    }
                    await this.startPreprocessJob('/api/preprocess/convert_action/start', {
                        dataset_key: datasetKey,
                        options: {
                            target_dim: this.preprocess.target_dim,
                            out_root: this.preprocess.schema_out_root,
                            dry_run: this.preprocess.schema_dry_run
                        }
                    }, datasetKey);
                },
                async startQualityFlags() {
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    if (this.preprocess.quality_overwrite) {
                        const manualText = this.preprocess.quality_clear_manual_flags
                            ? '\\n\\nManual flags will also be cleared.'
                            : '\\n\\nManual flags are preserved.';
                        const ok = window.confirm(
                            'Run abnormal flag detection?'
                            + (this.preprocess.quality_overwrite
                                ? '\\n\\nThis clears old automatic abnormal flags and prompt/action mismatch flags before rebuilding them.'
                                : '')
                            + manualText
                        );
                        if (!ok) return;
                    }
                    await this.startPreprocessJob('/api/preprocess/quality_flags/start', {
                        dataset_key: datasetKey,
                        options: {
                            data_version: this.normalizeDataVersion(
                                this.preprocess.quality_data_version,
                                this.defaultProcessingDataVersion(this.selectedDataset)
                            ),
                            workers: this.preprocess.quality_workers,
                            episodes: this.preprocess.quality_episode_ids,
                            overwrite: this.preprocess.quality_overwrite,
                            clear_manual_flags: this.preprocess.quality_overwrite && this.preprocess.quality_clear_manual_flags
                        }
                    }, datasetKey);
                },
                async startLowercasePrompts() {
                    if (!this.canStartLegacyMutation()) return;
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    const ok = window.confirm(
                        `Normalize prompts to lowercase in ${datasetKey}?\\n\\nThis modifies task/episode metadata, pending viewer prompt assignments, and Parquet task_index when duplicate tasks merge. A metadata backup will be created.`
                    );
                    if (!ok) return;
                    await this.startPreprocessJob('/api/preprocess/lowercase_prompts/start', {
                        dataset_key: datasetKey,
                    }, datasetKey);
                },
                async startClearFlags() {
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    const ok = window.confirm(
                        `Clear all manual and automatic flags for ${datasetKey}?\\n\\nThis removes viewer flag markers and flag issues. It does not delete episodes.`
                    );
                    if (!ok) return;
                    await this.startPreprocessJob('/api/preprocess/clear_flags/start', {
                        dataset_key: datasetKey,
                    }, datasetKey);
                },
                async startFlagFix(fixKind) {
                    if (!this.canStartLegacyMutation()) return;
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    const labels = {
                        trim_early_gripper_first_frame: 'trim the first frame for all early_gripper_transition issues',
                        fix_stuck_closed_action: 'set gripper action to closed for all stuck_closed_gripper_no_action issues',
                        fix_state_gripper_transition_action: 'add gripper action transitions shortly before matching state-only gripper transitions',
                        delete_all_flagged: 'delete all flagged episodes'
                    };
                    const actionLabel = labels[fixKind] || fixKind;
                    const destructive = fixKind === 'delete_all_flagged';
                    let deletionReason = '';
                    let deleteCount = 0;
                    if (destructive) {
                        await this.loadDeleteFlagReasonOptions(true);
                        deletionReason = this.preprocess.flag_delete_reason.trim();
                        deleteCount = this.flaggedDeleteCount();
                        if (!deletionReason || !deleteCount) return;
                    }
                    const message = destructive
                        ? `This will delete ${deleteCount} flagged episodes in ${datasetKey}.`
                            + `\n\nReason: ${deletionReason}`
                            + '\n\nThis modifies the source dataset and cannot be undone. Continue?'
                        : `This will ${actionLabel} in ${datasetKey}. Continue?`;
                    if (!window.confirm(message)) return;
                    if (destructive && window.prompt(`Type DELETE ${deleteCount} to confirm.`) !== `DELETE ${deleteCount}`) return;
                    await this.startPreprocessJob('/api/preprocess/flag_fixes/start', {
                        dataset_key: datasetKey,
                        options: {
                            fix_kind: fixKind,
                            data_version: this.normalizeDataVersion(
                                this.preprocess.quality_data_version,
                                this.defaultProcessingDataVersion(this.selectedDataset)
                            ),
                            episodes: this.preprocess.quality_episode_ids,
                            reason: deletionReason
                        }
                    }, datasetKey);
                },
                async startSplitMerge() {
                    if (!this.canStartSplitMerge()) {
                        this.error = this.splitMergeDisabledReason();
                        return;
                    }
                    if (this.selectedDataset?.remote) {
                        if (this.preprocess.split_merge_op === 'merge') {
                            if (!this.canStartSplitMerge()) {
                                this.error = this.splitMergeDisabledReason();
                                return;
                            }
                            const candidates = this.mergeCandidates();
                            const sources = this.preprocess.merge_src_keys.map(key => candidates.find(item => item.key === key));
                            const exclusions = this.mergeDeleteEpisodesPayload();
                            await this.submitRemoteJob(
                                `/api/control/locations/${sources[0].remote_location_id}/preprocess-jobs`,
                                {op: 'merge', options: {
                                    source_location_ids: sources.map(item => item.remote_location_id),
                                    dimension_policy: this.preprocess.merge_dimension_policy,
                                    ...this.mergeAlignmentOptions(),
                                    exclude_episodes: sources.map(item => exclusions[item.key] || []),
                                    workers: this.preprocess.merge_workers,
                                    out_root: this.preprocess.merge_out_root.trim() || null,
                                    dry_run: this.preprocess.merge_dry_run
                                }}
                            );
                            return;
                        }
                        if (!this.selectedRemoteLocation || this.preprocess.split_merge_op !== 'split') return;
                        await this.submitRemoteJob(
                            `/api/control/locations/${this.selectedRemoteLocation.location_id}/preprocess-jobs`,
                            {
                                op: 'split',
                                options: {
                                    episode_range: this.preprocess.episode_range.trim() || null,
                                    task_filter: this.preprocess.task_filter.trim() || null,
                                    out_root: this.preprocess.split_out_root.trim() || null,
                                    dry_run: this.preprocess.split_dry_run
                                }
                            }
                        );
                        return;
                    }
                    if (this.preprocess.split_merge_op === 'subtract') {
                        const datasetKey = await this.ensureSelectedDatasetLoaded();
                        if (!datasetKey) return;
                        await this.startPreprocessJob('/api/preprocess/subtract/start', {
                            dataset_key: datasetKey,
                            options: {
                                src_keys: this.preprocess.subtract_src_keys,
                                out_name: this.preprocess.subtract_out_root,
                                root_dir: this.rootDir,
                                workers: this.preprocess.subtract_workers,
                                dry_run: this.preprocess.subtract_dry_run
                            }
                        }, datasetKey);
                        return;
                    }
                    if (this.preprocess.split_merge_op === 'merge') {
                        await this.startPreprocessJob('/api/preprocess/merge/start', {
                            options: {
                                src_keys: this.preprocess.merge_src_keys,
                                out_name: this.preprocess.merge_out_root,
                                root_dir: this.rootDir,
                                workers: this.preprocess.merge_workers,
                                dimension_policy: this.preprocess.merge_dimension_policy,
                                ...this.mergeAlignmentOptions(),
                                delete_episodes_by_key: this.mergeDeleteEpisodesPayload(),
                                dry_run: this.preprocess.merge_dry_run
                            }
                        });
                        return;
                    }
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    await this.startPreprocessJob('/api/preprocess/split/start', {
                        dataset_key: datasetKey,
                        options: {
                            episode_range: this.preprocess.episode_range,
                            task_filter: this.preprocess.task_filter,
                            out_root: this.preprocess.split_out_root,
                            dry_run: this.preprocess.split_dry_run
                        }
                    }, datasetKey);
                },
                async startPrecompute(overrides = {}) {
                    if (!this.selectedDataset?.data_version) this.options.data_version = null;
                    if (!this.selectedDataset) return;
                    if (this.selectedDataset.remote) {
                        if (!this.selectedRemoteLocation) return;
                        const requestOptions = {...this.options, ...overrides};
                        const episodeText = String(requestOptions.episodes || '').trim();
                        let episodes = null;
                        if (episodeText && episodeText.toLowerCase() !== 'all') {
                            episodes = this.parseEpisodeIdsText(episodeText);
                            if (!episodes.length) {
                                this.error = 'Enter valid episode ids or ranges, or leave episodes empty for all.';
                                return;
                            }
                        }
                        const workers = Number(requestOptions.prepare_workers || 4);
                        const remoteOptions = {
                            episodes,
                            data_version: this.normalizeDataVersion(
                                requestOptions.data_version,
                                this.defaultProcessingDataVersion(this.selectedDataset)
                            ),
                            prepare_videos: Boolean(requestOptions.prepare_videos),
                            prepare_csv: Boolean(requestOptions.prepare_csv),
                            overwrite: Boolean(requestOptions.overwrite),
                            overwrite_csv: Boolean(requestOptions.overwrite_csv),
                            force_recompute_stage: Boolean(requestOptions.force_recompute_stage),
                            fallback_stage_count: Number(requestOptions.fallback_stage_count || 5),
                            prepare_workers: Number.isFinite(workers) && workers > 0 ? workers : 4
                        };
                        const downsample = Number(requestOptions.downsample);
                        if (String(requestOptions.downsample ?? '').trim() && Number.isFinite(downsample)) {
                            remoteOptions.downsample = downsample;
                        }
                        const maxFrames = Number(requestOptions.max_frames);
                        if (String(requestOptions.max_frames ?? '').trim() && Number.isFinite(maxFrames)) {
                            remoteOptions.max_frames = maxFrames;
                        }
                        await this.prepareRemoteViewer(this.selectedRemoteLocation, remoteOptions);
                        return;
                    }
                    const datasetKey = this.selectedDataset.key;
                    const requestedDataVersion = this.normalizeDataVersion(
                        this.options.data_version,
                        this.defaultProcessingDataVersion(this.selectedDataset)
                    );
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (this.selectedDatasetKey !== datasetKey) return;
                    }
                    const requestOptions = {...this.options, ...overrides};
                    requestOptions.data_version = this.normalizeDataVersion(
                        requestedDataVersion,
                        this.defaultProcessingDataVersion(this.selectedDataset)
                    );
                    this.applyOptionRules(requestOptions);
                    this.syncOptionRules();
                    if (!this.choosePrecomputeOverwrite(requestOptions)) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/precompute/start', {
                            dataset_key: datasetKey,
                            options: requestOptions
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startLabeling() {
                    if (!this.selectedDataset) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (this.selectedDatasetKey !== datasetKey) return;
                    }
                    const existing = this.currentLabelVariantStatus();
                    const existingCount = existing ? existing.labels_count : 0;
                    const backendLabels = {grounding_dino: 'GroundingDINO', qwen_remote: 'Qwen Gradio', qwen_dashscope: 'OpenAI-compatible VLM'};
                    const labelKind = `${backendLabels[this.labeling.backend] || this.labeling.backend}${this.labeling.trial ? ' trial' : ''}`;
                    if (this.labeling.run_mode === 'full') {
                        if (!this.confirmOverwrite(
                            `${labelKind} labels`,
                            existingCount,
                            'Reviewed labels for the same result set and selected episodes will be cleared.'
                        )) return;
                    }
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/labeling/start', {
                            dataset_key: datasetKey,
                            options: this.labeling
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async mergeLabels() {
                    if (!this.canStartLegacyMutation()) return;
                    const datasetKey = this.selectedDataset.key;
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson(`/api/labeling/${datasetKey}/merge`, {
                            annotate: this.labeling.annotate
                        });
                        if (this.selectedDatasetKey !== datasetKey) return;
                        await this.loadDatasetDetails(datasetKey);
                    } catch (err) {
                        if (this.selectedDatasetKey === datasetKey) {
                            this.error = err.message;
                        }
                    } finally {
                        this.busy = false;
                    }
                },
                async loadConstructionPreview() {
                    const current = consoleReadScope(this, 'loadConstructionPreview', 'dataset');
                    if (!this.selectedDataset) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (!current() || this.selectedDatasetKey !== datasetKey) return;
                    }
                    this.error = '';
                    this.constructionLoading = true;
                    try {
                        const data = await this.requestJson('/api/construction/preview', {
                            dataset_key: datasetKey,
                            uncertainty_threshold: this.construction.uncertainty_threshold,
                            options: {
                                allow_pick_to_give: this.construction.allow_pick_to_give
                            }
                        });
                        if (!current() || this.selectedDatasetKey !== datasetKey) return;
                        this.constructionPreview = data;
                        for (const row of this.constructionScenarioRows()) {
                            if (!(row.key in this.construction.per_scenario_counts)) {
                                this.construction.per_scenario_counts[row.key] = 0;
                            }
                        }
                    } catch (err) {
                        if (!current()) return;
                        if (!current() || this.selectedDatasetKey !== datasetKey) return;
                        this.constructionPreview = null;
                        if (err.message === 'object_labeling_required') {
                            this.error = '';
                        } else {
                            this.error = err.message;
                        }
                    } finally {
                        if (current() && this.selectedDatasetKey === datasetKey) {
                            this.constructionLoading = false;
                        }
                    }
                },
                async startConstruction() {
                    if (!this.selectedDataset) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.confirmOverwrite('data construction records', (this.selectedDataset.construction || {}).records_count || 0)) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/construction/start', {
                            dataset_key: datasetKey,
                            options: this.construction
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startTagging() {
                    if (!this.selectedDataset) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (this.selectedDatasetKey !== datasetKey) return;
                    }
                    const existingCount = this.taggingExistingCount();
                    const kind = this.tagging.trial ? 'trial tagging results' : 'tagging results';
                    if (this.tagging.overwrite && existingCount > 0) {
                        const selected = (this.tagging.tag_names || []).length ? this.tagging.tag_names.join(', ') : 'all selected tags';
                        const ok = window.confirm(
                            `Existing ${kind}: ${existingCount} episodes.\n\n` +
                            `Overwrite existing values for: ${selected}.\n` +
                            `Other existing tag fields will be kept.\n\nContinue?`
                        );
                        if (!ok) return;
                    }
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/tagging/start', {
                            dataset_key: datasetKey,
                            options: this.tagging
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async mergeTags() {
                    if (!this.canStartLegacyMutation()) return;
                    const datasetKey = this.selectedDataset.key;
                    this.error = '';
                    this.busy = true;
                    try {
                        const url = (this.selectedDataset.tagging && this.selectedDataset.tagging?.merge_url)
                            ? this.selectedDataset.tagging?.merge_url
                            : `/api/tagging/${datasetKey}/merge`;
                        await this.requestJson(url, null, 'POST');
                        if (this.selectedDatasetKey !== datasetKey) return;
                        await this.loadDatasetDetails(datasetKey);
                    } catch (err) {
                        if (this.selectedDatasetKey === datasetKey) this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startEmbedding() {
                    if (!this.selectedDataset) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.selectedDataset.loaded) {
                        await this.loadDatasetDetails(datasetKey);
                        if (this.selectedDatasetKey !== datasetKey) return;
                    }
                    if (!this.confirmOverwrite('embedding points', (this.selectedDataset.embedding || {}).points_count || 0)) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/embedding/start', {
                            dataset_key: datasetKey,
                            options: this.embedding
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startCompare() {
                    if (!this.selectedDataset || !this.compare.dataset_key_b) return;
                    const datasetKey = this.selectedDataset.key;
                    if (!this.confirmOverwrite(
                        'compare cache',
                        (this.selectedDataset.compare || {}).cache_count || 0,
                        `Pair: ${datasetKey} vs ${this.compare.dataset_key_b}`
                    )) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/compare/start', {
                            dataset_key_a: datasetKey,
                            dataset_key_b: this.compare.dataset_key_b
                        });
                        this.upsertJob(data.job);
                        if (this.selectedDatasetKey === datasetKey) this.activeJob = data.job;
                        else this.syncActiveJob();
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                operationAvailable(operation, dataset = this.selectedDataset) {
                    return dataset?.operation_capabilities?.[operation]?.available !== false;
                },
                operationReason(operation) {
                    return this.selectedDataset?.operation_capabilities?.[operation]?.reason || '';
                },
                defaultProcessingDataVersion(dataset = this.selectedDataset) {
                    const profile = dataset?.default_processing_profile;
                    return profile ? profile.legacy_data_version : (dataset?.data_version ? 'DVT2' : null);
                },
                stagePolicyLabel() {
                    if (!this.selectedDataset) return 'Default stage policy: H10W DVT2 · stage v1';
                    if (String(this.selectedDataset.robot_type || '').toLowerCase() === 'umi'
                        && this.operationAvailable('auto_stage')) {
                        return `Stage policy: Equal time segments · ${this.options.fallback_stage_count || 5} segments (UMI)`;
                    }
                    if (!this.operationAvailable('auto_stage')) return this.operationReason('auto_stage');
                    const version = this.options.data_version || this.defaultProcessingDataVersion();
                    return `Stage policy: H10W ${version} · stage v1`;
                },
                applyDatasetDataVersion(dataset, force = false) {
                    if (!dataset?.data_version) {
                        this.options.data_version = null;
                        this.dataVersionManual = false;
                    } else if (force || !this.dataVersionManual) {
                        this.options.data_version = this.defaultProcessingDataVersion(dataset);
                    }
                },
                mergeCandidates() {
                    if (!this.selectedDataset?.remote) return this.datasets;
                    return this.remoteLocations
                        .filter(item => item.node_id === (this.selectedRemoteLocation?.node_id || this.selectedDataset?.remote_node_id) && item.state === 'available')
                        .map(item => this.remoteDatasetRecord(item));
                },
                filteredMergeDatasets() {
                    const query = String(this.preprocess.merge_dataset_search || '').trim().toLowerCase();
                    const candidates = this.mergeCandidates();
                    if (!query) return this.sortItems(candidates);
                    const tokens = query.split(/\s+/).filter(Boolean);
                    return this.sortItems(candidates.filter(item => {
                        const haystack = this.datasetSearchHaystack(item);
                        return tokens.every(token => haystack.includes(token));
                    }));
                },
                filteredSubtractDatasets() {
                    const query = String(this.preprocess.subtract_dataset_search || '').trim().toLowerCase();
                    const candidates = this.datasets.filter(item => item.key !== this.selectedDatasetKey);
                    if (!query) return this.sortItems(candidates);
                    const tokens = query.split(/\s+/).filter(Boolean);
                    return this.sortItems(candidates.filter(item => {
                        const haystack = this.datasetSearchHaystack(item);
                        return tokens.every(token => haystack.includes(token));
                    }));
                },
                mergeDatasetLabel(key) {
                    const item = this.mergeCandidates().find(dataset => dataset.key === key);
                    return item ? this.datasetName(item) : key;
                },
                subtractDatasetLabel(key) {
                    const item = this.datasets.find(dataset => dataset.key === key);
                    return item ? this.datasetName(item) : key;
                },
                removeMergeSource(key) {
                    this.preprocess.merge_src_keys = (this.preprocess.merge_src_keys || []).filter(item => item !== key);
                    if (this.preprocess.merge_delete_episodes && Object.prototype.hasOwnProperty.call(this.preprocess.merge_delete_episodes, key)) {
                        delete this.preprocess.merge_delete_episodes[key];
                    }
                },
                removeSubtractSource(key) {
                    this.preprocess.subtract_src_keys = (this.preprocess.subtract_src_keys || []).filter(item => item !== key);
                },
                mergeDeleteEpisodesPayload() {
                    const payload = {};
                    for (const key of this.preprocess.merge_src_keys || []) {
                        const value = String((this.preprocess.merge_delete_episodes || {})[key] || '').trim();
                        if (value) payload[key] = value;
                    }
                    return payload;
                },
                canAutoCatalog() {
                    if (!this.controlPlaneEnabled) return true;
                    return Boolean(
                        this.controlPlaneUser
                        && ['admin', 'data_manager', 'operator'].includes(this.controlPlaneUser.role)
                    );
                },
                hasMutationTask() {
                    return this.mutationFields.some(field => this.options[field.key]);
                },
                canStart() {
                    if (this.selectedDataset?.remote) {
                        return Boolean(this.selectedRemoteLocation && this.canOperateRemote());
                    }
                    if (this.selectedDataset && this.selectedDataset.source_protected) {
                        const mutationKeys = ['fix_episode_indices', 'write_parquet', 'write_subtask', 'overwrite_subtask_text', 'overwrite_parquet'];
                        if (mutationKeys.some(key => this.options[key])) return false;
                    }
                    if (this.selectedDatasetIsV3()) {
                        return Boolean(
                            this.selectedDataset
                            && this.selectedDataset.loaded
                            && (this.options.prepare_videos || this.options.prepare_csv)
                        );
                    }
                    return Boolean(this.selectedDataset && this.selectedDataset.loaded) && (
                        this.options.prepare_videos ||
                        this.options.prepare_csv ||
                        this.hasMutationTask()
                    );
                },
                canOpenViewer() {
                    return Boolean(
                        this.selectedDataset
                        && this.selectedDataset.loaded
                        && this.selectedDataset.cache
                        && this.selectedDataset.cache.status === 'cached'
                    );
                },
                canStartLabeling() {
                    return Boolean(
                        this.selectedDataset &&
                        this.selectedDataset.loaded &&
                        this.selectedLabelingBackendAvailable() &&
                        (this.isQwenBackend(this.labeling.backend)
                            ? this.labeling.endpoint && this.labeling.qwen_model && this.qwenTokenReady()
                            : this.labeling.model_id)
                    );
                },
                labelingBackendCapabilities(key) {
                    if (!this.labelingCapabilities) return null;
                    if (this.labelingCapabilities.backends && this.labelingCapabilities.backends[key]) {
                        return this.labelingCapabilities.backends[key];
                    }
                    if (key === 'grounding_dino') return this.labelingCapabilities;
                    return null;
                },
                labelingBackendAvailable(key) {
                    const caps = this.labelingBackendCapabilities(key);
                    return Boolean(caps && caps.available);
                },
                isQwenBackend(key) {
                    return key === 'qwen_remote' || key === 'qwen_dashscope';
                },
                applyLabelingBackendDefaults(force = false) {
                    const caps = this.labelingBackendCapabilities(this.labeling.backend);
                    if (!caps) return;
                    if ((force || !this.labeling.endpoint) && caps.default_endpoint) {
                        this.labeling.endpoint = caps.default_endpoint;
                    }
                    if ((force || !this.labeling.qwen_model) && caps.default_model) {
                        this.labeling.qwen_model = caps.default_model;
                    }
                },
                selectedLabelingBackendAvailable() {
                    return this.labelingBackendAvailable(this.labeling.backend);
                },
                qwenTokenReady(key = this.labeling.backend) {
                    const caps = this.labelingBackendCapabilities(key);
                    return Boolean(this.labeling.qwen_token || (caps && caps.token_configured));
                },
                labelingBackendError(key) {
                    const caps = this.labelingBackendCapabilities(key);
                    if (!caps) return 'Backend capability is unknown.';
                    if (!caps.available) return caps.error || 'Backend dependency is unavailable.';
                    if (this.isQwenBackend(key) && caps.requires_token && !this.qwenTokenReady(key)) {
                        return `${key === 'qwen_dashscope' ? 'OpenAI-compatible VLM' : 'Remote Qwen'} requires a token. Set ${caps.token_env_vars ? caps.token_env_vars[0] : 'DASHSCOPE_API_KEY'} before launching data_platform, paste it in the token field, or use EMPTY for a local unauthenticated server.`;
                    }
                    return '';
                },
                labelingQwenModels() {
                    const caps = this.labelingBackendCapabilities(this.labeling.backend);
                    return caps && caps.models && caps.models.length ? caps.models : ['Qwen/Qwen3.6-35B-A3B', 'qwen3.6-plus', 'qwen3.7-plus', 'qwen3.6-flash', 'qwen3-vl-plus', 'qwen-vl-plus'];
                },
                canOpenReview() {
                    return Boolean(
                        this.selectedDataset &&
                        this.selectedDataset.labeling &&
                        this.selectedDataset.labeling?.labels_count > 0
                    ) || Boolean(
                        this.activeJob &&
                        this.activeJob.job_type === 'labeling' &&
                        this.activeJob.status !== 'error' &&
                        this.activeJob.review_url &&
                        this.activeJob.dataset_key === (this.selectedDataset && this.selectedDataset.key)
                    );
                },
                canMergeLabels() {
                    return Boolean(
                        this.selectedDataset &&
                        this.selectedDataset.labeling &&
                        this.selectedDataset.labeling?.reviewed_count > 0 &&
                        this.labeling.annotate
                    );
                },
                constructionReady() {
                    return Boolean(
                        this.selectedDataset &&
                        this.selectedDataset.loaded &&
                        this.selectedDataset.construction &&
                        this.selectedDataset.construction.object_labeling_ready
                    );
                },
                constructionVocab() {
                    return this.constructionPreview && this.constructionPreview.vocab ? this.constructionPreview.vocab : [];
                },
                constructionScenarioRows() {
                    const scenarios = this.constructionPreview && this.constructionPreview.scenarios
                        ? this.constructionPreview.scenarios
                        : {};
                    const labels = {
                        single_pick: 'single pick',
                        directional_pick: 'directional pick',
                        relative_pick: 'relative pick',
                        give: 'give'
                    };
                    return Object.keys(labels).map(key => {
                        const data = scenarios[key] || {candidate_count: 0, missing_distribution: {}};
                        const dist = data.missing_distribution || {};
                        const sourceDist = data.source_visual_distribution || {};
                        const backgroundDist = data.background_distribution || {};
                        const distributionText = Object.keys(dist).length
                            ? Object.entries(dist).map(([name, count]) => `${name}:${count}`).join(' · ')
                            : 'no missing-object candidates';
                        const sourceText = Object.keys(sourceDist).length
                            ? Object.entries(sourceDist).map(([name, count]) => `${name}:${count}`).join(' · ')
                            : 'no source-object candidates';
                        const backgroundText = Object.keys(backgroundDist).length
                            ? Object.entries(backgroundDist).map(([name, count]) => `${name}:${count}`).join(' · ')
                            : 'no background tags';
                        return {
                            key,
                            label: labels[key],
                            candidate_count: data.candidate_count || 0,
                            distributionText: `missing ${distributionText} | source ${sourceText} | background ${backgroundText}`
                        };
                    });
                },
                syntheticPlaceholder() {
                    if (!this.selectedDataset || !this.selectedDataset.root) return '<src>_synthetic_<timestamp>';
                    return `${this.selectedDataset.root}_synthetic_<timestamp>`;
                },
                standardizePlaceholder() {
                    if (!this.selectedDataset || !this.selectedDataset.root) return '<src>_preprocessed';
                    return `${this.selectedDataset.root}_preprocessed`;
                },
                syntheticDefaultPath(root) {
                    if (!root) return '';
                    const now = new Date();
                    const pad = value => String(value).padStart(2, '0');
                    const ts = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
                    return `${root}_synthetic_${ts}`;
                },
                canStartConstruction() {
                    if (!this.constructionReady()) return false;
                    return Object.values(this.construction.per_scenario_counts || {}).some(value => Number(value || 0) > 0);
                },
                constructionReviewUrl() {
                    if (
                        this.selectedDataset &&
                        this.activeJob &&
                        this.activeJob.dataset_key === this.selectedDataset.key &&
                        this.activeJob.job_type === 'construction' &&
                        this.activeJob.review_url
                    ) {
                        return this.activeJob.review_url;
                    }
                    if (this.selectedDataset && this.selectedDataset.construction && this.selectedDataset.construction.records_count > 0) {
                        return this.selectedDataset.construction.review_url;
                    }
                    return '';
                },
                canStartTagging() {
                    return Boolean(
                        this.selectedDataset &&
                        this.selectedDataset.loaded &&
                        this.tagging.tag_names &&
                        this.tagging.tag_names.length > 0 &&
                        !this.hasUnavailableSelectedVlmTags() &&
                        !this.taggingTokenMissing()
                    );
                },
                taggingExistingCount() {
                    const tagging = this.selectedDataset && this.selectedDataset.tagging ? this.selectedDataset.tagging : {};
                    if (!this.tagging.trial) return tagging.tags_count || 0;
                    const variants = tagging.variants || [];
                    const trial = variants.find(item => item.id === 'trial');
                    return trial ? (trial.tags_count || 0) : 0;
                },
                selectedVlmTagNames() {
                    const selected = new Set(this.tagging.tag_names || []);
                    return (this.taggingSchema || [])
                        .filter(tag => tag.backend === 'vlm' && selected.has(tag.name))
                        .map(tag => tag.name);
                },
                taggingBackendCapabilities(key) {
                    if (!this.taggingCapabilities) return null;
                    if (this.taggingCapabilities.backends && this.taggingCapabilities.backends[key]) {
                        return this.taggingCapabilities.backends[key];
                    }
                    return key === this.tagging.vlm_backend ? this.taggingCapabilities : null;
                },
                taggingBackendAvailable(key) {
                    const caps = this.taggingBackendCapabilities(key);
                    return Boolean(caps && caps.available);
                },
                applyTaggingBackendDefaults(force = false) {
                    const caps = this.taggingBackendCapabilities(this.tagging.vlm_backend);
                    if (!caps) return;
                    if ((force || !this.tagging.vlm_endpoint) && caps.default_endpoint) {
                        this.tagging.vlm_endpoint = caps.default_endpoint;
                    }
                    if ((force || !this.tagging.vlm_model) && caps.default_model) {
                        this.tagging.vlm_model = caps.default_model;
                    }
                },
                taggingTokenReady(key = this.tagging.vlm_backend) {
                    const caps = this.taggingBackendCapabilities(key);
                    return Boolean(this.tagging.vlm_token || (caps && caps.token_configured));
                },
                taggingTokenMissing() {
                    const caps = this.taggingBackendCapabilities(this.tagging.vlm_backend);
                    return Boolean(this.selectedVlmTagNames().length > 0 && caps && caps.requires_token && !this.taggingTokenReady());
                },
                taggingBackendError(key) {
                    const caps = this.taggingBackendCapabilities(key);
                    if (!caps) return 'VLM backend capability is unknown.';
                    if (!caps.available) return caps.error || 'VLM backend is unavailable.';
                    if (caps.requires_token && !this.taggingTokenReady(key)) {
                        return `${key === 'qwen_dashscope' ? 'OpenAI-compatible VLM' : 'Remote Qwen'} requires a token. Set ${caps.token_env_vars ? caps.token_env_vars[0] : 'DASHSCOPE_API_KEY'} before launching data_platform, paste it in the token field, or use EMPTY for a local unauthenticated server.`;
                    }
                    return '';
                },
                taggingVlmModels() {
                    const caps = this.taggingBackendCapabilities(this.tagging.vlm_backend);
                    return caps && caps.models && caps.models.length ? caps.models : ['Qwen/Qwen3.6-35B-A3B', 'qwen3.6-plus', 'qwen3.7-plus', 'qwen3.6-flash', 'qwen3-vl-plus', 'qwen-vl-plus'];
                },
                hasUnavailableSelectedVlmTags() {
                    return Boolean(
                        this.selectedVlmTagNames().length > 0 &&
                        this.taggingCapabilities &&
                        !this.taggingBackendAvailable(this.tagging.vlm_backend)
                    );
                },
                taggingReviewUrl() {
                    if (!this.selectedDataset) return '';
                    if (this.activeJob && this.activeJob.job_type === 'tagging' && this.activeJob.review_url && this.activeJob.dataset_key === this.selectedDataset.key) {
                        return this.activeJob.review_url;
                    }
                    if (this.selectedDataset.tagging && this.selectedDataset.tagging?.tags_count > 0) {
                        return this.selectedDataset.tagging?.review_url;
                    }
                    return '';
                },
                canMergeTags() {
                    return Boolean(this.selectedDataset && this.selectedDataset.tagging && this.selectedDataset.tagging?.tags_count > 0);
                },
                canStartEmbedding() {
                    if (!this.operationAvailable('embedding')) return false;
                    return Boolean(this.selectedDataset && this.selectedDataset.loaded && this.embedding.ckpt_path);
                },
                embeddingUrl() {
                    if (!this.selectedDataset) return '';
                    if (this.activeJob && this.activeJob.job_type === 'embedding' && this.activeJob.review_url && this.activeJob.dataset_key === this.selectedDataset.key) {
                        return this.activeJob.review_url;
                    }
                    if (this.selectedDataset.embedding && this.selectedDataset.embedding?.points_count > 0) {
                        return this.selectedDataset.embedding?.scatter_url;
                    }
                    return '';
                },
                smoothingReportUrl() {
                    if (
                        this.selectedDataset &&
                        this.activeJob &&
                        this.activeJob.job_type === 'preprocess_smooth_action' &&
                        this.activeJob.dataset_key === this.selectedDataset.key &&
                        this.activeJob.review_url
                    ) {
                        return this.activeJob.review_url;
                    }
                    if (this.selectedDataset && this.selectedDataset.smoothing && this.selectedDataset.smoothing.status === 'ready') {
                        return this.selectedDataset.smoothing.report_url;
                    }
                    return '';
                },
                prefillMergeOutput() {
                    if (!this.selectedDataset?.remote || String(this.preprocess.merge_out_root || '').trim()) return;
                    const root = (this.selectedRemoteNode()?.writable_roots || [])[0];
                    if (!root) return;
                    this.preprocess.merge_out_root = `${String(root).replace(/\/+$/, '')}/merged_${Date.now()}`;
                },
                mergeOutputError() {
                    const value = String(this.preprocess.merge_out_root || '').trim();
                    if (!this.selectedDataset?.remote) {
                        return value.includes('/') || value.includes('\\') ? 'Enter an output folder name, not a path.' : '';
                    }
                    const roots = this.selectedRemoteNode()?.writable_roots || [];
                    if (!roots.length) return 'This Agent has no writable output roots configured.';
                    if (!value) return '';
                    if (!value.startsWith('/')) return 'Output must be an absolute path on the Agent.';
                    if (value.split('/').includes('..')) return 'Output path must not contain parent-directory (..) segments.';
                    const normalize = path => '/' + String(path).split('/').filter(part => part && part !== '.').join('/');
                    const path = normalize(value);
                    const contains = (parent, child) => parent === '/' || parent === child || child.startsWith(parent + '/');
                    if (!roots.some(root => contains(normalize(root), path))) return `Output must be inside writable roots: ${roots.join(', ')}. No task has been submitted.`;
                    if (roots.some(root => normalize(root) === path)) return 'Add a new dataset folder below the writable root.';
                    const selected = new Set(this.preprocess.merge_src_keys || []);
                    const overlap = this.mergeCandidates().filter(item => selected.has(item.key)).some(item => {
                        const source = item.root || item.path;
                        return source && (contains(normalize(source), path) || contains(path, normalize(source)));
                    });
                    return overlap ? 'Output must be separate from every source dataset.' : '';
                },
                mergeOutputPreview() {
                    const name = String(this.preprocess.merge_out_root || '').trim();
                    if (this.selectedDataset?.remote) {
                        if (!name) return 'auto: new sibling dataset on the selected Agent';
                        return name.startsWith('/') ? `Agent output: ${name}` : 'output: enter an absolute path on the Agent';
                    }
                    const base = String(this.rootDir || '').replace(/\/+$/, '');
                    if (!name) return 'auto: first selected dataset + _merge_<timestamp>';
                    if (name.startsWith('/') || name.includes('/') || name.includes('\\')) return 'output: enter a folder name only, not a path';
                    return base ? `output: ${base}/${name}` : `output: ${name}`;
                },
                subtractOutputPreview() {
                    const name = String(this.preprocess.subtract_out_root || '').trim();
                    const base = String(this.rootDir || '').replace(/\/+$/, '');
                    if (!name) return 'auto: current dataset + _subtract_<timestamp>';
                    if (name.startsWith('/') || name.includes('/') || name.includes('\\')) return 'output: enter a folder name only, not a path';
                    return base ? `output: ${base}/${name}` : `output: ${name}`;
                },
                compareDatasetB() {
                    if (!this.compare.dataset_key_b) return null;
                    return this.compareCandidates.find(item => item.key === this.compare.dataset_key_b) || null;
                },
                canStartCompare() {
                    return Boolean(
                        this.selectedDataset &&
                        this.compare.dataset_key_b &&
                        this.compare.dataset_key_b !== this.selectedDataset.key
                    );
                },
                compareUrl() {
                    if (!this.canStartCompare()) return '';
                    if (this.activeJob && this.activeJob.job_type === 'compare' && this.activeJob.review_url && this.activeJob.dataset_key === this.selectedDataset.key) {
                        return this.activeJob.review_url;
                    }
                    return `/${this.selectedDataset.key}/compare/${this.compare.dataset_key_b}`;
                },
    };
}
