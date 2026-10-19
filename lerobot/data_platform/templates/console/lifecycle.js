// Lifecycle state and behavior for the legacy console.
function consoleLifecycleState() {
    return {
                sourceBatches: [],
                reconciliationReports: [],
                datasetProfiles: [],
                datasetRequirements: [],
                dataRecipes: [],
                curationWorkspaces: [],
                curationManifests: [],
                lifecycleVersions: [],
                lifecycleProfiles: [],
                lifecycleQuarantine: [],
                lifecycleLoading: false,
                sourceBatchForm: {
                    source_batch_id: '',
                    name: '',
                    source_kind: 'robot',
                    source_uri: '',
                    expected_episode_count: ''
                },
                requirementForm: {
                    name: '',
                    target_episode_count: '',
                    dimensions_json: '{}',
                    quality_constraints_json: '{}'
                },
                recipeForm: {
                    name: '',
                    requirement_id: '',
                    cohort_query_json: '{}',
                    composition_json: '{}',
                    exact_dedup: false,
                    random_seed: 0
                },
                curationForm: {
                    base_dataset_version_id: '',
                    include_episode_ids: '',
                    exclude_episode_ids: '',
                    reason: '',
                    collect_current_artifacts: true,
                    repair_recipes_json: '[]',
                    materialize_out_root: '',
                    materialization_profile_id: ''
                },
                profileForm: {
                    name: '',
                    steps_json: '[]',
                    profile_id: '',
                    out_root: ''
                },
                async loadLifecycle() {
                    const current = consoleReadScope(this, 'loadLifecycle', 'dataset');
                    if (this.datasetAccessRestricted || !this.selectedDatasetKey || !this.tabEnabled('versions')) {
                        this.lifecycleVersions = [];
                        this.lifecycleProfiles = [];
                        this.lifecycleQuarantine = [];
                        this.sourceBatches = [];
                        this.reconciliationReports = [];
                        this.datasetProfiles = [];
                        this.datasetRequirements = [];
                        this.dataRecipes = [];
                        this.curationWorkspaces = [];
                        this.curationManifests = [];
                        return;
                    }
                    const datasetKey = this.selectedDatasetKey;
                    this.lifecycleLoading = true;
                    try {
                        const [versionsData, profilesData, quarantineData, sourceBatchesData, reconciliationsData, datasetProfilesData, requirementsData, recipesData, workspacesData, manifestsData] = await Promise.all([
                            this.requestJson(`/api/lifecycle/versions?dataset_key=${encodeURIComponent(datasetKey)}`),
                            this.requestJson('/api/lifecycle/profiles'),
                            this.requestJson(`/api/lifecycle/quarantine?dataset_key=${encodeURIComponent(datasetKey)}`),
                            this.requestJson('/api/lifecycle/source-batches'),
                            this.requestJson('/api/lifecycle/reconciliations'),
                            this.requestJson('/api/curation/dataset-profiles'),
                            this.requestJson('/api/curation/requirements'),
                            this.requestJson('/api/curation/recipes'),
                            this.requestJson('/api/curation/workspaces'),
                            this.requestJson(`/api/curation/manifests?dataset_key=${encodeURIComponent(datasetKey)}`)
                        ]);
                        if (!current() || this.selectedDatasetKey !== datasetKey) return;
                        this.lifecycleVersions = versionsData.versions || [];
                        this.lifecycleProfiles = profilesData.profiles || [];
                        this.lifecycleQuarantine = quarantineData.quarantine || [];
                        this.sourceBatches = sourceBatchesData.source_batches || [];
                        const versionIds = new Set(this.lifecycleVersions.map(item => item.version_id));
                        this.reconciliationReports = (reconciliationsData.reconciliations || []).filter(
                            item => versionIds.has(item.output_dataset_version_id)
                                || item.input_dataset_version_ids.some(versionId => versionIds.has(versionId))
                        );
                        this.datasetProfiles = (datasetProfilesData.dataset_profiles || []).filter(
                            item => versionIds.has(item.dataset_version_id)
                        );
                        this.datasetRequirements = requirementsData.requirements || [];
                        this.dataRecipes = (recipesData.recipes || []).filter(
                            item => versionIds.has(item.base_dataset_version_id)
                        );
                        this.curationWorkspaces = (workspacesData.workspaces || []).filter(
                            item => versionIds.has(item.base_dataset_version_id)
                        );
                        this.curationManifests = manifestsData.manifests || [];
                        if (!this.curationForm.base_dataset_version_id && this.lifecycleVersions.length) {
                            this.curationForm.base_dataset_version_id = this.lifecycleVersions[0].version_id;
                        }
                        if (!this.sourceBatchForm.source_uri && this.selectedDataset) {
                            this.sourceBatchForm.source_uri = this.selectedDataset.root || '';
                        }
                    } finally {
                        if (current() && this.selectedDatasetKey === datasetKey) this.lifecycleLoading = false;
                    }
                },
                async startLifecycleIngest() {
                    const datasetKey = await this.ensureSelectedDatasetLoaded();
                    if (!datasetKey) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/lifecycle/ingest/start', {
                            dataset_key: datasetKey,
                            options: {
                                stage: 'raw',
                                source_batch_id: this.sourceBatchForm.source_batch_id || null
                            }
                        });
                        this.upsertJob(data.job);
                        this.activeJob = data.job;
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async createSourceBatch() {
                    if (!this.selectedDataset) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/lifecycle/source-batches', {
                            name: this.sourceBatchForm.name,
                            source_kind: this.sourceBatchForm.source_kind,
                            source_uri: this.sourceBatchForm.source_uri || this.selectedDataset.root,
                            robot_profile: this.selectedDataset.robot_profile || 'unknown',
                            signal_schema: this.selectedDataset.signal_schema || 'unknown',
                            dataset_format: this.selectedDataset.format_version || 'unknown',
                            retention_class: this.sourceBatchForm.source_kind === 'generated'
                                ? 'managed'
                                : 'protected_source',
                            expected_episode_count: this.sourceBatchForm.expected_episode_count || null,
                            metadata: {
                                stage_profile: this.selectedDataset.stage_profile || '',
                                gripper_encoding: this.selectedDataset.gripper_encoding || ''
                            },
                            created_by: 'local-user'
                        });
                        this.sourceBatchForm.source_batch_id = data.source_batch.source_batch_id;
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async createDatasetProfile(version) {
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson('/api/curation/dataset-profiles', {
                            dataset_version_id: version.version_id,
                            created_by: 'local-user'
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async createDatasetRequirement() {
                    this.error = '';
                    this.busy = true;
                    try {
                        const dimensions = this.parseJsonValue(
                            this.requirementForm.dimensions_json,
                            'coverage dimensions',
                            {}
                        );
                        const qualityConstraints = this.parseJsonValue(
                            this.requirementForm.quality_constraints_json,
                            'quality constraints',
                            {}
                        );
                        const data = await this.requestJson('/api/curation/requirements', {
                            name: this.requirementForm.name,
                            target_episode_count: this.requirementForm.target_episode_count || null,
                            dimensions,
                            quality_constraints: qualityConstraints,
                            reason: this.curationForm.reason || null,
                            created_by: 'local-user'
                        });
                        this.recipeForm.requirement_id = data.requirement.requirement_id;
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async createDataRecipe() {
                    this.error = '';
                    this.busy = true;
                    try {
                        const cohortQuery = this.parseJsonValue(
                            this.recipeForm.cohort_query_json,
                            'cohort query',
                            {}
                        );
                        const composition = this.parseJsonValue(
                            this.recipeForm.composition_json,
                            'recipe composition',
                            {}
                        );
                        await this.requestJson('/api/curation/recipes', {
                            name: this.recipeForm.name,
                            requirement_id: this.recipeForm.requirement_id || null,
                            base_dataset_version_id: this.curationForm.base_dataset_version_id,
                            cohort_query: cohortQuery,
                            include_episode_ids: this.curationForm.include_episode_ids,
                            exclude_episode_ids: this.curationForm.exclude_episode_ids,
                            composition,
                            deduplication: {mode: this.recipeForm.exact_dedup ? 'exact' : 'none'},
                            random_seed: Number(this.recipeForm.random_seed || 0),
                            created_by: 'local-user'
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async compileDataRecipe(recipe) {
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson(`/api/curation/recipes/${recipe.recipe_id}/compile`, {
                            owner: 'local-user'
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                preprocessingProfiles() {
                    return this.lifecycleProfiles.filter(profile => profile.kind === 'preprocessing');
                },
                materializationProfiles() {
                    return this.lifecycleProfiles.filter(profile => profile.kind === 'materialization');
                },
                async createPreprocessingProfile() {
                    this.error = '';
                    this.busy = true;
                    try {
                        const steps = this.parseJsonValue(this.profileForm.steps_json, 'profile steps', []);
                        if (!Array.isArray(steps) || !steps.length) {
                            throw new Error('profile steps must be a non-empty JSON array');
                        }
                        const data = await this.requestJson('/api/lifecycle/profiles', {
                            kind: 'preprocessing',
                            name: this.profileForm.name,
                            steps,
                            created_by: 'local-user'
                        });
                        this.profileForm.profile_id = data.profile.profile_id;
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startLifecycleProfile() {
                    if (!this.selectedDatasetKey) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/lifecycle/preprocess/start', {
                            dataset_key: this.selectedDatasetKey,
                            base_dataset_version_id: this.curationForm.base_dataset_version_id || null,
                            profile_id: this.profileForm.profile_id,
                            out_root: this.profileForm.out_root,
                            execution_options: {workers: 8}
                        });
                        this.upsertJob(data.job);
                        this.activeJob = data.job;
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async createCurationWorkspace() {
                    if (!this.selectedDatasetKey) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const repairs = this.parseJsonValue(
                            this.curationForm.repair_recipes_json,
                            'repair recipes',
                            []
                        );
                        if (!Array.isArray(repairs)) throw new Error('repair recipes must be a JSON array');
                        await this.requestJson('/api/curation/workspaces', {
                            dataset_key: this.selectedDatasetKey,
                            base_dataset_version_id: this.curationForm.base_dataset_version_id || null,
                            include_episode_ids: this.curationForm.include_episode_ids,
                            exclude_episode_ids: this.curationForm.exclude_episode_ids,
                            reason: this.curationForm.reason,
                            collect_current_artifacts: this.curationForm.collect_current_artifacts,
                            repair_recipes: repairs,
                            owner: 'local-user'
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async publishCurationWorkspace(workspace) {
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson(`/api/curation/workspaces/${workspace.workspace_id}/publish`, {
                            expected_revision: workspace.revision,
                            reviewer: 'local-user',
                            reason: this.curationForm.reason
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                manifestAdvanceLabel(manifest) {
                    const labels = {
                        draft: 'Submit for review',
                        in_review: 'Approve',
                        approved: 'Publish'
                    };
                    return labels[manifest.status] || manifest.status;
                },
                async advanceManifest(manifest) {
                    const next = {draft: 'in_review', in_review: 'approved', approved: 'published'}[manifest.status];
                    if (!next) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson(`/api/curation/manifests/${manifest.manifest_id}/transition`, {
                            status: next,
                            reviewer: next === 'in_review' ? null : 'local-user'
                        });
                        await this.loadLifecycle();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async startManifestMaterialization(manifest) {
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/lifecycle/materialize/start', {
                            manifest_id: manifest.manifest_id,
                            out_root: this.curationForm.materialize_out_root || null,
                            workers: 8,
                            profile_id: this.curationForm.materialization_profile_id || null
                        });
                        this.upsertJob(data.job);
                        this.activeJob = data.job;
                        this.pollJob(data.job.id);
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
    };
}
