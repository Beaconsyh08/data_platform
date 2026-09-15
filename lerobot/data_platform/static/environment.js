/* Shared by every named-environment page, including the login page. */
(() => {
    const originalFetch = window.fetch.bind(window);
    let observedUser;
    window.fetch = async (input, init = {}) => {
        const url = new URL(input instanceof Request ? input.url : input, location.href);
        const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const status = await originalFetch('/api/auth/status', {cache: 'no-store'});
            if (!status.ok) throw new Error('Unable to read session status');
            const auth = await status.json();
            const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
            headers.set('X-Data-Platform-CSRF', auth.csrf_token);
            init = {...init, headers};
        }
        return originalFetch(input, init);
    };
    document.addEventListener('DOMContentLoaded', async () => {
        const environment = document.querySelector('meta[name="data-platform-environment"]').content;
        const release = document.querySelector('meta[name="data-platform-release"]').content;
        const bar = document.createElement('div');
        bar.id = 'dp-environment-bar';
        const label = document.createElement('strong');
        label.textContent = `${environment === 'dev' ? 'Development · Test data' : 'Production'} · ${release}`;
        bar.append(label);
        document.body.prepend(bar);
        try {
            const auth = await (await originalFetch('/api/auth/status', {cache: 'no-store'})).json();
            if (!auth.user) return;
            observedUser = auth.user.user_id;
            const user = document.createElement('span');
            user.textContent = `${auth.user.username} (${auth.user.role})`;
            bar.append(user);
            if (environment === 'dev') {
                const comparison = document.createElement('span');
                comparison.textContent = 'Checking production release…';
                bar.append(comparison);
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'dp-production-deploy';
                button.textContent = 'Deploy to production';
                button.disabled = true;
                if (auth.user.role === 'admin' && !auth.user.original_actor) bar.append(button);
                const approveButton = document.createElement('button');
                approveButton.type = 'button';
                approveButton.className = 'dp-production-deploy';
                approveButton.textContent = 'Review and approve';
                approveButton.disabled = true;
                const dialog = document.createElement('dialog');
                dialog.className = 'dp-acceptance-dialog';
                dialog.setAttribute('aria-labelledby', 'dp-acceptance-title');
                dialog.innerHTML = `<h2 id="dp-acceptance-title">Release acceptance</h2>
                    <p data-release></p><p data-checks role="status">Checking development services and Agents…</p>
                    <p>Confirm each scenario only after completing it on this release. Approval does not deploy to production.</p>
                    <form><fieldset><legend>Manual acceptance</legend>
                    <label><input type="checkbox" name="role_permissions"> Role permissions and account isolation work correctly</label>
                    <label><input type="checkbox" name="environment_isolation"> Development and production data remain isolated</label>
                    <label><input type="checkbox" name="local_job"> A representative local job completed successfully</label>
                    <label><input type="checkbox" name="remote_viewer"> Remote dataset viewing works</label>
                    <label><input type="checkbox" name="remote_preprocess"> Remote preprocessing completed successfully</label>
                    <label><input type="checkbox" name="rollback_drill"> The rollback procedure has been tested</label>
                    </fieldset><div class="dp-acceptance-actions"><button type="button" data-close>Cancel</button>
                    <button type="submit" disabled>Approve this release</button></div></form>`;
                const form = dialog.querySelector('form');
                const submit = dialog.querySelector('[type="submit"]');
                const checks = dialog.querySelector('[data-checks]');
                let review, ready = false, approving = false;
                const syncSubmit = () => {
                    submit.disabled = approving || !ready || ![...form.querySelectorAll('input')].every(input => input.checked);
                };
                form.addEventListener('change', syncSubmit);
                dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
                dialog.addEventListener('cancel', event => { if (approving) event.preventDefault(); });
                dialog.addEventListener('close', () => { review = null; ready = false; form.reset(); syncSubmit(); });
                if (auth.user.role === 'admin' && !auth.user.original_actor) {
                    bar.insertBefore(approveButton, button);
                    document.body.append(dialog);
                }
                approveButton.addEventListener('click', async () => {
                    if (!snapshot?.can_approve) return;
                    review = {revision: snapshot.revision, release: snapshot.dev.release};
                    const selected = review;
                    ready = false; form.reset(); syncSubmit();
                    dialog.querySelector('[data-release]').textContent = `Release: ${review.release}`;
                    checks.textContent = 'Checking development services, environment identity, Agent versions and heartbeats…';
                    dialog.showModal();
                    try {
                        const response = await originalFetch(`/api/dev/acceptance?revision=${encodeURIComponent(review.revision)}`, {cache: 'no-store'});
                        const result = await response.json();
                        if (!response.ok) throw new Error(result.error);
                        if (review !== selected) return;
                        ready = true;
                        checks.textContent = 'Automatic checks passed. Confirm the completed manual scenarios below.';
                    } catch (error) { if (review === selected) checks.textContent = error.message; }
                    syncSubmit();
                });
                form.addEventListener('submit', async event => {
                    event.preventDefault();
                    syncSubmit();
                    if (submit.disabled || !review) return;
                    approving = true; syncSubmit();
                    dialog.querySelector('[data-close]').disabled = true;
                    try {
                        const evidence = {release: review.release};
                        form.querySelectorAll('input').forEach(input => { evidence[input.name] = input.checked; });
                        const response = await window.fetch('/api/dev/acceptance', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({revision: review.revision, confirm: true, evidence}),
                        });
                        const result = await response.json();
                        if (!response.ok) throw new Error(result.error);
                        dialog.close();
                        await refresh();
                    } catch (error) { ready = false; checks.textContent = `${error.message} Close and reopen this review to retry.`; }
                    finally { approving = false; dialog.querySelector('[data-close]').disabled = false; syncSubmit(); }
                });
                const progressButton = document.createElement('button');
                progressButton.type = 'button'; progressButton.className = 'dp-production-deploy';
                progressButton.textContent = 'Deployment progress'; progressButton.hidden = true;
                const progressDialog = document.createElement('dialog');
                progressDialog.className = 'dp-acceptance-dialog dp-deployment-dialog';
                progressDialog.setAttribute('aria-labelledby', 'dp-deployment-title');
                progressDialog.innerHTML = `<h2 id="dp-deployment-title">Production deployment</h2>
                    <p data-deploy-release></p><p data-deploy-status role="status" aria-live="polite"></p>
                    <progress aria-label="Deployment in progress"></progress><p data-deploy-time></p>
                    <ol data-deploy-steps></ol><p data-deploy-connection></p>
                    <p>Closing this window does not stop the deployment.</p><button type="button" data-deploy-close>Hide</button>`;
                bar.append(progressButton); document.body.append(progressDialog);
                let trackedDeployment, hiddenDeployment, completedDeployment, latestProgress, suppressedProgressId;
                const rememberDeployment = value => {
                    try { if (value) window.sessionStorage?.setItem('dp-production-deployment', value);
                          else window.sessionStorage?.removeItem('dp-production-deployment'); } catch (_) {}
                };
                try { trackedDeployment = window.sessionStorage?.getItem('dp-production-deployment'); } catch (_) {}
                const showProgress = () => { if (!progressDialog.open) progressDialog.showModal(); };
                progressButton.addEventListener('click', () => { suppressedProgressId = null; renderProgress(snapshot?.progress); showProgress(); });
                progressDialog.querySelector('[data-deploy-close]').addEventListener('click', () => progressDialog.close());
                progressDialog.addEventListener('close', () => { hiddenDeployment = latestProgress?.id; });
                const renderProgress = progress => {
                    if (!progress || progress.id === suppressedProgressId) return;
                    latestProgress = progress;
                    const terminal = ['installed', 'failed', 'server-installed'].includes(progress.status);
                    progressButton.hidden = false;
                    progressDialog.querySelector('[data-deploy-release]').textContent = `Release: ${progress.release}`;
                    progressDialog.querySelector('[data-deploy-status]').textContent = progress.message;
                    progressDialog.querySelector('[data-deploy-status]').dataset.status = progress.status;
                    progressDialog.querySelector('progress').hidden = terminal;
                    const elapsed = Math.max(0, Math.floor((progress.finished_at || (terminal ? progress.updated_at : Date.now() / 1000)) - progress.started_at));
                    progressDialog.querySelector('[data-deploy-time]').textContent = `${terminal && !progress.finished_at ? 'Last recorded elapsed' : 'Elapsed'}: ${Math.floor(elapsed / 60)}m ${elapsed % 60}s · Updated: ${new Date(progress.updated_at * 1000).toLocaleTimeString()}`;
                    const list = progressDialog.querySelector('[data-deploy-steps]');
                    list.replaceChildren();
                    for (const step of progress.steps || []) {
                        const row = document.createElement('li');
                        row.textContent = `${new Date(step.at * 1000).toLocaleTimeString()} — ${step.message}`;
                        list.append(row);
                    }
                    progressDialog.querySelector('[data-deploy-connection]').textContent = terminal ? '' : 'Updates automatically every 3 seconds.';
                    progressDialog.querySelector('[data-deploy-close]').textContent = terminal ? 'Close' : 'Hide';
                    if (!terminal) {
                        trackedDeployment = progress.id; rememberDeployment(progress.id);
                        if (hiddenDeployment !== progress.id) showProgress();
                    } else if (trackedDeployment === progress.id && completedDeployment !== progress.id) {
                        completedDeployment = progress.id; rememberDeployment(null); showProgress();
                    }
                };
                let snapshot, refreshing = false, submittingDeployment = false;
                const refresh = async () => {
                    if (refreshing) return;
                    refreshing = true;
                    try {
                        const response = await originalFetch('/api/dev/production', {cache: 'no-store'});
                        snapshot = await response.json();
                        if (!response.ok) throw new Error(snapshot.error);
                        comparison.textContent = `Production: ${snapshot.prod.release || snapshot.prod.environment || 'Unknown'} · ${snapshot.comparison === 'same' ? 'Same release' : (snapshot.comparison === 'different' ? 'Different releases' : 'Comparison unavailable')}`;
                        comparison.title = 'Compares deployed code releases. Databases and datasets remain separate.';
                        renderProgress(snapshot.progress);
                        button.disabled = submittingDeployment || !snapshot.can_promote;
                        approveButton.disabled = !snapshot.can_approve || approving;
                        approveButton.textContent = snapshot.approved ? 'Release approved' : 'Review and approve';
                        approveButton.title = snapshot.approved ? 'This release has been approved' : (snapshot.can_approve ? 'Review acceptance checks for this release' : snapshot.reason || 'Acceptance unavailable');
                        if (review && (review.revision !== snapshot.revision || !snapshot.can_approve) && !approving) {
                            ready = false; syncSubmit();
                            checks.textContent = 'Release or availability changed. Close and reopen this review.';
                        }
                        button.title = snapshot.reason || 'Deploy this tested release to production';
                        if (snapshot.reason) comparison.textContent += ` · ${snapshot.reason}`;
                    } catch (error) {
                        comparison.textContent = error.message || 'Production status unavailable';
                        button.disabled = true;
                        approveButton.disabled = true;
                        if (progressDialog.open) progressDialog.querySelector('[data-deploy-connection]').textContent = 'Connection interrupted; retrying automatically. Deployment may still be running.';
                    } finally { refreshing = false; }
                };
                button.addEventListener('click', async () => {
                    if (submittingDeployment || !snapshot?.can_promote) return;
                    if (!confirm(`Deploy ${snapshot.dev.release} to production? Production will briefly enter maintenance. Production databases and datasets will be preserved.`)) return;
                    button.disabled = true;
                    submittingDeployment = true;
                    suppressedProgressId = latestProgress?.id;
                    progressDialog.querySelector('[data-deploy-status]').textContent = 'Submitting deployment request…';
                    progressDialog.querySelector('[data-deploy-release]').textContent = `Release: ${snapshot.dev.release}`;
                    progressDialog.querySelector('[data-deploy-steps]').replaceChildren();
                    progressDialog.querySelector('progress').hidden = false;
                    showProgress();
                    try {
                        const response = await window.fetch('/api/dev/production', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({confirm: true, revision: snapshot.revision}),
                        });
                        const result = await response.json();
                        if (!response.ok) throw new Error(result.error);
                        trackedDeployment = result.deployment_id;
                        rememberDeployment(trackedDeployment);
                        comparison.textContent = `Deploying ${result.release} to production…`;
                        progressDialog.querySelector('[data-deploy-status]').textContent = 'Request accepted. Waiting for deployment progress…';
                    } catch (error) { progressDialog.querySelector('[data-deploy-status]').textContent = error.message;
                        progressDialog.querySelector('progress').hidden = true;
                    } finally { submittingDeployment = false; }
                    await refresh();
                });
                await refresh();
                setInterval(refresh, 3000);
            }
            if (environment !== 'dev' || !auth.dev_role_switch) return;
            const response = await originalFetch('/api/dev/role-session');
            if (!response.ok) return;
            const state = await response.json();
            const select = document.createElement('select');
            select.setAttribute('aria-label', 'Development test role');
            const names = {admin: 'Administrator', operator_a: 'Operator A', operator_b: 'Operator B', viewer: 'Viewer'};
            for (const choice of state.choices) {
                const option = document.createElement('option');
                option.value = choice;
                option.textContent = names[choice];
                select.append(option);
            }
            select.value = state.user.original_actor ? state.user.username.replace('dev_test_', '') : 'admin';
            select.addEventListener('change', async () => {
                select.disabled = true;
                try {
                    const result = await window.fetch('/api/dev/role-session', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({identity: select.value}),
                    });
                    if (!result.ok) throw new Error((await result.json()).error);
                    localStorage.setItem('dp-dev-role-changed', String(Date.now()));
                    location.reload();
                } catch (error) { alert(error.message); select.disabled = false; }
            });
            bar.append(select);
        } catch (error) {
            const message = document.createElement('span');
            message.textContent = 'Session status unavailable';
            bar.append(message);
        }
    });
    window.addEventListener('storage', event => {
        if (event.key === 'dp-dev-role-changed') location.reload();
    });
    window.addEventListener('focus', async () => {
        if (!observedUser) return;
        try {
            const auth = await (await originalFetch('/api/auth/status', {cache: 'no-store'})).json();
            if (auth.user?.user_id !== observedUser) location.reload();
        } catch (_) { /* A later request will report connectivity errors. */ }
    });
})();
