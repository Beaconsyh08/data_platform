// Shell state and behavior for the legacy console.
function consoleShellState() {
    return {
                activeWorkspace: 'data_platform',
                activePage: 'datasets',
                activeTab: '',
                rememberedPages: {},
                rememberedTabs: {},
                consoleMode: {{ console_mode|tojson }},
                legacyMutationsEnabled: {{ legacy_mutations_enabled|tojson }},
                remoteSourceMutationsEnabled: {{ remote_source_mutations_enabled|default(false)|tojson }},
                controlPlaneEnabled: {{ control_plane_enabled|tojson }},
                datasetAccessRestricted: {{ dataset_access_restricted|default(false)|tojson }},
                allowedTabs: {{ allowed_tabs|tojson }},
                allowedOpenLinks: {{ allowed_open_links|tojson }},
                groups: {{ console_groups|tojson }},
                initialPage: {{ initial_page|tojson }},
                initialTab: {{ initial_tab|tojson }},
                busy: false,
                error: '',
                submissionNotice: '',
                requestTimeoutMs: 30000,
                datasetDetailTimeoutMs: 120000,
                async init() {
                    if (this._initialized) return;
                    this._initialized = true;
                    const startupNavigation = this.navigationRevision || 0;
                    const startupBrowse = this.datasetBrowseRevision || 0;
                    const storedJobId = this.storedJobId();
                    await this.loadAdminStatus();
                    if (this.consoleMode === 'visualize') {
                        this.preprocess.schema_op = 'delete_episodes';
                    }
                    const startupLoads = [
                        this.safeStartupLoad(() => this.loadDatasetRoots()),
                        this.safeStartupLoad(() => this.loadJobs(false)),
                        this.safeStartupLoad(() => this.loadOperationLogs())
                    ];
                    if (this.controlPlaneEnabled) {
                        startupLoads.push(this.safeStartupLoad(() => this.loadRemoteSources(true)));
                    }
                    if (['quality_flags', 'data_modification', 'standardize', 'transform', 'dataset_ops', 'split_merge'].some(tab => this.tabEnabled(tab))) {
                        startupLoads.push(this.safeStartupLoad(() => this.loadPreprocessCapabilities()));
                    }
                    if (this.tabEnabled('labeling')) {
                        startupLoads.push(this.safeStartupLoad(() => this.loadLabelingCapabilities()));
                    }
                    if (this.tabEnabled('tagging')) {
                        startupLoads.push(this.safeStartupLoad(() => this.loadTaggingSchema()));
                    }
                    await Promise.all(startupLoads);
                    if ((this.navigationRevision || 0) !== startupNavigation) {
                        this.installJobRefreshHandlers();
                        return;
                    }
                    const browseChanged = (this.datasetBrowseRevision || 0) !== startupBrowse;
                    const preserveBrowser = browseChanged || this.restoreDatasetBrowser();
                    const startupJob = this.jobs.find(job => job.id === storedJobId)
                        || this.jobs.find(job => job.id === this.selectedJobId)
                        || null;
                    if (startupJob) await this.selectJob(startupJob, false);
                    const key = this.initialSelectedDataset || this.storedDatasetKey();
                    if (key) {
                        const dataset = this.datasets.find(item => item.key === key);
                        if (dataset) {
                            this.selectDataset(dataset, false, {deferDetails: true, preserveBrowser});
                        } else if (key.startsWith('remote:')) {
                            const location = this.remoteLocations.find(
                                item => `remote:${item.location_id}` === key
                            );
                            if (location) await this.useRemoteDataset(location, false, {preserveBrowser});
                        }
                    }
                    if ((this.navigationRevision || 0) !== startupNavigation) { this.installJobRefreshHandlers(); return; }
                    const startupTab = this.initialTab === 'schema'
                        ? (this.consoleMode === 'visualize' ? 'dataset_ops' : 'transform')
                        : this.initialTab;
                    if (startupTab && this.validTabs().includes(startupTab)) {
                        await this.selectTab(startupTab, false);
                    } else if (this.initialPage && this.validPages().includes(this.initialPage)) {
                        await this.selectPage(this.initialPage, false);
                    } else if (this.selectedDatasetKey) {
                        await this.selectPage('preprocessing', false);
                    } else {
                        await this.selectPage('datasets', false);
                    }
                    this.updateHomeUrl();
                    this.installJobRefreshHandlers();
                },
                installJobRefreshHandlers() {
                    if (this._jobRefreshHandlersInstalled) return;
                    this._jobRefreshHandlersInstalled = true;
                    const resume = () => this.resumeJobPolling();
                    window.addEventListener('pageshow', resume);
                    window.addEventListener('caption-jobs-submitted', resume);
                    window.addEventListener('focus', resume);
                    document.addEventListener('visibilitychange', () => {
                        if (!document.hidden) resume();
                    });
                },
                async safeStartupLoad(fn) {
                    try {
                        await fn();
                    } catch (err) {
                        if (!this.error) this.error = err.message;
                    }
                },
                availableGroups() {
                    const dataManager = this.controlPlaneUser?.role === 'data_manager';
                    return this.groups.filter(
                        group => group.key !== 'legacy_admin' || this.adminModeEnabled || dataManager
                    ).map(group => group.key === 'legacy_admin' && dataManager ? {
                        ...group, label: 'Data management',
                        pages: group.pages.map(page => ({...page, label: 'Source data'}))
                    } : group);
                },
                tabEnabled(tab) {
                    return this.allowedTabs.includes(tab);
                },
                openLinkEnabled(key) {
                    return this.allowedOpenLinks.includes(key);
                },
                validTabs() {
                    return this.availableGroups().flatMap(
                        group => group.pages.flatMap(page => page.tabs.map(tab => tab.key))
                    );
                },
                validPages() {
                    return this.availableGroups().flatMap(group => group.pages.map(page => page.key));
                },
                currentWorkspace() {
                    return this.availableGroups().find(group => group.key === this.activeWorkspace)
                        || this.availableGroups()[0]
                        || null;
                },
                activeWorkspaceLabel() {
                    const workspace = this.currentWorkspace();
                    return workspace ? workspace.label : 'Workspace';
                },
                temporalCaptionUrl() {
                    if (!this.selectedDataset) return '';
                    return this.selectedDataset.remote
                        ? `/remote/${encodeURIComponent(this.selectedDataset.remote_location_id)}/temporal-caption`
                        : `/${this.selectedDatasetKey}/temporal-caption`;
                },
                currentPages() {
                    const workspace = this.currentWorkspace();
                    if (!workspace) return [];
                    if (this.selectedDataset?.remote && workspace.key === 'data_curation') {
                        return workspace.pages;
                    }
                    return workspace.pages;
                },
                findPage(pageKey) {
                    for (const group of this.availableGroups()) {
                        const page = group.pages.find(item => item.key === pageKey);
                        if (page) return {group, page};
                    }
                    return null;
                },
                pageForTab(tabKey) {
                    for (const group of this.availableGroups()) {
                        const page = group.pages.find(item => item.tabs.some(tab => tab.key === tabKey));
                        if (page) return {group, page};
                    }
                    return null;
                },
                currentPage() {
                    const found = this.findPage(this.activePage);
                    return found ? found.page : null;
                },
                currentSubTabs() {
                    const page = this.currentPage();
                    if (!page) return [];
                    if (this.selectedDataset?.remote && page.key === 'explore') {
                        return page.tabs;
                    }
                    if (this.selectedDataset?.remote && page.key === 'annotation') {
                        return page.tabs;
                    }
                    return page.tabs;
                },
                pageOpenLinkEnabled(key) {
                    const page = this.currentPage();
                    return Boolean(page && page.open_links.includes(key) && this.openLinkEnabled(key));
                },
                isOperationPage() {
                    return !['datasets', 'runs', 'mcap_import'].includes(this.activePage);
                },
                activePageRequiresDataset() {
                    return this.activeTab !== 'pipelines' && this.isOperationPage();
                },
                activePageLabel() {
                    const page = this.currentPage();
                    return page ? page.label : 'Workspace';
                },
                activePageDescription() {
                    if (this.activePage === 'explore' && this.selectedDataset?.remote) {
                        return 'Explore dataset statistics or prepare the Viewer to inspect episodes.';
                    }
                    const descriptions = {
                        preprocessing: 'Prepare a new dataset while preserving the source.',
                        versions: 'Track dataset versions, sources, and storage locations.',
                        explore: 'Explore episodes, task coverage, and dataset differences.',
                        quality: 'Find issues to review. Detection does not change source data.',
                        annotation: 'Create and review stage, object, and caption annotations.',
                        dataset_build: 'Build and publish a reviewed dataset selection.',
                        admin_operations: 'Run controlled legacy operations that may modify source data. Review risk before execution.',
                    };
                    return descriptions[this.activePage] || '';
                },
                visualizationItems() {
                    if (!this.selectedDataset) return [];
                    const remoteViewerPending = Boolean(
                        this.selectedDataset.remote && this.remoteViewerPending(this.selectedRemoteLocation)
                    );
                    const compareCount = Number(
                        this.selectedDataset.compare && this.selectedDataset.compare.cache_count || 0
                    );
                    const items = [
                        {
                            key: 'inspect', group: 'inspect', icon: 'DI', label: 'Data Inspect',
                            output: 'Raw records · Viewer CSV · schema',
                            description: 'Preview records and fields without preparing videos.',
                            ready: Boolean(this.inspectUrl()), href: this.inspectUrl(), openLabel: 'Open data inspect',
                            setupLabel: 'Select dataset', missingStatus: 'Select dataset'
                        },
                        {
                            key: 'viewer', group: 'inspect', icon: 'EV', label: 'Episode Viewer',
                            output: 'Video · signals · stages',
                            description: 'Inspect video, signals, and episode annotations together.',
                            ready: this.canOpenViewer(), href: this.viewerUrl(), openLabel: 'Open viewer',
                            setupLabel: remoteViewerPending
                                ? 'Preparing viewer…'
                                : this.selectedDataset.remote ? 'Prepare viewer' : 'Prepare video + CSV',
                            setupDisabled: this.selectedDataset.remote
                                && (!this.canOperateRemote() || remoteViewerPending),
                            missingStatus: remoteViewerPending
                                ? `Preparing ${this.remoteViewerProgress(this.selectedRemoteLocation)}%`
                                : this.selectedDataset.remote && !this.canOperateRemote()
                                    ? 'Operator needed'
                                    : 'Needs cache'
                        },
                        {
                            key: 'analysis', group: 'inspect', icon: 'AN', label: 'Dataset Analysis',
                            output: 'Distributions · coverage',
                            description: 'Explore task coverage and episode statistics.',
                            ready: Boolean(this.analysisUrl()), href: this.analysisUrl(), openLabel: 'Open analysis',
                            setupLabel: 'Load dataset', missingStatus: 'Not available'
                        },
                        {
                            key: 'labeling', group: 'review', icon: 'LB', label: 'Label Review',
                            output: 'Frames · boxes · decisions',
                            description: 'Review and correct object labels and bounding boxes.',
                            ready: this.canOpenReview(), href: this.canOpenReview() ? this.reviewUrl() : '', openLabel: 'Open label review',
                            setupLabel: 'Run object labeling', missingStatus: 'Not generated'
                        },
                        {
                            key: 'temporal_caption', group: 'review', icon: 'TC', label: 'Temporal Caption Review',
                            output: 'Video · segments · captions',
                            description: 'Review captions and compare annotation results.',
                            ready: Boolean(this.temporalCaptionUrl()), href: this.temporalCaptionUrl() + '/review', openLabel: 'Open caption review',
                            setupLabel: 'Create caption task', missingStatus: 'Select dataset'
                        },
                        {
                            key: 'tagging', group: 'review', icon: 'TG', label: 'Tag Review',
                            output: 'Trajectory · grasp heatmap',
                            description: 'Review tags, trajectories, and grasp locations.',
                            ready: Boolean(this.taggingReviewUrl()), href: this.taggingReviewUrl(), openLabel: 'Open tag review',
                            setupLabel: 'Run auto-tagging', missingStatus: 'Not generated'
                        },
                        {
                            key: 'construction', group: 'review', icon: 'DC', label: 'Construction Review',
                            output: 'Synthetic samples · decisions',
                            description: 'Review constructed samples before building a dataset.',
                            ready: Boolean(this.constructionReviewUrl()), href: this.constructionReviewUrl(), openLabel: 'Open construction review',
                            setupLabel: 'Build construction plan', missingStatus: 'Not generated'
                        },
                        {
                            key: 'embedding', group: 'diagnostics', icon: 'EM', label: 'Embedding Map',
                            output: '2D scatter · clusters',
                            description: 'Find clusters and outliers in episode embeddings.',
                            ready: Boolean(this.embeddingUrl()), href: this.embeddingUrl(), openLabel: 'Open embedding map',
                            setupLabel: 'Create embedding', missingStatus: 'Not generated'
                        },
                        {
                            key: 'compare', group: 'diagnostics', icon: 'CP', label: 'Dataset Compare',
                            output: 'Overlap · statistics · samples',
                            description: 'Compare dataset structure, coverage, and samples.',
                            ready: Boolean(this.compareUrl()), href: this.compareUrl(), openLabel: 'Open comparison',
                            setupLabel: 'Select comparison dataset',
                            missingStatus: compareCount ? `${compareCount} saved · choose pair` : 'Choose dataset B'
                        },
                        {
                            key: 'smoothing', group: 'diagnostics', icon: 'SM', label: 'Smoothing Report',
                            output: 'Before/after curves · metrics',
                            description: 'Compare signals before and after smoothing.',
                            ready: Boolean(this.smoothingReportUrl()), href: this.smoothingReportUrl(), openLabel: 'Open smoothing report',
                            setupLabel: 'Run action smoothing', missingStatus: 'Not generated'
                        }
                    ];
                    const supportedItems = this.selectedDataset.remote
                        ? items.filter(item => ['viewer', 'analysis', 'inspect', 'temporal_caption'].includes(item.key))
                        : items;
                    return supportedItems
                        .filter(item => this.openLinkEnabled(item.key))
                        .map(item => ({
                            ...item,
                            status: item.ready ? 'Ready' : item.missingStatus,
                            statusClass: item.ready
                                ? 'border-emerald-800 bg-emerald-950/60 text-emerald-300'
                                : 'border-amber-900 bg-amber-950/50 text-amber-300'
                        }));
                },
                visualizationGroups() {
                    const groups = [
                        {key: 'inspect', label: 'Inspect', description: 'Understand individual episodes and overall dataset coverage.'},
                        {key: 'review', label: 'Review generated artifacts', description: 'Check labels, tags and constructed samples.'},
                        {key: 'diagnostics', label: 'Compare & diagnose', description: 'Explore clusters, coverage and signal changes.'}
                    ];
                    const visibleGroups = new Set(this.visualizationItems().map(item => item.group));
                    return groups.filter(group => visibleGroups.has(group.key));
                },
                visualizationItemsFor(groupKey) {
                    return this.visualizationItems().filter(item => item.group === groupKey);
                },
                visualizationReadyCount() {
                    return this.visualizationItems().filter(item => item.ready).length;
                },
                async openVisualizationSetup(key) {
                    if (key === 'viewer' && this.selectedDataset?.remote) {
                        if (this.selectedRemoteLocation && this.canOperateRemote()) {
                            await this.prepareRemoteViewer(this.selectedRemoteLocation);
                        }
                        return;
                    }
                    const targets = {
                        viewer: 'cache',
                        labeling: 'labeling',
                        tagging: 'tagging',
                        construction: 'construction',
                        embedding: 'embedding',
                        compare: 'compare',
                        smoothing: 'transform'
                    };
                    const tab = targets[key];
                    if (!tab || !this.tabEnabled(tab)) return;
                    await this.selectTab(tab);
                    if (key === 'smoothing') {
                        this.preprocess.schema_op = 'smooth_action';
                        this.syncOptionRules();
                    }
                },
                tabDefaultSchemaOp(tab) {
                    if (tab === 'data_modification') return 'value_edit';
                    if (tab === 'standardize') return 'standardize';
                    if (tab === 'transform') return 'convert_action';
                    if (tab === 'dataset_ops') return this.consoleMode === 'visualize' ? 'delete_episodes' : 'rewrite_prompts';
                    return '';
                },
                syncSchemaOpForTab(tab) {
                    const allowed = {
                        data_modification: ['value_edit'],
                        standardize: ['standardize'],
                        transform: this.selectedDataset?.remote
                            ? ['value_edit', 'convert_action', 'convert_v3', 'repair_v3_video_timestamps', 'drop_field', 'smooth_action']
                            : ['convert_action', 'convert_v3', 'repair_v3_video_timestamps', 'drop_field', 'smooth_action'],
                        dataset_ops: this.selectedDataset?.remote
                            ? ['delete_episodes']
                            : this.consoleMode === 'visualize'
                            ? ['delete_episodes']
                            : ['rewrite_prompts', 'fix_prompt_prepositions', 'apply_prompt_assignments', 'delete_episodes'],
                    };
                    if (!allowed[tab]) return;
                    if (!allowed[tab].includes(this.preprocess.schema_op)) {
                        this.preprocess.schema_op = this.tabDefaultSchemaOp(tab);
                    }
                },
                async selectWorkspace(groupKey, updateUrl = true) {
                    if (updateUrl) this.navigationRevision = (this.navigationRevision || 0) + 1;
                    const group = this.availableGroups().find(item => item.key === groupKey);
                    if (!group) return;
                    this.activeWorkspace = group.key;
                    await this.selectPage(this.rememberedPages?.[group.key] || group.pages[0].key, updateUrl);
                },
                unifiedCurationActive() {
                    return this.controlPlaneEnabled && this.consoleMode === 'full' && Boolean(this.selectedDataset)
                        && ['explore', 'quality', 'annotation', 'dataset_build'].includes(this.activePage)
                        && this.activeTab !== 'explore_overview';
                },
                curationTarget() {
                    if (!this.selectedDataset) return null;
                    return this.selectedDataset.remote
                        ? {location_id: this.selectedDataset.remote_location_id}
                        : {dataset_key: this.selectedDatasetKey};
                },
                async selectPage(pageKey, updateUrl = true) {
                    if (updateUrl) this.navigationRevision = (this.navigationRevision || 0) + 1;
                    if (
                        this.selectedDataset?.remote
                        && !['datasets', 'mcap_import', 'preprocessing', 'admin_operations', 'runs', 'explore', 'annotation', 'quality', 'dataset_build'].includes(pageKey)
                    ) {
                        this.error = 'This Agent dataset currently supports preprocessing, authorized source mutations, Viewer, and Runs.';
                        return;
                    }
                    const found = this.findPage(pageKey);
                    if (!found) return;
                    this.activeWorkspace = found.group.key;
                    this.activePage = found.page.key;
                    (this.rememberedPages ||= {})[found.group.key] = pageKey;
                    const tabs = this.currentSubTabs();
                    if (tabs.length && !tabs.some(tab => tab.key === this.activeTab)) {
                        await this.selectTab(tabs.find(tab => tab.key === this.rememberedTabs?.[pageKey])?.key || tabs[0].key, false);
                    } else if (!tabs.length) {
                        this.activeTab = '';
                    }
                    if (pageKey === 'runs') {
                        await Promise.all([this.loadJobs(), this.loadOperationLogs()]);
                        this.selectFirstVisibleJob();
                    }
                    if (updateUrl) this.updateHomeUrl();
                },
                updateHomeUrl() {
                    const params = new URLSearchParams();
                    if (this.selectedDatasetKey) params.set('select', this.selectedDatasetKey);
                    if (this.activePage) params.set('page', this.activePage);
                    if (this.activeTab) params.set('tab', this.activeTab);
                    const url = params.toString() ? `/?${params.toString()}` : '/';
                    window.history.replaceState(null, '', url);
                },
                async selectTab(tab, updateUrl = true) {
                    if (updateUrl) this.navigationRevision = (this.navigationRevision || 0) + 1;
                    if (tab === 'schema') tab = this.consoleMode === 'visualize' ? 'dataset_ops' : 'transform';
                    if (
                        this.selectedDataset?.remote
                        && this.pageForTab(tab)?.group.key !== 'data_curation'
                        && ![
                            'cache', 'standardize', 'transform', 'split_merge', 'data_modification',
                            'dataset_ops', 'explore_overview', 'temporal_caption', 'mcap_import', 'pipelines'
                        ].includes(tab)
                    ) {
                        this.error = `This Agent dataset does not support the ${tab} workflow yet.`;
                        return;
                    }
                    if (!this.tabEnabled(tab)) return;
                    const found = this.pageForTab(tab);
                    if (!found) return;
                    this.activeTab = tab;
                    (this.rememberedTabs ||= {})[found.page.key] = tab;
                    (this.rememberedPages ||= {})[found.group.key] = found.page.key;
                    this.activeWorkspace = found.group.key;
                    this.activePage = found.page.key;
                    if (this.unifiedCurationActive()) {
                        if (updateUrl) this.updateHomeUrl();
                        return;
                    }
                    this.syncSchemaOpForTab(tab);
                    if (['quality_flags', 'standardize', 'transform', 'dataset_ops', 'split_merge'].includes(tab)) await this.loadPreprocessCapabilities();
                    if (tab === 'quality_flags' && this.adminModeEnabled) {
                        await this.loadDeleteFlagReasonOptions();
                    }
                    if (tab === 'labeling') await this.loadLabelingCapabilities();
                    if (tab === 'construction') await this.loadConstructionPreview();
                    if (tab === 'tagging') {
                        await this.loadTaggingCapabilities();
                        await this.loadTaggingSchema();
                    }
                    if (tab === 'embedding') await this.loadEmbeddingCapabilities();
                    if (tab === 'compare') await this.loadCompareCandidates();
                    if (['versions', 'curation_manifest'].includes(tab)) await this.loadLifecycle();
                    if (updateUrl) this.updateHomeUrl();
                },
                async requestJson(url, payload = null, method = null, timeoutMs = null) {
                    const sameAccount = consoleReadScope(this, null);
                    const sameInput = consoleReadScope(this, null, 'dataset');
                    const requestMethod = method || (payload ? 'POST' : 'GET');
                    const isMutation = !['GET', 'HEAD'].includes(requestMethod.toUpperCase());
                    if (isMutation) {
                        this.error = '';
                        this.remoteError = '';
                        this.submissionNotice = 'Submitting request…';
                    }
                    const controller = window.AbortController ? new AbortController() : null;
                    const timeout = timeoutMs === null ? this.requestTimeoutMs : timeoutMs;
                    const timer = controller && timeout > 0
                        ? window.setTimeout(() => controller.abort(), timeout)
                        : null;
                    try {
                        const response = await fetch(url, {
                            method: requestMethod,
                            headers: payload ? {'Content-Type': 'application/json'} : {},
                            body: payload ? JSON.stringify(payload) : null,
                            signal: controller ? controller.signal : undefined
                        });
                        const text = await response.text();
                        if (!sameAccount()) throw new Error("Account changed; response discarded");
                        let data = {};
                        if (text) {
                            try {
                                data = JSON.parse(text);
                            } catch (err) {
                                throw new Error(`Invalid JSON response from ${url} (HTTP ${response.status})`);
                            }
                        }
                        if (!response.ok) {
                            const requestError = new Error(data.error || `request failed: ${response.status}`);
                            requestError.status = response.status;
                            requestError.payload = data;
                            throw requestError;
                        }
                        if (isMutation && sameInput()) {
                            const jobId = data.job?.job_id || data.job?.id;
                            this.submissionNotice = jobId
                                ? `Task accepted: ${jobId} (${data.job.status || 'queued'}). Track progress in Runs.`
                                : data.request?.status === 'pending'
                                    ? `Deletion request submitted: ${data.request.request_id}. Awaiting admin review in Runs; no data has been deleted.`
                                    : 'Request completed successfully.';
                        }
                        return data;
                    } catch (err) {
                        if (isMutation && sameInput()) {
                            this.submissionNotice = '';
                            this.error = err.name === 'AbortError' || err instanceof TypeError
                                ? 'Submission could not be confirmed. Check Runs before retrying; the server may have received the task.'
                                : `Request failed: ${err.message}`;
                            err.message = this.error;
                        }
                        if (err && err.name === 'AbortError') {
                            throw new Error(isMutation ? this.error : `request timed out: ${url}`);
                        }
                        throw err;
                    } finally {
                        if (timer) window.clearTimeout(timer);
                    }
                },
                parseJsonValue(text, label, fallback) {
                    const value = String(text || '').trim();
                    if (!value) return fallback;
                    try {
                        return JSON.parse(value);
                    } catch (err) {
                        throw new Error(`${label} must be valid JSON`);
                    }
                },
    };
}
