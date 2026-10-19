// Browse state and behavior for the legacy console.
function consoleBrowseState() {
    return {
                datasets: [],
                candidates: [],
                datasetSort: 'cache',
                datasetSearch: '',
                datasetPickerOpen: false,
                datasetPickerSearch: '',
                initialSelectedDataset: {{ initial_selected_dataset|tojson }},
                selectedDatasetKey: null,
                selectedDataset: null,
                selectionToken: 0,
                datasetServerFilter: 'local',
                datasetBrowseRevision: 0,
                scanPath: {{ datasets_root|tojson }},
                remoteScanPath: '',
                controlPlaneNodes: [],
                remoteLocations: [],
                selectedRemoteLocation: null,
                remoteActionBusy: false,
                remoteError: '',
                rootDir: {{ datasets_root|tojson }},
                sourceRoots: [],
                rootProtected: false,
                scanBusy: false,
                scanMessage: '',
                remoteScanSerial: 0,
                remoteScanRequest: null,
                loadingDatasetKey: null,
                registration: {root: ''},
                toggleDatasetPicker(open = !this.datasetPickerOpen) {
                    this.datasetPickerOpen = open;
                    if (open) this.$nextTick(() => this.$refs.datasetPickerSearch.focus());
                },
                closeDatasetPicker() {
                    this.datasetPickerOpen = false;
                    this.$refs.datasetPickerTrigger.focus();
                },
                confirmDatasetChange(key) {
                    if (key === this.selectedDatasetKey) return true;
                    return window.dispatchEvent(new CustomEvent('data-platform:before-dataset-change', {cancelable: true}));
                },
                datasetBrowserStorageKey() {
                    return `dataPlatform.datasetBrowser.${this.controlPlaneUser?.user_id || 'local'}`;
                },
                storeDatasetBrowser() {
                    try {
                        window.localStorage.setItem(this.datasetBrowserStorageKey(), JSON.stringify({
                            server: this.datasetServerFilter,
                            path: this.datasetServerFilter === 'local' ? '' : this.remoteScanPath
                        }));
                    } catch (err) {
                        // Browser storage is optional; browsing still works in the current page.
                    }
                },
                restoreDatasetBrowser() {
                    try {
                        const saved = JSON.parse(window.localStorage.getItem(this.datasetBrowserStorageKey()) || 'null');
                        if (!saved || typeof saved.server !== 'string' || typeof saved.path !== 'string') return false;
                        const node = this.controlPlaneNodes.find(item => item.node_id === saved.server);
                        if (!node) return false;
                        const path = saved.path.replace(/\/+$/, '');
                        if (path && !(node.allowed_roots || []).some(root => {
                            const allowed = String(root).replace(/\/+$/, '');
                            return path === allowed || path.startsWith(`${allowed}/`);
                        })) return false;
                        this.datasetServerFilter = saved.server;
                        this.scanPath = path;
                        this.remoteScanPath = path;
                        return true;
                    } catch (err) {
                        return false;
                    }
                },
                onScanPathInput() {
                    this.datasetBrowseRevision = (this.datasetBrowseRevision || 0) + 1;
                    this.remoteError = '';
                    this.rootProtected = false;
                    if (this.datasetServerFilter === 'local') return;
                    const path = String(this.scanPath || '').trim().replace(/\/+$/, '');
                    if (path && !this.remotePathAllowed(path)) return;
                    this.remoteScanPath = path;
                    this.storeDatasetBrowser();
                },
                storedDatasetKey() {
                    try {
                        const key = this.controlPlaneUser?.user_id
                            ? `dataPlatform.selectedDataset.${this.controlPlaneUser.user_id}` : 'dataPlatform.selectedDataset';
                        return window.localStorage.getItem(key) || '';
                    } catch (err) {
                        return '';
                    }
                },
                storeDatasetKey(key) {
                    try {
                        const storageKey = this.controlPlaneUser?.user_id
                            ? `dataPlatform.selectedDataset.${this.controlPlaneUser.user_id}` : 'dataPlatform.selectedDataset';
                        if (key) window.localStorage.setItem(storageKey, key);
                        else window.localStorage.removeItem(storageKey);
                    } catch (err) {
                        // localStorage can be disabled; URL state still works.
                    }
                },
                async loadDatasetRoots(discover = false) {
                    const current = consoleReadScope(this, 'loadDatasetRoots', 'browse');
                    const requestedRoot = this.rootDir;
                    if (this.datasetAccessRestricted) { await this.loadDatasets(); return; }
                    this.error = '';
                    const scanSerial = this.remoteScanSerial;
                    if (discover) this.scanBusy = true;
                    try {
                        const params = new URLSearchParams({scan: discover ? '1' : '0'});
                        if (this.rootDir) params.set('root_dir', this.rootDir);
                        const suffix = `?${params}`;
                        const data = await this.requestJson(`/api/dataset_roots${suffix}`);
                        if (!current() || this.rootDir !== requestedRoot) return;
                        this.rootDir = data.root_dir || this.rootDir;
                        if (this.datasetServerFilter === 'local') this.scanPath = this.rootDir;
                        this.candidates = data.candidates || [];
                        this.sourceRoots = data.source_roots || [];
                        this.rootProtected = Boolean(data.root_protected);
                        await this.loadDatasets();
                    } catch (err) {
                        if (!current()) return;
                        this.error = err.message;
                    } finally {
                        if (current() && discover && scanSerial === this.remoteScanSerial) this.scanBusy = false;
                    }
                },
                async loadDatasets() {
                    const current = consoleReadScope(this, 'loadDatasets', 'account');
                    const data = await this.requestJson('/api/datasets');
                    if (!current()) return;
                    const previousByKey = new Map(this.datasets.map(item => [item.key, item]));
                    this.datasets = (data.datasets || []).map(item => {
                        const previous = previousByKey.get(item.key);
                        if (previous && previous.loaded) {
                            return {
                                ...previous,
                                ...item,
                                loaded: true,
                                episodes: previous.episodes || []
                            };
                        }
                        return item;
                    });
                    if (this.selectedDatasetKey && !this.selectedDataset?.remote) {
                        const refreshed = this.datasets.find(item => item.key === this.selectedDatasetKey);
                        if (refreshed) {
                            this.selectedDataset = refreshed;
                            this.applyDatasetDataVersion(refreshed);
                        } else {
                            this.selectedDatasetKey = null;
                            this.selectedDataset = null;
                            this.storeDatasetKey('');
                            this.activeJob = null;
                            await this.selectPage('datasets', false);
                            this.updateHomeUrl();
                        }
                    }
                    this.syncActiveJob();
                },
                selectedServerNode() {
                    return this.controlPlaneNodes.find(
                        node => node.node_id === this.datasetServerFilter
                    ) || null;
                },
                knownScanPaths() {
                    const roots = this.datasetServerFilter === 'local'
                        ? [this.rootDir, ...this.sourceRoots]
                        : this.selectedServerNode()?.allowed_roots || [];
                    const paths = roots.map(root => String(root || '').trim()).filter(Boolean);
                    return [...new Set(paths.map(path => path.replace(/\/+$/, '') || '/'))];
                },
                scanPathPreset() {
                    const path = String(this.scanPath || '').trim();
                    const normalized = path.replace(/\/+$/, '') || (path ? '/' : '');
                    return this.knownScanPaths().includes(normalized) ? normalized : '';
                },
                selectScanPath(path) {
                    this.scanPath = path;
                    this.onScanPathInput();
                    if (!path) this.$nextTick(() => this.$refs.customScanPath.focus());
                },
                onDatasetServerChange() {
                    this.datasetBrowseRevision = (this.datasetBrowseRevision || 0) + 1;
                    this.remoteScanSerial += 1;
                    this.scanBusy = false;
                    this.scanMessage = '';
                    this.remoteScanRequest = null;
                    this.selectedRemoteLocation = null;
                    this.remoteViewerJob = null;
                    this.remoteError = '';
                    if (this.datasetServerFilter === 'local') {
                        this.scanPath = this.rootDir;
                        this.storeDatasetBrowser();
                        return;
                    }
                    const node = this.selectedServerNode();
                    this.scanPath = String(node?.allowed_roots?.[0] || '');
                    this.remoteScanPath = this.scanPath;
                    this.storeDatasetBrowser();
                },
                remotePathAllowed(path) {
                    const node = this.selectedServerNode();
                    const requested = String(path || '').replace(/\/+$/, '');
                    return Boolean(node && requested && (node.allowed_roots || []).some(root => {
                        const allowed = String(root || '').replace(/\/+$/, '');
                        return requested === allowed || requested.startsWith(`${allowed}/`);
                    }));
                },
                remoteScanDisabledReason() {
                    if (this.datasetServerFilter === 'local') return '';
                    if (!this.canOperateRemote()) return 'Immediate scans require an admin, data manager or operator account. Use Refresh to reload the list.';
                    const node = this.selectedServerNode();
                    if (node?.dataset_scan_allowed === false) return 'Immediate scans require unrestricted dataset access. Use Refresh to reload your permitted datasets.';
                    if (node?.capabilities?.dataset_scan_protocol !== 1) return 'Upgrade the selected Agent to support immediate scans.';
                    if (node?.status !== 'online') return 'The selected Agent is offline. Use Refresh to reload the registered list.';
                    return '';
                },
                async refreshSelectedServer() {
                    const requested = String(this.scanPath || '').trim();
                    if (this.datasetServerFilter === 'local') {
                        this.rootDir = requested;
                        await this.loadDatasetRoots();
                    } else {
                        this.onScanPathInput();
                        await this.loadRemoteSources();
                    }
                },
                async scanSelectedServer() {
                    if (this.scanBusy) return;
                    const requested = String(this.scanPath || '').trim();
                    if (!requested) return;
                    this.error = '';
                    this.remoteError = '';
                    this.scanMessage = '';
                    if (this.datasetServerFilter === 'local') {
                        this.rootDir = requested;
                        await this.loadDatasetRoots(true);
                        return;
                    }
                    const denied = this.remoteScanDisabledReason();
                    if (denied) { this.remoteError = denied; return; }
                    if (!this.remotePathAllowed(requested)) {
                        this.remoteError = 'Path must be inside this Agent\'s allowed roots.';
                        return;
                    }
                    const nodeId = this.datasetServerFilter;
                    const serial = ++this.remoteScanSerial;
                    const current = () => serial === this.remoteScanSerial && nodeId === this.datasetServerFilter && requested === String(this.scanPath || '').trim();
                    this.scanBusy = true;
                    try {
                        let scan = this.remoteScanRequest;
                        if (!scan || scan.node_id !== nodeId || scan.path !== requested.replace(/\/+$/, '')) {
                            const data = await this.requestJson(`/api/control/nodes/${encodeURIComponent(nodeId)}/scans`, {path: requested});
                            scan = data.scan;
                        }
                        if (!current()) return;
                        this.remoteScanRequest = scan;
                        const deadline = Date.now() + 180000;
                        while (current()) {
                            if (scan.status === 'error') throw new Error(scan.error || 'Agent scan failed');
                            if (scan.status === 'done') {
                                this.remoteScanRequest = null;
                                await this.loadRemoteSources();
                                if (!current()) return;
                                if (this.remoteError) throw new Error(this.remoteError);
                                this.remoteScanPath = requested.replace(/\/+$/, '');
                                this.storeDatasetBrowser();
                                this.scanMessage = `Scan completed: ${scan.dataset_count} dataset(s) discovered under this path.`;
                                return;
                            }
                            this.scanMessage = scan.status === 'running' ? 'Agent is scanning the requested path…' : 'Scan requested; waiting for the Agent…';
                            if (Date.now() >= deadline) throw new Error('Scan is still pending on the Agent. Click Scan to check again; Refresh reloads the registered list.');
                            await new Promise(resolve => window.setTimeout(resolve, 1000));
                            if (!current()) return;
                            scan = (await this.requestJson(`/api/control/scans/${encodeURIComponent(scan.request_id)}`)).scan;
                            if (!current()) return;
                            this.remoteScanRequest = scan;
                        }
                    } catch (err) {
                        if (current()) {
                            this.remoteError = err.message;
                            this.scanMessage = '';
                            if (this.remoteScanRequest?.status === 'error') this.remoteScanRequest = null;
                        }
                    } finally {
                        if (serial === this.remoteScanSerial) this.scanBusy = false;
                    }
                },
                remoteLocationsForSelectedServer() {
                    const base = String(this.remoteScanPath || '').replace(/\/+$/, '');
                    return this.remoteLocations.filter(location => {
                        if (location.node_id !== this.datasetServerFilter) return false;
                        const root = String(location.root || '').replace(/\/+$/, '');
                        return !base || root === base || root.startsWith(`${base}/`);
                    });
                },
                filteredRemoteLocations() {
                    const query = String(this.datasetSearch || '').trim().toLowerCase();
                    const tokens = query.split(/\s+/).filter(Boolean);
                    const locations = this.remoteLocationsForSelectedServer().filter(location => {
                        if (!tokens.length) return true;
                        const haystack = this.datasetSearchHaystack(this.remoteDatasetRecord(location));
                        return tokens.every(token => haystack.includes(token));
                    });
                    locations.sort((left, right) => {
                        const leftDataset = this.remoteDatasetRecord(left);
                        const rightDataset = this.remoteDatasetRecord(right);
                        if (this.datasetSort === 'cache') {
                            const cacheDiff = this.cacheRank(leftDataset) - this.cacheRank(rightDataset);
                            if (cacheDiff !== 0) return cacheDiff;
                        }
                        if (this.datasetSort === 'path') {
                            return String(left.root || '').localeCompare(String(right.root || ''));
                        }
                        return this.remoteDatasetName(left).localeCompare(this.remoteDatasetName(right));
                    });
                    return locations;
                },
                canRequestEpisodeDeletion() {
                    return Boolean(this.selectedDataset?.remote && this.selectedRemoteLocation
                        && this.controlPlaneUser?.role === 'operator');
                },
                async loadDeletionRequests() {
                    if (!this.controlPlaneEnabled) return;
                    const data = await this.requestJson('/api/control/episode-deletion-requests');
                    this.deletionRequests = data.requests || [];
                },
                async reviewDeletionRequest(item, decision) {
                    const reason = window.prompt(`Reason to ${decision} deletion of episodes ${item.episodes.join(', ')} from ${item.dataset_key}:`);
                    if (!reason?.trim()) return;
                    if (decision === 'approve' && !window.confirm(`Approve source deletion in ${item.dataset_key}?\nEpisodes: ${item.episodes.join(', ')}\nRequester: ${item.requested_by}\nReason: ${item.reason}`)) return;
                    this.busy = true;
                    try {
                        const data = await this.requestJson(`/api/control/episode-deletion-requests/${item.request_id}/review`, {
                            decision, reason: reason.trim(), confirmation: `DELETE ${item.request_id}`
                        });
                        if (data.job) {
                            this.mergeRemoteJob(data.job);
                            this.scheduleRemoteRefresh();
                        }
                        await this.loadDeletionRequests();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async loadRemoteSources(restoreBrowser = false) {
                    const browseRevision = this.datasetBrowseRevision || 0;
                    if (!this.controlPlaneEnabled) return;
                    const requestId = this.remoteLoadRequest = (this.remoteLoadRequest || 0) + 1;
                    try {
                        this.loadDeletionRequests().catch(err => { this.error = err.message; });
                        const results = await Promise.allSettled([
                            this.requestJson('/api/control/nodes').then(data => {
                                if (requestId !== this.remoteLoadRequest) return;
                                this.controlPlaneNodes = data.nodes || [];
                                if (restoreBrowser && (this.datasetBrowseRevision || 0) === browseRevision) {
                                    this.restoreDatasetBrowser();
                                }
                                this.mergeRemoteJobsIntoRuns();
                            }),
                            this.requestJson('/api/control/locations?summary=1').then(data => {
                                if (requestId !== this.remoteLoadRequest) return;
                                this.remoteLocations = data.locations || [];
                                if (this.selectedRemoteLocation) {
                                    this.selectedRemoteLocation = this.remoteLocations.find(
                                        item => item.location_id === this.selectedRemoteLocation.location_id
                                    ) || null;
                                }
                                if (this.selectedDataset?.remote && this.selectedRemoteLocation) {
                                    this.selectedDataset = this.remoteDatasetRecord(this.selectedRemoteLocation);
                                }
                                this.syncRemoteViewerJob();
                                this.mergeRemoteJobsIntoRuns();
                            }),
                            this.loadRemoteJobs()
                        ]);
                        if (requestId !== this.remoteLoadRequest) return;
                        this.remoteError = results.flatMap((result, index) => result.status === 'rejected'
                            ? [`${['Nodes', 'Datasets', 'Tasks'][index]}: ${result.reason?.message || 'Request failed'}`]
                            : []).join('; ');
                    } catch (err) {
                        if (requestId === this.remoteLoadRequest) this.remoteError = err.message;
                    }
                },
                async selectRemoteLocation(location) {
                    this.selectedRemoteLocation = location;
                    this.remoteViewerJob = this.latestRemoteJob(location);
                    if (!this.remoteViewerJob || !['queued', 'running', 'cancel_requested', 'interrupted'].includes(this.remoteViewerJob.status)) return;
                    await this.refreshRemoteViewerJob();
                    this.scheduleRemoteRefresh();
                },
                remoteDatasetRecord(location) {
                    const metadata = location?.metadata || {};
                    const features = metadata.features || {};
                    const editableFeatures = ['action', 'state'].flatMap(key => {
                        const feature = features[key];
                        const shape = Array.isArray(feature?.shape) ? feature.shape : [];
                        const dimension = Number(shape[shape.length - 1] || 0);
                        if (!Number.isInteger(dimension) || dimension <= 0) return [];
                        const names = Array.isArray(feature.names) && feature.names.length === dimension
                            ? feature.names.map(String)
                            : [];
                        return [{
                            key,
                            label: key === 'action' ? 'Action' : 'State',
                            dimension,
                            names
                        }];
                    });
                    return {
                        key: `remote:${location.location_id}`,
                        repo_id: location.dataset_key,
                        root: location.root,
                        output_dir: location.output_dir || '',
                        loaded: true,
                        available: location.state === 'available',
                        registered: true,
                        state: location.state || 'available',
                        remote: true,
                        remote_location_id: location.location_id,
                        remote_node_id: location.node_id,
                        server_name: location.node_name || location.node_hostname || 'Agent',
                        episode_count: Number(metadata.total_episodes || 0),
                        total_frames: Number(metadata.total_frames || 0),
                        features,
                        editable_features: editableFeatures,
                        dataset_format_version: metadata.codebase_version || '',
                        data_version: metadata.data_version || null,
                        default_processing_profile: metadata.default_processing_profile || null,
                        robot_type: metadata.robot_type || 'unknown',
                        data_profile: metadata.data_profile,
                        operation_capabilities: metadata.operation_capabilities,
                        source_protected: false,
                        stage: metadata.stage || 'raw',
                        viewer_url: metadata.viewer_url || '',
                        analysis_url: `/remote/${encodeURIComponent(location.location_id)}/analysis`,
                        cache: {status: metadata.viewer_url ? 'cached' : 'missing'}
                    };
                },
                remoteDatasetName(location) {
                    return this.datasetName(this.remoteDatasetRecord(location));
                },
                async useRemoteDataset(location, updateUrl = true, options = {}) {
                    if (!location || !this.confirmDatasetChange(`remote:${location.location_id}`)) return;
                    if (updateUrl) this.navigationRevision = (this.navigationRevision || 0) + 1;
                    ++this.selectionToken;
                    const locationRoot = String(location.root || '');
                    const base = String(this.remoteScanPath || '').replace(/\/+$/, '');
                    const insideBrowser = this.datasetServerFilter === location.node_id
                        && (!base || locationRoot === base || locationRoot.startsWith(`${base}/`));
                    if (!options.preserveBrowser && !insideBrowser) {
                        this.datasetServerFilter = location.node_id;
                        const node = this.selectedServerNode();
                        const scanRoot = (node?.allowed_roots || []).find(root => (
                            locationRoot === String(root).replace(/\/+$/, '')
                            || locationRoot.startsWith(`${String(root).replace(/\/+$/, '')}/`)
                        )) || node?.allowed_roots?.[0] || '';
                        this.scanPath = String(scanRoot);
                        this.remoteScanPath = String(scanRoot).replace(/\/+$/, '');
                    }
                    if (!options.preserveBrowser) this.storeDatasetBrowser();
                    this.selectedRemoteLocation = location;
                    this.selectedDataset = this.remoteDatasetRecord(location);
                    this.selectedDatasetKey = this.selectedDataset.key;
                    this.dataVersionManual = false;
                    this.resetDeleteFlagReasonState();
                    this.syncValueEditFeatures();
                    this.applyDatasetDataVersion(this.selectedDataset, true);
                    this.preprocess.split_merge_op = 'split';
                    this.preprocess.merge_src_keys = [this.selectedDataset.key];
                    this.preprocess.merge_delete_episodes = {};
                    this.preprocess.merge_out_root = '';
                    this.prefillMergeOutput();
                    this.storeDatasetKey(this.selectedDataset.key);
                    this.syncSchemaOpForTab(this.activeTab);
                    if (!['datasets', 'preprocessing', 'admin_operations', 'runs', 'explore', 'quality', 'annotation', 'dataset_build'].includes(this.activePage)) {
                        await this.selectPage('preprocessing', false);
                    }
                    this.remoteViewerJob = this.latestRemoteJob(location);
                    this.activeJob = this.remoteViewerJob
                        ? this.remoteJobForRuns(this.remoteViewerJob)
                        : null;
                    if (this.remoteViewerJob && ['queued', 'running', 'cancel_requested', 'interrupted'].includes(this.remoteViewerJob.status)) {
                        this.scheduleRemoteRefresh();
                    }
                    if (updateUrl) this.updateHomeUrl();
                },
                async openDatasetTab(item, tab) {
                    if (item?.location_id && item?.node_id) {
                        await this.useRemoteDataset(item);
                        if (this.selectedRemoteLocation?.location_id !== item.location_id) return;
                    } else {
                        await this.selectDataset(item);
                        if (this.selectedDatasetKey !== item?.key) return;
                    }
                    await this.selectTab(tab);
                },
                async selectCandidate(candidate) {
                    if (!candidate || !candidate.root) return;
                    this.registration.root = candidate.root;
                    const existing = this.datasets.find(item => item.root === candidate.root || item.key === candidate.registered_key);
                    if (existing) {
                        await this.selectDataset(existing);
                        return;
                    }
                    if (!this.canAutoCatalog()) return;
                    await this.registerDataset(candidate.root);
                },
                replaceDataset(dataset) {
                    const idx = this.datasets.findIndex(item => item.key === dataset.key);
                    if (idx >= 0) {
                        this.datasets.splice(idx, 1, dataset);
                    } else {
                        this.datasets.push(dataset);
                    }
                },
                async loadDatasetDetails(datasetKey, token = null) {
                    this.loadingDatasetKey = datasetKey;
                    try {
                        const data = await this.requestJson(`/api/datasets/${datasetKey}`, null, 'GET', this.datasetDetailTimeoutMs);
                        this.replaceDataset(data.dataset);
                        if (this.selectedDatasetKey === datasetKey && (token === null || token === this.selectionToken)) {
                            this.selectedDataset = data.dataset;
                            this.syncValueEditFeatures();
                            this.applyDatasetDataVersion(data.dataset);
                        }
                        return data.dataset;
                    } finally {
                        if (this.loadingDatasetKey === datasetKey) {
                            this.loadingDatasetKey = null;
                        }
                    }
                },
                async finishDatasetSelection(dataset, token) {
                    if (!dataset.loaded) {
                        try {
                            const loaded = await this.loadDatasetDetails(dataset.key, token);
                            if (token !== this.selectionToken || this.selectedDatasetKey !== dataset.key) return false;
                            this.registration.root = loaded.root || this.registration.root;
                            this.applyDatasetDataVersion(loaded);
                            this.construction.out_root = this.syntheticDefaultPath(loaded.root);
                        } catch (err) {
                            if (token !== this.selectionToken || this.selectedDatasetKey !== dataset.key) return false;
                            this.error = err.message;
                            return false;
                        }
                    }
                    if (this.activeTab === 'construction' && token === this.selectionToken) {
                        await this.loadConstructionPreview();
                    }
                    return true;
                },
                async selectDataset(dataset, updateUrl = true, options = {}) {
                    if (!this.confirmDatasetChange(dataset.key)) return;
                    if (updateUrl) this.navigationRevision = (this.navigationRevision || 0) + 1;
                    const token = ++this.selectionToken;
                    if (!options.preserveBrowser) {
                        this.datasetServerFilter = 'local';
                        this.scanPath = this.rootDir;
                        this.storeDatasetBrowser();
                    }
                    this.selectedRemoteLocation = null;
                    this.remoteViewerJob = null;
                    const datasetChanged = this.selectedDatasetKey !== dataset.key;
                    if (datasetChanged) this.dataVersionManual = false;
                    if (datasetChanged) this.resetDeleteFlagReasonState();
                    this.selectedDataset = dataset;
                    this.selectedDatasetKey = dataset.key;
                    this.syncValueEditFeatures();
                    if (this.selectedDatasetIsV3() && this.preprocess.schema_op === 'convert_v3') {
                        this.preprocess.schema_op = 'convert_action';
                    }
                    this.applyDatasetDataVersion(dataset, datasetChanged);
                    this.storeDatasetKey(dataset.key);
                    if (updateUrl) this.updateHomeUrl();
                    this.syncActiveJob();
                    if (!this.preprocess.merge_src_keys.includes(dataset.key)) {
                        this.preprocess.merge_src_keys = [dataset.key];
                    }
                    this.preprocess.subtract_src_keys = (this.preprocess.subtract_src_keys || []).filter(key => key !== dataset.key);
                    this.constructionPreview = null;
                    this.constructionLoading = false;
                    this.registration.root = dataset.root || this.registration.root;
                    this.construction.out_root = this.syntheticDefaultPath(dataset.root);
                    this.loadOperationLogs().catch(err => {
                        if (token === this.selectionToken && !this.error) this.error = err.message;
                    });
                    if (this.tabEnabled('versions')) {
                        this.loadLifecycle().catch(err => {
                            if (token === this.selectionToken && !this.error) this.error = err.message;
                        });
                    }
                    const detailPromise = this.finishDatasetSelection(dataset, token);
                    if (options.deferDetails) {
                        detailPromise.catch(err => {
                            if (token === this.selectionToken && !this.error) this.error = err.message;
                        });
                    } else {
                        await detailPromise;
                    }
                },
                async registerDataset(root = this.registration.root) {
                    if (!root) return null;
                    this.error = '';
                    this.busy = true;
                    this.registration.root = root;
                    try {
                        const data = await this.requestJson('/api/datasets/register', {root});
                        await this.loadDatasetRoots();
                        const dataset = this.datasets.find(
                            item => item.key === data.dataset.key || item.root === data.dataset.root
                        ) || data.dataset;
                        await this.selectDataset(dataset);
                        return dataset;
                    } catch (err) {
                        this.error = err.message;
                        return null;
                    } finally {
                        this.busy = false;
                    }
                },
                async setSourceRootProtection(protectedValue) {
                    if (!this.rootDir) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson('/api/source_roots', {
                            root: this.rootDir,
                            protected: Boolean(protectedValue)
                        }, 'PATCH');
                        this.sourceRoots = data.source_roots || [];
                        await this.loadDatasetRoots();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async setDatasetProtection(dataset, protectedValue) {
                    if (!dataset || !dataset.key) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        const data = await this.requestJson(`/api/datasets/${dataset.key}/protection`, {
                            protected: Boolean(protectedValue),
                            reason: protectedValue ? 'Marked as source data in the Data Platform console' : ''
                        }, 'PATCH');
                        this.replaceDataset(data.dataset);
                        if (this.selectedDatasetKey === dataset.key) {
                            this.selectedDataset = data.dataset;
                        }
                        await this.loadDatasetRoots();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                async deleteDataset(dataset) {
                    if (!dataset || !dataset.key) return;
                    const ok = window.confirm(`Remove ${dataset.repo_id} from Registered?\n\nDataset files and cache will not be deleted.`);
                    if (!ok) return;
                    this.error = '';
                    this.busy = true;
                    try {
                        await this.requestJson(`/api/datasets/${dataset.key}`, null, 'DELETE');
                        if (this.selectedDatasetKey === dataset.key) {
                            this.selectedDatasetKey = null;
                            this.selectedDataset = null;
                            this.storeDatasetKey('');
                            this.constructionPreview = null;
                            this.activeJob = null;
                            this.resetDeleteFlagReasonState();
                            await this.selectPage('datasets', false);
                            this.updateHomeUrl();
                        }
                        await this.loadDatasetRoots();
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.busy = false;
                    }
                },
                cacheRank(item) {
                    const status = item && item.cache ? item.cache.status : '';
                    if (status === 'cached') return 0;
                    if (status === 'missing') return 1;
                    if (status === 'unknown') return 2;
                    return 3;
                },
                datasetName(item) {
                    if (!item) return '';
                    const root = item.root || '';
                    const parts = root.split('/').filter(Boolean);
                    return parts.length > 0 ? parts[parts.length - 1] : (item.repo_id || item.name || item.key || '');
                },
                datasetServerName(item) {
                    return item?.remote ? (item.server_name || 'Agent') : 'Server A';
                },
                datasetStageLabel(item) {
                    const stage = String((item && item.stage) || 'raw').toLowerCase();
                    if (stage === 'standard') return 'Standard';
                    if (stage === 'curated') return 'Curated';
                    return 'Raw';
                },
                datasetStageClass(item) {
                    const stage = String((item && item.stage) || 'raw').toLowerCase();
                    if (stage === 'standard') {
                        return 'border-violet-800 bg-violet-950 text-violet-200';
                    }
                    if (stage === 'curated') {
                        return 'border-fuchsia-800 bg-fuchsia-950 text-fuchsia-200';
                    }
                    return 'border-slate-700 bg-slate-900 text-slate-300';
                },
                datasetStageHint(item) {
                    const label = this.datasetStageLabel(item);
                    if (label === 'Curated') return 'Curated: reviewed construction or Manifest output';
                    if (label === 'Standard') return 'Standard: immutable preprocessing output';
                    return 'Raw: original input without preprocessing evidence';
                },
                dataVersionText(item) {
                    return item?.data_version || null;
                },
                dataProfileLabel(item) {
                    if (!item) return '';
                    return `${item.robot_type || 'Unknown robot'} · ${item.dataset_format_version || item.codebase_version || 'Unknown format'}`;
                },
                normalizeDataVersion(value, fallback = null) {
                    const version = String(value || fallback || '').toUpperCase();
                    return ['DVT1', 'DVT2'].includes(version) ? version : null;
                },
                dataVersionClass(item) {
                    return 'border border-slate-700 bg-slate-900 text-slate-300';
                },
                sortItems(items) {
                    const values = [...items];
                    values.sort((a, b) => {
                        if (this.datasetSort === 'cache') {
                            const cacheDiff = this.cacheRank(a) - this.cacheRank(b);
                            if (cacheDiff !== 0) return cacheDiff;
                        }
                        if (this.datasetSort === 'path') {
                            return String(a.root || '').localeCompare(String(b.root || ''));
                        }
                        return this.datasetName(a).localeCompare(this.datasetName(b));
                    });
                    return values;
                },
                datasetSearchHaystack(item) {
                    const cache = item && item.cache ? item.cache : {};
                    const cacheParts = [
                        cache.status,
                        cache.videos && cache.videos.status,
                        cache.csv && cache.csv.status,
                    ];
                    return [
                        item && item.name,
                        item && item.key,
                        item && item.repo_id,
                        item && item.root,
                        item && item.output_dir,
                        this.datasetServerName(item || {}),
                        this.datasetName(item || {}),
                        this.datasetStatusText(item || {}),
                        this.dataVersionText(item || {}),
                        ...cacheParts,
                    ].filter(Boolean).join(' ').toLowerCase();
                },
                filterDatasets(items) {
                    const query = String(this.datasetSearch || '').trim().toLowerCase();
                    if (!query) return items;
                    const tokens = query.split(/\s+/).filter(Boolean);
                    return items.filter(item => {
                        const haystack = this.datasetSearchHaystack(item);
                        return tokens.every(token => haystack.includes(token));
                    });
                },
                localRootMatches(path) {
                    const root = String(path || '').replace(/\/+$/, '');
                    const base = String(this.rootDir || '').replace(/\/+$/, '');
                    return Boolean(root) && (!base || root === base || root.startsWith(`${base}/`));
                },
                localRegisteredDatasets() {
                    return this.sortItems(this.filterDatasets(
                        this.datasets.filter(item => this.localRootMatches(item.root))
                    ));
                },
                localUncatalogedDatasets() {
                    return this.sortItems(this.filterDatasets(
                        this.candidates.filter(item => !item.registered)
                    ));
                },
                localDatasetCount() {
                    return this.datasets.filter(item => this.localRootMatches(item.root)).length
                        + this.candidates.filter(item => !item.registered).length;
                },
                visibleLocalDatasetCount() {
                    return this.localRegisteredDatasets().length + this.localUncatalogedDatasets().length;
                },
                pickerDatasets() {
                    const query = String(this.datasetPickerSearch || '').trim().toLowerCase();
                    const tokens = query.split(/\s+/).filter(Boolean);
                    const allDatasets = [
                        ...this.datasets,
                        ...(this.canAutoCatalog()
                            ? this.candidates.filter(item => !item.registered).map(item => ({...item, local_candidate: true}))
                            : []),
                        ...this.remoteLocations.map(location => this.remoteDatasetRecord(location))
                    ];
                    const values = tokens.length
                        ? allDatasets.filter(item => {
                            const haystack = this.datasetSearchHaystack(item);
                            return tokens.every(token => haystack.includes(token));
                        })
                        : allDatasets;
                    return this.sortItems(values);
                },
                async selectDatasetFromPicker(dataset) {
                    this.datasetPickerOpen = false;
                    this.datasetPickerSearch = '';
                    if (dataset?.remote) {
                        const location = this.remoteLocations.find(
                            item => item.location_id === dataset.remote_location_id
                        );
                        if (location) await this.useRemoteDataset(location);
                        return;
                    }
                    if (dataset?.local_candidate) {
                        await this.selectCandidate(dataset);
                        return;
                    }
                    await this.selectDataset(dataset);
                },
                datasetSearchSummary() {
                    return `showing ${this.visibleLocalDatasetCount()} / ${this.localDatasetCount()}`;
                },
                selectedDatasetIsV3() {
                    return this.datasetIsV3(this.selectedDataset);
                },
                datasetIsV3(dataset) {
                    if (!dataset) return false;
                    const version = String(dataset.dataset_format_version || '').replace(/^v/i, '');
                    return version === '3' || version.startsWith('3.');
                },
                hasCacheStatus(dataset) {
                    return Boolean(dataset && dataset.cache && !['not_loaded', 'unknown'].includes(dataset.cache.status));
                },
                datasetStatusText(dataset) {
                    if (!dataset) return 'unknown';
                    if (dataset.cache && dataset.cache.status === 'cached') return 'viewer ready';
                    return 'viewer missing';
                },
                datasetStatusClass(dataset) {
                    if (dataset.cache && dataset.cache.status === 'cached') return 'bg-emerald-950 text-emerald-300 border border-emerald-800';
                    return 'bg-slate-900 text-slate-400 border border-slate-700';
                },
                openViewerText() {
                    if (!this.selectedDataset) return 'Select dataset first';
                    if (!this.selectedDataset.loaded) return 'Preparing dataset details…';
                    if (this.selectedDatasetIsV3() && !this.canOpenViewer()) return 'Prepare v3.0 viewer cache first';
                    return this.canOpenViewer() ? 'Open viewer' : 'Prepare video + CSV first';
                },
                viewerUrl() {
                    if (!this.canOpenViewer()) return '';
                    let url = '';
                    if (this.activeJob && this.activeJob.viewer_url && this.activeJob.dataset_key === this.selectedDataset.key) url = this.activeJob.viewer_url;
                    else if (this.selectedDataset && this.selectedDataset.viewer_url) url = this.selectedDataset.viewer_url;
                    if (!url) return '';
                    const separator = url.includes('?') ? '&' : '?';
                    return `${url}${separator}data_version=${encodeURIComponent(this.options.data_version || '')}`;
                },
                reviewUrl() {
                    if (!this.selectedDataset) return '';
                    if (this.activeJob && this.activeJob.review_url && this.activeJob.dataset_key === this.selectedDataset.key) return this.activeJob.review_url;
                    if (this.selectedDataset.labeling && this.selectedDataset.labeling?.review_url) return this.selectedDataset.labeling?.review_url;
                    if (this.selectedDataset.cache_only) return '';
                    return `/${this.selectedDataset.key}/labeling`;
                },
                analysisUrl() {
                    if (!this.selectedDataset) return '';
                    if (this.selectedDataset.remote) return this.selectedDataset.analysis_url || '';
                    return this.selectedDataset.analysis_url || `/${this.selectedDataset.key}/analysis`;
                },
                inspectUrl() {
                    if (!this.selectedDataset) return '';
                    if (this.selectedDataset.remote) {
                        const id = this.selectedRemoteLocation?.location_id;
                        return id ? `/remote/${encodeURIComponent(id)}/inspect` : '';
                    }
                    return `/${this.selectedDataset.key}/inspect`;
                },
    };
}
