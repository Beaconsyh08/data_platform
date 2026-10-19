// The embedded workspace and /pipelines use the same editor and execution interfaces.
function consolePipelineState() {
    return {
                syncPipelineWorkspace(host) {
                    const active = this.activeTab === 'pipelines' && this.activePage === 'preprocessing';
                    const locationId = this.selectedRemoteLocation?.location_id || this.selectedDataset?.remote_location_id || '';
                    const identity = `${this.controlPlaneUser?.user_id || ''}:${this.controlPlaneUser?.role || ''}:${this.selectedDatasetKey || ''}:${locationId}`;
                    if (host._pipelineIdentity === identity && host._pipelineActive === active) return;
                    host._pipelineEditor?.destroy();
                    host._pipelineIdentity = identity;
                    host._pipelineActive = active;
                    const root = host.shadowRoot || host.attachShadow({mode: 'open'});
                    root.replaceChildren();
                    if (!active) return;
                    root.append(document.getElementById('pipeline-workspace-template').content.cloneNode(true));
                    host._pipelineEditor = mountPipelineEditor(root, {embedded: true, locationId});
                },
    };
}
