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
            user.textContent = `${auth.user.display_name} (${auth.user.role})`;
            bar.append(user);
            if (environment === 'dev') {
                const comparison = document.createElement('span');
                comparison.textContent = 'Checking production release…';
                bar.append(comparison);
                const button = document.createElement('button');
                button.textContent = 'Deploy to production';
                button.disabled = true;
                if (auth.user.role === 'admin' && !auth.user.original_actor) bar.append(button);
                let snapshot;
                const refresh = async () => {
                    try {
                        const response = await originalFetch('/api/dev/production', {cache: 'no-store'});
                        snapshot = await response.json();
                        if (!response.ok) throw new Error(snapshot.error);
                        comparison.textContent = `Production: ${snapshot.prod.release || snapshot.prod.environment || 'Unknown'} · ${snapshot.comparison === 'same' ? 'Same release' : (snapshot.comparison === 'different' ? 'Different releases' : 'Comparison unavailable')}`;
                        comparison.title = 'Compares deployed code releases. Databases and datasets remain separate.';
                        button.disabled = !snapshot.can_promote;
                        button.title = snapshot.reason || 'Deploy this tested release to production';
                        if (snapshot.reason) comparison.textContent += ` · ${snapshot.reason}`;
                    } catch (error) {
                        comparison.textContent = error.message || 'Production status unavailable';
                        button.disabled = true;
                    }
                };
                button.addEventListener('click', async () => {
                    if (!snapshot?.can_promote) return;
                    if (!confirm(`Deploy ${snapshot.dev.release} to production? Production will briefly enter maintenance. Production databases and datasets will be preserved.`)) return;
                    button.disabled = true;
                    try {
                        const response = await window.fetch('/api/dev/production', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({confirm: true, revision: snapshot.revision}),
                        });
                        const result = await response.json();
                        if (!response.ok) throw new Error(result.error);
                        comparison.textContent = `Deploying ${result.release} to production…`;
                    } catch (error) { alert(error.message); }
                    await refresh();
                });
                await refresh();
                setInterval(refresh, 15000);
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
