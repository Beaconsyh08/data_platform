// Runs state and behavior for the legacy console.
function consoleRunsState() {
    return {
                jobs: [], jobControlBusy: '', jobControlError: '', jobControlNotice: '',
                activeJob: null,
                operationLogs: [],
                operationLogsLoading: false,
                operationLogsError: '',
                operationLogFilter: {status: '', operation: ''},
                jobDrawerOpen: false,
                selectedJobId: '',
                runsFilter: {scope: 'all', status: ''},
                jobsCursor: null, jobsHasMore: false, jobsPageScope: '', jobsPageMode: 'all', jobsPagesExpanded: false,
                jobsCreatedFrom: '', jobsCreatedTo: '',
                remoteJobs: [],
                remoteViewerJob: null,
                remoteRefreshTimer: null,
                showDetailedJobLogs: false,
                selectedRemoteJobTimer: null,
                deletionRequests: [],
                pollTimer: null,
                pollJobId: null,
                lastResumeAt: 0,
                storedJobId() {
                    try {
                        return window.localStorage.getItem('dataPlatform.selectedJob') || '';
                    } catch (err) {
                        return '';
                    }
                },
                storeJobId(jobId) {
                    try {
                        if (jobId) window.localStorage.setItem('dataPlatform.selectedJob', jobId);
                        else window.localStorage.removeItem('dataPlatform.selectedJob');
                    } catch (err) {
                        // localStorage can be disabled; the current page still keeps the selection.
                    }
                },
                async loadRemoteJobs(append = false) {
                    const requestId = this.remoteJobsLoadRequest = (this.remoteJobsLoadRequest || 0) + 1;
                    const filters = {summary: '1', page_size: '50'};
                    const selected = this.runsFilter.scope === 'current' ? this.selectedRemoteLocation?.location_id : '';
                    if (selected) filters.location_id = selected;
                    const status = this.runsFilter.status === 'running' ? 'active' : this.runsFilter.status;
                    if (status || this.jobsPageMode !== 'all') filters.status = status || this.jobsPageMode;
                    if (this.jobsCreatedFrom) filters.created_from = new Date(this.jobsCreatedFrom).toISOString();
                    if (this.jobsCreatedTo) filters.created_to = new Date(this.jobsCreatedTo).toISOString();
                    const scope = JSON.stringify({filters, actor: this.controlPlaneUser?.user_id, role: this.controlPlaneUser?.role});
                    const changed = scope !== this.jobsPageScope;
                    if (changed) {append = false; this.jobsCursor = null; this.jobsHasMore = false; this.jobsPagesExpanded = false;}
                    if (append) this.jobsPagesExpanded = true;
                    const params = new URLSearchParams(filters);
                    if (append && this.jobsCursor) params.set('cursor', this.jobsCursor);
                    const data = await this.requestJson('/api/control/jobs?' + params.toString());
                    if (requestId !== this.remoteJobsLoadRequest) return;
                    const previousById = new Map(this.remoteJobs.map(job => [job.job_id, job]));
                    this.jobsPageScope = scope;
                    if (append || changed || !this.jobsPagesExpanded) {
                        this.jobsCursor = data.next_cursor || null; this.jobsHasMore = !!data.has_more;
                    }
                    const incoming = new Set((data.jobs || []).map(job => job.job_id));
                    const retained = !changed && this.jobsPagesExpanded
                        ? this.remoteJobs.filter(job => !incoming.has(job.job_id)) : [];
                    this.remoteJobs = (data.jobs || []).map(job => {
                        const previous = previousById.get(job.job_id);
                        if (previous?.updated_at && job.updated_at && previous.updated_at > job.updated_at) return previous;
                        return previous?.events && previous.attempt_id === job.attempt_id ? {...job, events: previous.events} : job;
                    });
                    this.remoteJobs.push(...retained);
                    this.syncRemoteViewerJob();
                    this.mergeRemoteJobsIntoRuns();
                },
                async resetJobPages() {
                    this.jobsPageScope = ''; this.jobsCursor = null; this.jobsHasMore = false;
                    await this.loadJobs();
                },
                async moreJobPages() {
                    if (!this.jobsHasMore) return;
                    try {await this.loadRemoteJobs(true);} catch (error) {this.remoteError = `Tasks: ${error.message}`;}
                },
                syncRemoteViewerJob() {
                    if (!this.selectedRemoteLocation) {
                        this.remoteViewerJob = null;
                        return;
                    }
                    this.remoteViewerJob = this.latestRemoteJob(this.selectedRemoteLocation);
                },
                latestRemoteJob(location) {
                    if (!location) return null;
                    const jobs = (this.remoteJobs || []).filter(
                        job => job.location_id === location.location_id
                    );
                    jobs.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
                    return jobs[0] || null;
                },
                latestRemoteViewerJob(location) {
                    if (!location) return null;
                    const jobs = (this.remoteJobs || []).filter(job => (
                        job.location_id === location.location_id && job.operation === 'viewer.prepare'
                    ));
                    jobs.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
                    return jobs[0] || null;
                },
                mergeRemoteJob(job) {
                    if (!job?.job_id) return;
                    const previous = this.remoteJobs.find(item => item.job_id === job.job_id);
                    if (previous?.updated_at && job.updated_at && previous.updated_at > job.updated_at) return;
                    const index = this.remoteJobs.findIndex(item => item.job_id === job.job_id);
                    if (index >= 0) this.remoteJobs.splice(index, 1, job);
                    else this.remoteJobs.unshift(job);
                    if (this.selectedRemoteLocation?.location_id === job.location_id) {
                        this.remoteViewerJob = job;
                        this.activeJob = this.remoteJobForRuns(job);
                    }
                    this.mergeRemoteJobsIntoRuns();
                },
                async trackMcapJobs(jobs) {
                    jobs.forEach(job => this.mergeRemoteJob(job));
                    this.runsFilter.scope = 'all';
                    this.runsFilter.status = '';
                    this.jobDrawerOpen = true;
                    await this.selectJob(this.remoteJobForRuns(jobs[0]));
                },
                remoteJobForRuns(job) {
                    if (!job) return null;
                    const events = job.status === 'queued' ? [] : (job.events || []).filter(
                        event => !event.payload?.attempt_id || event.payload.attempt_id === job.attempt_id
                    );
                    const lastEvent = events.length ? events[events.length - 1] : null;
                    const payload = job.progress || job.result?.progress || lastEvent?.payload || {};
                    const total = Number(payload.total || 0);
                    const current = Number(payload.current || 0);
                    let progress = Number(payload.progress);
                    if (!Number.isFinite(progress) && total > 0) progress = current / total * 100;
                    if (job.status === 'done') progress = 100;
                    const result = job.result || {};
                    const outputLocation = result.synced_location || result.dataset_location || null;
                    const outputRoot = result.preprocess?.out_root
                        || outputLocation?.root
                        || result.backup_root
                        || '';
                    const location = this.remoteLocations.find(
                        item => item.location_id === job.location_id
                    );
                    const node = this.controlPlaneNodes.find(item => item.node_id === job.node_id);
                    const serverName = location?.node_name || node?.name || node?.hostname || 'Agent';
                    const outputLocationId = result.output_location_id || outputLocation?.location_id || '';
                    return {
                        id: job.job_id,
                        remote: true,
                        archived_reports: job.archived_reports || [],
                        job_type: job.operation,
                        dataset_key: `remote:${job.location_id}`,
                        display_dataset_key: location
                            ? `${serverName} · ${this.remoteDatasetName(location)}`
                            : (job.options?.source_root || job.location_id),
                        server_name: serverName,
                        status: job.status,
                        progress: Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0,
                        current,
                        total,
                        message: job.status === 'queued'
                            ? `Queued: ${job.queue_ahead || 0} task(s) ahead · ${({
                                data_path_conflict: 'waiting for data paths',
                                exclusive_job: 'waiting for exclusive job',
                                node_capacity: 'node slots full',
                                user_capacity: 'user slots full',
                                agent_capacity: 'waiting for Agent capacity'
                            })[job.queue_wait_reason] || 'waiting for scheduling'} · node ${job.node_slots?.used || 0}/${job.node_slots?.limit || 1}`
                            : (job.error || (job.status === 'done' ? 'Completed' : payload.message || lastEvent?.message || job.status)),
                        error: job.error || null,
                        output_root: outputRoot,
                        output_location_id: outputLocationId,
                        output_dataset_key: outputLocationId ? `remote:${outputLocationId}` : null,
                        output_dataset_name: outputLocation ? this.remoteDatasetName(outputLocation) : '',
                        output_viewer_url: result.viewer_url || outputLocation?.metadata?.viewer_url || null,
                        review_url: result.review_url || null,
                        result_summary: result.mcap_import || result.preprocess?.summary || (job.operation === 'caption.annotate' ? job.options : null),
                        viewer_url: result.viewer_url
                            || (job.operation === 'viewer.prepare' ? location?.metadata?.viewer_url : null)
                            || null,
                        logs: events.filter(event => this.showDetailedJobLogs || event.payload?.log_visible !== false).map(event => ({
                            time: event.created_at ? new Date(event.created_at).toLocaleTimeString() : '',
                            message: event.message || '',
                            payload: event.payload || {}
                        })),
                        created_at: job.created_at,
                        started_at: job.started_at,
                        finished_at: job.finished_at,
                        updated_at: job.updated_at,
                        requested_by: job.requested_by,
                        requested_by_username: job.requested_by_username,
                        control_job_id: job.job_id,
                        revision: job.revision,
                        available_actions: job.available_actions || [],
                        action_reasons: job.action_reasons || {},
                        elapsed_seconds: job.started_at
                            ? Math.max(0, (new Date(job.finished_at || Date.now()) - new Date(job.started_at)) / 1000)
                            : 0,
                        eta_seconds: null
                    };
                },
                mergeRemoteJobsIntoRuns() {
                    const localJobs = (this.jobs || []).filter(job => !job.remote);
                    const remoteRunJobs = (this.remoteJobs || [])
                        .filter(job => !String(job.operation || '').startsWith('local.request.'))
                        .map(job => this.remoteJobForRuns(job))
                        .filter(Boolean);
                    this.jobs = [...remoteRunJobs, ...localJobs].sort(
                        (a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))
                    );
                    if (!this.selectedJobId && this.jobs.length) this.selectedJobId = this.jobs[0].id;
                },
                remoteViewerPending(location) {
                    const job = this.latestRemoteViewerJob(location);
                    return Boolean(job && ['queued', 'running', 'cancel_requested', 'interrupted'].includes(job.status));
                },
                remoteViewerProgress(location) {
                    const job = this.latestRemoteViewerJob(location);
                    if (!job) return 0;
                    if (job.status === 'done') return 100;
                    const events = job.events || [];
                    const payload = job.progress || job.result?.progress || (events.length ? events[events.length - 1].payload || {} : {});
                    let progress = Number(payload.progress);
                    if (!Number.isFinite(progress) && Number(payload.total) > 0) {
                        progress = Number(payload.current || 0) / Number(payload.total) * 100;
                    }
                    return Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0;
                },
                remoteViewerMessage(location) {
                    const job = this.latestRemoteViewerJob(location);
                    if (!job) return '';
                    if (job.error) return job.error;
                    const events = job.events || [];
                    if (job.status === 'done') return 'Viewer cache ready';
                    if (job.progress?.message || job.result?.progress?.message) return job.progress?.message || job.result.progress.message;
                    if (events.length) return events[events.length - 1].message || job.status;
                    if (job.status === 'queued') return 'Waiting for Agent';
                    if (job.status === 'running') return 'Preparing viewer cache on Agent';
                    if (job.status === 'done') return 'Viewer cache ready';
                    return 'Viewer preparation failed';
                },
                remoteViewerActionText(location) {
                    const job = this.latestRemoteViewerJob(location);
                    if (this.remoteActionBusy && this.selectedRemoteLocation?.location_id === location.location_id) {
                        return 'Submitting…';
                    }
                    if (job?.status === 'queued') return 'Queued';
                    if (job?.status === 'running') return 'Preparing…';
                    if (job?.status === 'error') return 'Retry prepare';
                    return 'Prepare viewer';
                },
                async refreshRemoteViewerJob() {
                    if (!this.remoteViewerJob?.job_id) return;
                    try {
                        const data = await this.requestJson(
                            `/api/control/jobs/${this.remoteViewerJob.job_id}?summary=1`
                        );
                        this.mergeRemoteJob(data.job);
                        if (['done', 'error', 'cancelled'].includes(data.job.status)) {
                            await this.loadRemoteSources();
                            this.mergeRemoteJob(data.job);
                        }
                        this.remoteError = '';
                    } catch (err) {
                        this.remoteError = err.message;
                    }
                },
                scheduleRemoteRefresh() {
                    if (this.remoteRefreshTimer) window.clearTimeout(this.remoteRefreshTimer);
                    if (!this.remoteViewerJob || !['queued', 'running', 'cancel_requested', 'interrupted'].includes(this.remoteViewerJob.status)) {
                        this.remoteRefreshTimer = null;
                        return;
                    }
                    this.remoteRefreshTimer = window.setTimeout(async () => {
                        await this.refreshRemoteViewerJob();
                        this.scheduleRemoteRefresh();
                    }, 5000);
                },
                ownsRun(job) {
                    return !!(this.controlPlaneEnabled && this.controlPlaneUser?.user_id
                        && ['operator', 'data_manager', 'admin'].includes(this.controlPlaneUser.role)
                        && job?.requested_by === this.controlPlaneUser.user_id);
                },
                canControlRun(job, action) {
                    return this.ownsRun(job) && !!job?.control_job_id && (job.available_actions || []).includes(action);
                },
                runControlReason(job) {
                    if (!job || !this.controlPlaneEnabled) return '';
                    if (!this.ownsRun(job)) return this.adminModeEnabled
                        ? 'Manage other users’ tasks in Platform management.' : 'Only the submitter can control this task.';
                    const action = ['error', 'cancelled', 'interrupted'].includes(job.status) ? 'retry' : 'cancel';
                    return this.canControlRun(job, action) ? '' : (job.action_reasons?.[action] || 'This task does not support this control.');
                },
                async controlRun(job, action) {
                    if (this.jobControlBusy || !this.canControlRun(job, action)) return;
                    const id = job.control_job_id;
                    this.jobControlBusy = id; this.jobControlError = ''; this.jobControlNotice = '';
                    try {
                        const key = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
                        const data = await this.requestJson(`/api/jobs/${encodeURIComponent(id)}/${action}`, {
                            revision: job.revision, idempotency_key: key,
                        });
                        if (job.remote) this.mergeRemoteJob(data.job);
                        await this.loadJobs();
                        this.selectedJobId = job.id;
                        this.jobControlNotice = data.job.status === 'cancel_requested'
                            ? 'Stop requested. Waiting for the executor to confirm that all processes have exited.'
                            : action === 'retry' ? 'Task queued for retry.' : 'Queued task cancelled.';
                    } catch (error) { this.jobControlError = error.message; }
                    finally { this.jobControlBusy = ''; }
                },
                async loadJobs(refreshRemote = true) {
                    const remoteRefresh = refreshRemote && this.controlPlaneEnabled
                        ? this.loadRemoteJobs().then(() => {
                            if (this.remoteError.startsWith('Tasks:')) this.remoteError = '';
                        }).catch(err => { this.remoteError = `Tasks: ${err.message}`; })
                        : Promise.resolve();
                    const requestId = this.jobsLoadRequest = (this.jobsLoadRequest || 0) + 1;
                    const data = await this.requestJson('/api/jobs');
                    if (requestId !== this.jobsLoadRequest) return;
                    this.jobs = data.jobs || [];
                    this.mergeRemoteJobsIntoRuns();
                    this.syncActiveJob();
                    if (!this.selectedJobId) {
                        this.selectedJobId = (this.activeJob || this.jobs[0] || {}).id || '';
                    }
                    this.scrollLogsSoon();
                    if (this.isRunningJob(this.activeJob)) {
                        if (this.activeJob.remote) {
                            this.remoteViewerJob = this.remoteJobs.find(
                                job => job.job_id === this.activeJob.id
                            ) || this.remoteViewerJob;
                            this.scheduleRemoteRefresh();
                        } else {
                            this.pollJob(this.activeJob.id);
                        }
                    }
                    await remoteRefresh;
                    const requested = new URLSearchParams(location.search).get('job_id');
                    if (requested && !this._assistantJobOpened) {
                        this._assistantJobOpened = true;
                        const current = consoleReadScope(this, 'assistantJobLink');
                        try {
                            const result = await this.requestJson('/api/control/jobs/' + encodeURIComponent(requested));
                            if (!current()) return;
                            if (!this.remoteJobs.some(item => item.job_id === requested)) this.remoteJobs.push(result.job);
                            this.mergeRemoteJobsIntoRuns(); this.selectedJobId = requested; this.jobDrawerOpen = true;
                        } catch (error) {if (current()) this.jobControlError = error.message;}
                    }
                },
                async loadOperationLogs() {
                    const current = consoleReadScope(this, 'loadOperationLogs', 'dataset');
                    if (this.datasetAccessRestricted) { this.operationLogs = []; return; }
                    this.operationLogsLoading = true;
                    this.operationLogsError = '';
                    try {
                        const params = new URLSearchParams({limit: '200', summary: '1'});
                        const currentDatasetOnly = this.activePage !== 'runs' || this.runsFilter.scope === 'current';
                        if (currentDatasetOnly && this.selectedDatasetKey) params.set('dataset_key', this.selectedDatasetKey);
                        if (this.operationLogFilter.status) params.set('status', this.operationLogFilter.status);
                        if (this.operationLogFilter.operation) params.set('operation', this.operationLogFilter.operation);
                        const data = await this.requestJson(`/api/operation_log?${params.toString()}`);
                        if (!current()) return;
                        const previous = new Map(this.operationLogs.map(entry => [entry.event_id, entry]));
                        this.operationLogs = (data.operations || []).map(entry => Object.assign(previous.get(entry.event_id) || {}, entry));
                    } catch (error) {
                        if (current()) this.operationLogsError = error.message;
                    } finally {
                        if (current()) this.operationLogsLoading = false;
                    }
                },
                async loadOperationDetail(entry) {
                    if (!entry.summary || entry.fullDetail || entry.detailLoading) return;
                    const current = consoleReadScope(this, 'operationDetail:' + entry.event_id, 'dataset');
                    entry.detailLoading = true; entry.detailError = '';
                    try {
                        const params = new URLSearchParams({event_id: entry.event_id, limit: '1'});
                        const currentDatasetOnly = this.activePage !== 'runs' || this.runsFilter.scope === 'current';
                        if (currentDatasetOnly && this.selectedDatasetKey) params.set('dataset_key', this.selectedDatasetKey);
                        const result = await this.requestJson('/api/operation_log?' + params);
                        if (!current()) return;
                        if (!result.operations?.length) throw new Error('This event is no longer in retained history.');
                        entry.fullDetail = result.operations[0];
                    } catch (error) {if (current()) entry.detailError = error.message;}
                    finally {if (current()) entry.detailLoading = false;}
                },
                operationLabel(entry) {
                    return String(entry.operation || entry.op || 'unknown').replaceAll('_', ' ');
                },
                operationSummary(entry) {
                    const parts = [];
                    if (entry.phase) parts.push(entry.phase);
                    const episodes = Array.isArray(entry.episode_ids) ? entry.episode_ids : [];
                    if (episodes.length) parts.push(`episodes ${episodes.join(', ')}`);
                    const details = entry.details || {};
                    if (details.job_id) parts.push(`job ${details.job_id}`);
                    if (details.error) parts.push(String(details.error));
                    return parts.join(' · ');
                },
                operationStatusClass(status) {
                    if (status === 'success' || status === 'ok') return 'border-emerald-800 text-emerald-300';
                    if (status === 'failed' || status === 'error') return 'border-red-800 text-red-300';
                    if (status === 'accepted' || status === 'started') return 'border-sky-800 text-sky-300';
                    return 'border-slate-700 text-slate-400';
                },
                formatOperationTime(value) {
                    if (!value) return '';
                    const date = new Date(value);
                    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
                },
                jobBelongsToSelectedDataset(job) {
                    if (!job || !this.selectedDatasetKey) return false;
                    if (job.dataset_key === this.selectedDatasetKey) return true;
                    const related = Array.isArray(job.related_dataset_keys) ? job.related_dataset_keys : [];
                    return related.includes(this.selectedDatasetKey);
                },
                isRunningJob(job) {
                    return Boolean(job && !['done', 'error', 'cancelled', 'stopped'].includes(String(job.status || '')));
                },
                runningJobs() {
                    return this.jobs.filter(job => this.isRunningJob(job));
                },
                failedJobs() {
                    return this.jobs.filter(job => String(job.status || '') === 'error');
                },
                jobButtonSummary() {
                    const running = this.runningJobs();
                    if (running.length) {
                        const queued = running.filter(job => job.status === 'queued').length;
                        return queued === running.length ? `${queued} queued` : `${running.length} active`;
                    }
                    const failed = this.failedJobs();
                    if (failed.length) return `${failed.length} failed`;
                    return 'idle';
                },
                jobStateText(job) {
                    if (job.status === 'queued') return 'Queued';
                    if (job.status === 'done') return 'Completed';
                    if (job.status === 'error') return 'Failed';
                    return job.status === 'running' ? `Running · ${job.progress || 0}%` : job.status;
                },
                jobStatusClass(status) {
                    if (status === 'done') return 'border-emerald-800 text-emerald-300';
                    if (status === 'error') return 'border-red-800 text-red-300';
                    if (this.isRunningJob({status})) return 'border-sky-800 text-sky-300';
                    return 'border-slate-700 text-slate-400';
                },
                jobOperationLabel(job) {
                    const operation = String(job?.job_type || job?.operation || job?.id || 'job');
                    const labels = {
                        'viewer.prepare': 'Prepare viewer',
                        'caption.annotate': 'Temporal Caption',
                        'preprocess.standardize': 'Standardize dataset',
                        preprocess_standardize: 'Standardize dataset',
                    };
                    if (labels[operation]) return labels[operation];
                    return operation
                        .replace(/^preprocess[._]/, '')
                        .replace(/^mutation[._]/, '')
                        .replaceAll('_', ' ')
                        .replaceAll('.', ' ');
                },
                jobServerLabel(job) {
                    return job?.server_name || (job?.remote ? 'Agent' : 'Server A');
                },
                formatJobTime(value) {
                    if (value === null || value === undefined || value === '') return '-';
                    const normalized = typeof value === 'number' && value < 1000000000000
                        ? value * 1000
                        : value;
                    const date = new Date(normalized);
                    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
                },
                jobOutputLabel(job) {
                    if (!job) return '';
                    if (job.output_dataset_name) return job.output_dataset_name;
                    if (job.output_dataset_key) return job.output_dataset_key;
                    return job.output_root || '';
                },
                canUseJobOutput(job) {
                    return Boolean(job && job.status === 'done' && job.output_dataset_key);
                },
                jobViewerUrl(job) {
                    if (!job) return '';
                    if (job.output_viewer_url) return job.output_viewer_url;
                    if (job.output_dataset_key) {
                        if (job.remote && job.output_location_id) {
                            const location = this.remoteLocations.find(
                                item => item.location_id === job.output_location_id
                            );
                            return location?.metadata?.viewer_url || '';
                        }
                        const dataset = this.datasets.find(item => (
                            item.key === job.output_dataset_key
                            || item.repo_id === job.output_dataset_key
                            || item.root === job.output_root
                        ));
                        return dataset?.cache?.status === 'cached' ? (dataset.viewer_url || '') : '';
                    }
                    return job.viewer_url || '';
                },
                canPrepareJobOutput(job) {
                    if (!this.canUseJobOutput(job) || this.jobViewerUrl(job)) return false;
                    if (this.jobs.some(item => (
                        item.id !== job.id
                        && this.isRunningJob(item)
                        && item.dataset_key === job.output_dataset_key
                    ))) return false;
                    return job.remote ? this.canOperateRemote() : this.canAutoCatalog();
                },
                async prepareJobOutput(job) {
                    if (!this.canPrepareJobOutput(job)) return;
                    if (job.remote) {
                        let location = this.remoteLocations.find(
                            item => item.location_id === job.output_location_id
                        );
                        if (!location) {
                            await this.loadRemoteSources();
                            location = this.remoteLocations.find(
                                item => item.location_id === job.output_location_id
                            );
                        }
                        if (!location) return;
                        await this.useRemoteDataset(location);
                        const viewerJob = await this.prepareRemoteViewer(location);
                        if (viewerJob) {
                            this.selectedJobId = viewerJob.job_id;
                            this.storeJobId(viewerJob.job_id);
                        }
                        return;
                    }
                    await this.loadDatasets();
                    const dataset = this.datasets.find(item => (
                        item.key === job.output_dataset_key
                        || item.repo_id === job.output_dataset_key
                        || item.root === job.output_root
                    ));
                    if (!dataset) return;
                    await this.selectDataset(dataset);
                    const previousJobId = job.id;
                    this.selectedJobId = '';
                    await this.startPrecompute({
                        ...this.cacheOnlyOverrides(),
                        episodes: '',
                        prepare_videos: true,
                        prepare_csv: true,
                        overwrite: false,
                        overwrite_csv: false
                    });
                    if (!this.selectedJobId) this.selectedJobId = previousJobId;
                    this.storeJobId(this.selectedJobId);
                },
                async useJobOutput(job) {
                    if (!this.canUseJobOutput(job)) return;
                    if (job.remote) {
                        if (!job.output_location_id) return;
                        let location = this.remoteLocations.find(
                            item => item.location_id === job.output_location_id
                        );
                        if (!location) {
                            await this.loadRemoteSources();
                            location = this.remoteLocations.find(
                                item => item.location_id === job.output_location_id
                            );
                        }
                        if (!location) return;
                        await this.useRemoteDataset(location);
                    } else {
                        await this.loadDatasets();
                        const dataset = this.datasets.find(item => (
                            item.key === job.output_dataset_key
                            || item.repo_id === job.output_dataset_key
                            || item.root === job.output_root
                        ));
                        if (!dataset) return;
                        await this.selectDataset(dataset);
                    }
                    await this.selectPage('datasets');
                    this.jobDrawerOpen = false;
                },
                filteredJobs() {
                    return this.jobs.filter(job => {
                        if (this.runsFilter.scope === 'current' && !this.jobBelongsToSelectedDataset(job)) return false;
                        if (!this.runsFilter.status) return true;
                        if (this.runsFilter.status === 'running') return this.isRunningJob(job);
                        return String(job.status || '') === this.runsFilter.status;
                    });
                },
                selectedJob() {
                    const visible = this.filteredJobs();
                    return visible.find(job => job.id === this.selectedJobId) || visible[0] || null;
                },
                async selectJob(job, remember = true) {
                    if (!job?.id) return;
                    this.jobControlError = ''; this.jobControlNotice = '';
                    this.selectedJobId = job.id;
                    if (remember) this.storeJobId(job.id);
                    this.scrollLogsSoon();
                    if (!job.remote) {
                        if (this.selectedRemoteJobTimer) window.clearTimeout(this.selectedRemoteJobTimer);
                        this.selectedRemoteJobTimer = null;
                        return;
                    }
                    await this.refreshSelectedRemoteJob(job.id);
                },
                async refreshSelectedRemoteJob(jobId) {
                    if (!jobId || this.selectedJobId !== jobId) return;
                    try {
                        const data = await this.requestJson(`/api/control/jobs/${jobId}?summary=1`);
                        this.mergeRemoteJob(data.job);
                        this.remoteError = '';
                    } catch (err) {
                        this.remoteError = err.message;
                    }
                    this.scheduleSelectedRemoteJobRefresh(jobId);
                    this.scrollLogsSoon();
                },
                scheduleSelectedRemoteJobRefresh(jobId) {
                    if (this.selectedRemoteJobTimer) window.clearTimeout(this.selectedRemoteJobTimer);
                    this.selectedRemoteJobTimer = null;
                    const job = this.remoteJobs.find(item => item.job_id === jobId);
                    if (
                        this.selectedJobId !== jobId
                        || !job
                        || !['queued', 'running', 'cancel_requested', 'interrupted'].includes(job.status)
                    ) return;
                    this.selectedRemoteJobTimer = window.setTimeout(
                        () => this.refreshSelectedRemoteJob(jobId),
                        5000
                    );
                },
                selectFirstVisibleJob() {
                    const visible = this.filteredJobs();
                    if (!visible.some(job => job.id === this.selectedJobId)) {
                        const first = visible[0] || null;
                        if (first) this.selectJob(first);
                        else this.selectedJobId = '';
                    }
                    this.scrollLogsSoon();
                },
                openJobDrawer(currentOnly = false) {
                    this.runsFilter.scope = currentOnly && this.selectedDatasetKey ? 'current' : 'all';
                    this.runsFilter.status = '';
                    const visible = this.filteredJobs();
                    const current = visible.filter(job => this.jobBelongsToSelectedDataset(job));
                    const preferred = current.find(job => this.isRunningJob(job)) || current[0]
                        || visible.find(job => this.isRunningJob(job)) || visible[0] || null;
                    if (preferred) this.selectJob(preferred);
                    this.jobDrawerOpen = true;
                    this.scrollLogsSoon();
                },
                upsertJob(job) {
                    if (!job || !job.id) return;
                    const idx = this.jobs.findIndex(item => item.id === job.id);
                    if (idx >= 0) {
                        this.jobs.splice(idx, 1, job);
                    } else {
                        this.jobs.unshift(job);
                    }
                    if (!this.selectedJobId) this.selectedJobId = job.id;
                },
                syncActiveJob() {
                    if (!this.selectedDatasetKey) {
                        this.activeJob = null;
                        return;
                    }
                    this.activeJob = this.jobs.find(job => this.jobBelongsToSelectedDataset(job)) || null;
                    this.scrollLogsSoon();
                    if (this.isRunningJob(this.activeJob)) {
                        if (this.activeJob.remote) {
                            this.remoteViewerJob = this.remoteJobs.find(
                                job => job.job_id === this.activeJob.id
                            ) || this.remoteViewerJob;
                            this.scheduleRemoteRefresh();
                        } else {
                            this.pollJob(this.activeJob.id);
                        }
                    }
                },
                scrollLogsSoon() {
                    this.$nextTick(() => {
                        const box = this.$refs && this.$refs.jobLogBox;
                        if (box) box.scrollTop = box.scrollHeight;
                    });
                },
                async resumeJobPolling() {
                    const now = Date.now();
                    if (now - this.lastResumeAt < 500) return;
                    this.lastResumeAt = now;
                    try {
                        await this.loadJobs();
                    } catch (err) {
                        if (this.activeJob) {
                            this.activeJob = {...this.activeJob, message: err.message};
                        }
                    }
                },
                pollJob(jobId) {
                    if (!jobId) return;
                    if (this.pollJobId === jobId && this.pollTimer) return;
                    if (this.pollTimer) {
                        clearTimeout(this.pollTimer);
                        this.pollTimer = null;
                    }
                    this.pollJobId = jobId;
                    const schedule = (delay = 750) => {
                        if (this.pollJobId !== jobId) return;
                        this.pollTimer = setTimeout(tick, delay);
                    };
                    const tick = async () => {
                        if (this.pollJobId !== jobId) return;
                        if (document.hidden) {
                            schedule(2000);
                            return;
                        }
                        try {
                            const data = await this.requestJson(`/api/jobs/${jobId}`);
                            const keepActive = this.activeJob && this.activeJob.id === jobId;
                            this.upsertJob(data.job);
                            if (keepActive) this.activeJob = data.job;
                            else this.syncActiveJob();
                            this.scrollLogsSoon();
                            if (['done', 'error', 'cancelled'].includes(data.job.status)) {
                                clearTimeout(this.pollTimer);
                                this.pollTimer = null;
                                this.pollJobId = null;
                                await this.loadDatasets();
                                if (this.selectedDatasetKey) await this.loadDatasetDetails(this.selectedDatasetKey);
                                if (this.tabEnabled('versions')) await this.loadLifecycle();
                                await this.loadOperationLogs();
                            } else {
                                schedule(750);
                            }
                        } catch (err) {
                            if (this.activeJob && this.activeJob.id === jobId) {
                                this.activeJob = {...this.activeJob, message: err.message};
                            }
                            schedule(1500);
                        }
                    };
                    tick();
                },
                formatDuration(seconds) {
                    if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '-';
                    const total = Math.max(0, Math.round(Number(seconds)));
                    const hours = Math.floor(total / 3600);
                    const minutes = Math.floor((total % 3600) / 60);
                    const secs = total % 60;
                    if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
                    if (minutes > 0) return `${minutes}m ${String(secs).padStart(2, '0')}s`;
                    return `${secs}s`;
                },
    };
}
