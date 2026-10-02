(() => {
    "use strict";
    const root = document.querySelector('[data-cdek-sync]');
    if (!root) return;
    const toggle = root.querySelector('[data-cdek-toggle]');
    const panel = root.querySelector('#cdekSyncPanel');
    const run = root.querySelector('[data-cdek-run]');
    const feedback = root.querySelector('[data-cdek-feedback]');
    let timer, loading = false, running = false;
    const put = (key, value) => { root.querySelector('[data-cdek-' + key + ']').textContent = value; };
    function show(open) {
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        root.classList.toggle('is-open', open);
        clearTimeout(timer);
        if (open) refresh();
    }
    async function refresh(manual = false) {
        if (loading) return;
        loading = true;
        run.disabled = true;
        if (manual) feedback.textContent = 'Запускаем обновление…';
        try {
            const response = await fetch(root.dataset.url, {
                method: manual ? 'POST' : 'GET', credentials: 'same-origin',
                headers: manual ? {'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'} : {'Accept':'application/json'},
                body: manual ? new URLSearchParams({csrf_token: root.dataset.csrf}).toString() : undefined
            });
            if (response.redirected) throw new Error('Сессия завершена. Обновите страницу.');
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || 'Не удалось получить состояние СДЭК.');
            running = data.outcome === 'running';
            const count = data.counts.problems;
            root.dataset.syncState = running ? 'running' : !data.configured || data.outcome === 'error' ? 'error' : count || data.counts.stale || data.outcome === 'partial' ? 'attention' : data.outcome === 'unknown' ? 'unknown' : 'success';
            put('label', running ? 'Сверка СДЭК · обновление…' : 'Сверка СДЭК · ' + count + ' проблем');
            put('state', !data.configured ? 'API не подключён' : running ? 'Обновляется…' : data.outcome === 'error' ? 'Ошибка обновления' : data.outcome === 'partial' ? 'Частично обновлено' : data.outcome === 'unknown' ? 'Общая проверка ещё не выполнялась' : 'Обновление завершено');
            put('attempt', data.last_attempt_display || '—');
            put('success', 'Последняя успешная: ' + (data.last_success_display || '—') + ' МСК');
            put('problems', count);
            feedback.textContent = data.message || '';
            run.dataset.configured = String(data.configured);
        } catch (error) {
            feedback.textContent = error instanceof SyntaxError ? 'Не удалось получить состояние СДЭК.' : error.message;
            root.dataset.syncState = 'error';
        } finally {
            loading = false;
            run.disabled = running || run.dataset.configured === 'false';
            clearTimeout(timer);
            if (!panel.hidden) timer = setTimeout(refresh, running ? 2000 : 15000);
        }
    }
    toggle.addEventListener('click', () => show(panel.hidden));
    run.addEventListener('click', () => refresh(true));
    root.querySelector('[data-cdek-refresh]').addEventListener('click', () => refresh());
    document.addEventListener('click', event => { if (!root.contains(event.target)) show(false); });
    root.addEventListener('keydown', event => {
        if (event.key === 'Escape') { show(false); toggle.focus(); }
    });
})();
