(() => {
    const root = document.querySelector('[data-orders-sync]');
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = '1';
    const pending = new Set();
    const failures = new Map();
    const timestamps = new Map();
    const rows = Object.fromEntries([...root.querySelectorAll('[data-sync-row]')].map(row => [row.dataset.syncRow, row]));
    const labels = {success: 'Актуально', running: 'Обновляется…', error: 'Ошибка', attention: 'Требует внимания'};
    const toggle = root.querySelector('[data-orders-sync-toggle]');
    const panel = root.querySelector('[data-orders-sync-panel]');
    function setPanelOpen(open, focus = false) {
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        root.classList.toggle('is-open', open);
        if (focus) toggle.focus();
    }
    toggle.addEventListener('click', () => setPanelOpen(panel.hidden));
    function renderAggregate() {
        const states = Object.values(rows).map(row => row.dataset.noData === 'true' && row.dataset.state === 'attention' ? 'unknown' : row.dataset.state);
        // Aggregate existing source states; absence of data alone is neutral.
        const state = ['error', 'running', 'attention', 'unknown'].find(value => states.includes(value)) || 'success';
        root.dataset.syncState = state;
        root.querySelector('[data-orders-sync-label]').textContent = 'Синхронизация · ' + (labels[state] || 'Нет данных');
    }
    const format = date => new Intl.DateTimeFormat('ru-RU', {dateStyle: 'short', timeStyle: 'short'}).format(date);
    function render(source, data) {
        const row = rows[source];
        const date = data.last_success_at ? new Date(typeof data.last_success_at === 'number' ? data.last_success_at * 1000 : data.last_success_at) : null;
        const valid = date && !Number.isNaN(date.getTime());
        if (valid) timestamps.set(source, date);
        const stale = !valid || Date.now() - date.getTime() > 15 * 60000;
        const state = pending.has(source) || data.outcome === 'running' ? 'running'
            : failures.has(source) || data.outcome === 'error' ? 'error'
            : stale || data.attention || data.pending?.length || data.outcome === 'partial' || ['partial', 'error'].includes(data.full_outcome) ? 'attention' : 'success';
        row.dataset.noData = String(!valid && !data.attention && !data.pending?.length && data.outcome !== 'partial' && !['partial', 'error'].includes(data.full_outcome));
        row.dataset.state = state;
        renderAggregate();
        row.querySelector('[data-sync-state]').textContent = labels[state];
        row.querySelector('[data-orders-sync-time]').textContent = valid ? (date.toDateString() === new Date().toDateString() ? 'Сегодня, ' + date.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'}) : format(date)) : 'Нет данных';
        root.querySelector('[data-sync-latest]').textContent = timestamps.size ? format(new Date(Math.max(...timestamps.values()))) : 'Нет данных';
        if (source === 'tictactoy') root.querySelector('[data-ttt-diagnostic]').textContent = 'TicTacToy: ' + (failures.get(source) || data.error || (valid ? 'Последняя успешная синхронизация: ' + format(date) : 'Нет подтверждённых данных о синхронизации.'));
    }
    async function request(source, method = 'GET') {
        const response = await fetch(`/api/orders/${source}/sync`, {method, credentials: 'same-origin', headers: {Accept: 'application/json', 'X-CSRF-Token': root.dataset.csrf}});
        const data = await response.json().catch(() => null);
        if (!response.ok || !data?.ok) throw new Error(data?.error?.message || data?.result?.error || `Не удалось обновить ${source === 'tictactoy' ? 'TicTacToy' : 'Wildberries'} (HTTP ${response.status})`);
        return data.result;
    }
    async function loadTtt() {
        if (pending.has('tictactoy')) return;
        try { render('tictactoy', await request('tictactoy')); }
        catch (error) { render('tictactoy', {outcome: 'error', error: error.message}); }
    }
    document.addEventListener('orders:wb-diagnostics', event => render('wildberries', event.detail));
    function updateButtons() {
        root.querySelectorAll('[data-sync-source]').forEach(button => {
            button.disabled = button.dataset.syncSource === 'all' ? pending.size > 0 : pending.has(button.dataset.syncSource);
            button.setAttribute('aria-busy', String(button.disabled));
        });
    }
    async function sync(source) {
        if (pending.has(source)) return;
        pending.add(source); failures.delete(source); updateButtons();
        rows[source].dataset.state = 'running';
        renderAggregate();
        rows[source].querySelector('[data-sync-state]').textContent = labels.running;
        let result;
        try {
            result = await request(source, 'POST');
            if (result.outcome === 'partial') window.VechasuNotify?.warning(result.notification?.message || 'Wildberries обновлён частично: проверьте диагностику.');
            else window.VechasuNotify?.success(`${source === 'tictactoy' ? 'TicTacToy' : 'Wildberries'}: заказы обновлены`);
        } catch (error) {
            failures.set(source, error.message);
            window.VechasuNotify?.error(error.message);
            if (source === 'wildberries') root.querySelector('[data-wb-recovery-message]').textContent = error.message;
        } finally {
            pending.delete(source); updateButtons();
            render(source, result || {outcome: 'error', last_success_at: timestamps.get(source)?.toISOString()});
            if (source === 'wildberries') document.dispatchEvent(new Event('orders:wb-refresh'));
        }
    }
    root.querySelectorAll('[data-sync-source]').forEach(button => button.addEventListener('click', async () => {
        if (button.disabled) return;
        const sources = button.dataset.syncSource === 'all' ? ['tictactoy', 'wildberries'] : [button.dataset.syncSource];
        await Promise.allSettled(sources.map(sync));
        document.dispatchEvent(new Event('orders:sync-complete'));
    }));
    root.querySelectorAll('[data-sync-details]').forEach(button => button.addEventListener('click', () => {
        const panel = root.querySelector('[data-sync-diagnostics]');
        panel.hidden = !panel.hidden;
        root.querySelectorAll('[data-sync-details]').forEach(trigger => trigger.setAttribute('aria-expanded', String(!panel.hidden)));
        if (!panel.hidden && button.closest('[data-sync-row="wildberries"]')) panel.querySelector('[data-wb-recovery]').open = true;
    }));
    document.addEventListener('click', event => {
        if (!root.contains(event.target)) setPanelOpen(false);
        const statuses = document.querySelector('[data-status-more]');
        if (statuses && !statuses.contains(event.target)) statuses.open = false;
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        if (!panel.hidden) {
            root.querySelector('[data-sync-diagnostics]').hidden = true;
            root.querySelectorAll('[data-sync-details]').forEach(button => button.setAttribute('aria-expanded', 'false'));
            setPanelOpen(false, true);
        }
        const statuses = document.querySelector('[data-status-more]');
        if (statuses?.open) {
            statuses.open = false;
            statuses.querySelector('summary').focus();
        }
    });
    loadTtt();
    window.setInterval(() => {if (!document.hidden) loadTtt();}, 60000);

    function updateStatusLabel() {
        const statuses = document.querySelector('[data-status-more]');
        if (!statuses) return;
        const selected = statuses.querySelector('[aria-pressed="true"]');
        if (!selected) return; // Keep the server label for a status with no matches.
        const count = selected.querySelector('[data-order-status-count]');
        const name = selected.firstChild.textContent.trim();
        statuses.querySelector('[data-status-label]').textContent = 'Статус: ' + name + (count ? ' · ' + count.textContent : '');
    }
    document.addEventListener('orders:status-selected', updateStatusLabel);
    document.addEventListener('orders:filters-updated', updateStatusLabel);
})();
