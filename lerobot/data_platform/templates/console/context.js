// Each request retains the account/environment and, when relevant, its selected input.
function consoleReadScope(app, channel, scope = 'account') {
    const versions = app._consoleReadVersions ||= {};
    const version = channel ? (versions[channel] = (versions[channel] || 0) + 1) : null;
    const identity = () => JSON.stringify([
        globalThis.location?.origin || '', app.controlPlaneUser?.user_id || '',
        app.controlPlaneUser?.role || '', app.adminModeEnabled || false,
        ...(scope === 'dataset' ? [app.selectionToken, app.selectedDatasetKey,
            app.selectedRemoteLocation?.location_id] : []),
        ...(scope === 'browse' ? [app.datasetBrowseRevision, app.datasetServerFilter] : []),
    ]);
    const original = identity();
    return () => original === identity() && (!channel || versions[channel] === version);
}
