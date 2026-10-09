/* Optional, after-load in-app delivery. Failures never interrupt the ERP shell. */
(() => {
    "use strict";
    if (window.__tasksModuleNotifications) return;
    window.__tasksModuleNotifications = true;
    const boot = window.ERP_TASKS_OPTIONAL;
    if (!boot) return;
    let busy = false, lastAttempt = 0;
    async function badge(signal) {
        const response = await fetch('/api/v1/tasks-module/inbox/badge', {
            credentials: 'same-origin', signal, headers: {'X-Vechasu-Notify': 'off'},
        });
        if (!response.ok) throw new Error('Badge unavailable');
        const counts = (await response.json()).data;
        document.querySelectorAll('[data-tasks-module-badge]').forEach(item => {
            const micro = item.dataset && item.dataset.tasksModuleBadge === 'micro';
            const count = micro ? counts.micro : (counts.normal === undefined ? counts.count : counts.normal);
            item.textContent = Number.isSafeInteger(count) && count > 0 ? (micro ? `⚡${count}` : String(count)) : '';
            if (item.setAttribute) item.setAttribute('aria-label', (micro ? 'Невыполненные микрозадачи: ' : 'Обычные входящие: ') + (count || 0));
            item.hidden = !item.textContent;
        });
    }
    async function claim() {
        if (busy || document.visibilityState !== "visible" || Date.now() - lastAttempt < 30000) return;
        busy = true;
        lastAttempt = Date.now();
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 2500);
        try {
            await badge(controller.signal).catch(() => {
                document.querySelectorAll('[data-tasks-module-badge]').forEach(item => { item.hidden = true; });
            });
            if (!window.VechasuNotify || !window.VechasuNotify.info) return;
            const response = await fetch("/api/v1/tasks-module/notifications/claim", {
                method: "POST", credentials: "same-origin", signal: controller.signal,
                headers: {"Content-Type": "application/json", "X-CSRF-Token": boot.csrf, "X-Vechasu-Notify": "off"},
                body: JSON.stringify({limit: 3}),
            });
            if (!response.ok) return;
            const payload = await response.json();
            const items = payload.data && payload.data.items;
            if (!Array.isArray(items) || !items.length) return;
            let directory = [];
            try {
                const names = await fetch("/api/v1/tasks-module/directory", {
                    credentials: "same-origin", signal: controller.signal, headers: {"X-Vechasu-Notify": "off"},
                });
                if (names.ok) directory = (await names.json()).data.items || [];
            } catch (_) { /* A name is optional; the assignment signal is still useful. */ }
            items.forEach(item => {
                if (!Number.isSafeInteger(item.task_id) || item.task_id <= 0) return;
                const actor = directory.find(user => user.id === item.actor_id);
                window.VechasuNotify.info(item.task_type === "micro" ? "⚡ Микрозадача · 24 часа" : "Новое поручение", {
                    detail: (item.source === "cdek" ? "Автоматически · СДЭК · " : actor ? "От " + actor.name + " · " : "") + String(item.title || ""),
                    action: {label: "Ознакомиться", href: "/app/tasks-module?view=inbox&preview=" + item.task_id},
                });
            });
        } catch (_) { /* Optional Tasks must not emit an ERP-wide error. */ }
        finally { window.clearTimeout(timeout); busy = false; }
    }
    document.addEventListener("visibilitychange", () => { claim().catch(() => {}); });
    claim().catch(() => {});
})();
