/* Shared by every named-environment page, including the login page. */
(() => {
    const originalFetch = window.fetch.bind(window);
    let observedUser;
    window.fetch = async (input, init = {}) => {
        const url = new URL(input instanceof Request ? input.url : input, location.href);
        const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const status = await originalFetch('/api/auth/status', {cache: 'no-store'});
            if (!status.ok) throw new Error('无法读取登录状态');
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
        label.textContent = `${environment === 'dev' ? '开发环境 · 测试数据' : '生产环境'} · ${release}`;
        bar.append(label);
        document.body.prepend(bar);
        try {
            const auth = await (await originalFetch('/api/auth/status', {cache: 'no-store'})).json();
            if (!auth.user) return;
            observedUser = auth.user.user_id;
            const user = document.createElement('span');
            user.textContent = `${auth.user.display_name} (${auth.user.role})`;
            bar.append(user);
            if (environment !== 'dev' || !auth.dev_role_switch) return;
            const response = await originalFetch('/api/dev/role-session');
            if (!response.ok) return;
            const state = await response.json();
            const select = document.createElement('select');
            select.setAttribute('aria-label', '开发测试身份');
            const names = {admin: '返回管理员', operator_a: '操作员 A', operator_b: '操作员 B', viewer: '只读用户'};
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
            message.textContent = '登录状态暂不可用';
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
