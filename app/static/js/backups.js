(function () {
    "use strict";

    const bootstrap = window.ERP_BACKUPS_BOOTSTRAP || {};
    const root = document.querySelector("[data-backup-root]");
    if (!root) return;

    const q = (selector) => root.querySelector(selector);
    const text = (selector, value) => { const node = q(selector); if (node) node.textContent = value; };
    const unknown = "Не удалось определить";
    const typeLabels = { automatic: "Ежедневный", daily: "Ежедневный", weekly: "Недельный", monthly: "Месячный", manual: "Ручной", safety: "Safety" };
    const reasonLabels = { scheduled: "По расписанию", manual: "Вручную", pre_restore: "Перед восстановлением", pre_import: "Перед импортом", pre_update: "Перед обновлением", pre_price_change: "Перед изменением цен", pre_stock_change: "Перед изменением остатков", pre_migration: "Перед миграцией" };
    const statusLabels = { not_checked: "Не проверен", verified: "Проверен", error: "Повреждён", creating: "Создаётся", verifying: "Проверяется" };
    let watchedOperationId = bootstrap.status && bootstrap.status.operation && bootstrap.status.operation.active
        ? bootstrap.status.operation.id
        : null;
    let refreshTimer = null;
    let pendingRecovery = null;
    const stageLabels = {
        pending: "Ожидает", preflight: "Предварительная проверка",
        safety_backup: "Safety backup",
        staging_restore: "Staging", staging_check: "Проверка staging",
        maintenance: "Режим обслуживания", production_restore: "Восстановление",
        service_restart: "Перезапуск", health_check: "Проверка ERP",
        completed: "Готово", failed: "Ошибка", critical: "Критическая ошибка",
    };

    function bytes(value) {
        if (!Number.isFinite(value)) return unknown;
        const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
        let amount = value;
        let index = 0;
        while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
        return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
    }

    function dateParts(value) {
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return { date: "—", time: "—", full: value || "—" };
        const source = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
        if (source) {
            const date = `${source[3]}.${source[2]}.${source[1]}`;
            const time = `${source[4]}:${source[5]}`;
            return { date, time, full: `${date}, ${time}` };
        }
        return {
            date: parsed.toLocaleDateString("ru-RU"),
            time: parsed.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }),
            full: parsed.toLocaleString("ru-RU"),
        };
    }

    function age(value) {
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return "—";
        const seconds = Math.max(0, (Date.now() - parsed.getTime()) / 1000);
        if (seconds < 3600) return `${Math.floor(seconds / 60)} мин.`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч.`;
        return `${Math.floor(seconds / 86400)} дн.`;
    }

    function cell(value, className) {
        const td = document.createElement("td");
        td.textContent = value;
        if (className) td.className = className;
        return td;
    }

    function badge(value, className) {
        const span = document.createElement("span");
        span.className = `badge ${className || ""}`;
        span.textContent = value;
        return span;
    }

    function emptyRow(body, message, columns) {
        body.replaceChildren();
        const row = document.createElement("tr");
        const td = cell(message, "table-empty");
        td.colSpan = columns;
        row.appendChild(td);
        body.appendChild(row);
    }

    function renderStorage(storage) {
        const percent = Number.isFinite(storage.percent) ? storage.percent : null;
        text('[data-field="storage-percent"]', percent === null ? "—" : `${percent}%`);
        text('[data-field="storage-used-total"]', storage.used == null || storage.total == null ? unknown : `${bytes(storage.used)} / ${bytes(storage.total)}`);
        text('[data-field="storage-free"]', bytes(storage.free));
        text('[data-field="server-disk"]', percent === null ? unknown : `${percent}%`);
        text('[data-field="server-free"]', bytes(storage.free));
        const state = q("[data-storage-state]");
        state.className = `status-pill ${storage.state || "unknown"}`;
        state.textContent = storage.state === "ok" ? "Норма" : storage.state === "warning" ? "Требует внимания" : storage.state === "critical" ? "Критично" : unknown;
        const color = storage.state === "ok" ? "#17824b" : storage.state === "warning" ? "#d79519" : storage.state === "critical" ? "#bd2c2c" : "#9aabba";
        const ring = q("[data-storage-ring]");
        ring.style.setProperty("--usage", `${Math.max(0, Math.min(100, percent || 0)) * 3.6}deg`);
        ring.style.setProperty("--ring", color);
        const progress = q("[data-storage-progress]");
        progress.style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
        progress.style.background = color;
        const labels = { application: "Приложение", databases: "Базы данных", media: "Uploads / изображения", backups: "Бэкапы", other: "Другое на filesystem" };
        const breakdown = q("[data-storage-breakdown]");
        breakdown.replaceChildren();
        Object.entries(labels).forEach(([key, label]) => {
            const item = document.createElement("div");
            item.className = "storage-segment";
            const name = document.createElement("span");
            const amount = document.createElement("strong");
            name.textContent = label;
            amount.textContent = bytes(storage.categories && storage.categories[key]);
            item.append(name, amount);
            breakdown.appendChild(item);
        });
    }

    function renderBackups(status) {
        const backups = status.backups || [];
        const body = q("[data-backup-rows]");
        body.replaceChildren();
        if (!backups.length) emptyRow(
            body,
            status.backup_directory && !status.backup_directory.available
                ? `Бэкапы недоступны: ${status.backup_directory.message}`
                : "Реальные бэкапы не найдены",
            7
        );
        backups.forEach((backup) => {
            const parts = dateParts(backup.timestamp);
            const row = document.createElement("tr");
            row.append(cell(parts.date), cell(parts.time), cell(bytes(backup.size)));
            const typeCell = document.createElement("td");
            const categories = backup.retention_categories && backup.retention_categories.length
                ? backup.retention_categories
                : [backup.backup_type || backup.type];
            categories.forEach((category) => typeCell.appendChild(
                badge(typeLabels[category] || category, `backup-type ${category}`)
            ));
            row.appendChild(typeCell);
            const statusCell = document.createElement("td");
            statusCell.appendChild(badge(statusLabels[backup.status] || backup.status, backup.status));
            row.appendChild(statusCell);
            row.appendChild(cell(reasonLabels[backup.reason] || backup.reason || "—"));
            const actions = document.createElement("td");
            const restore = document.createElement("button");
            restore.type = "button";
            restore.className = "backup-button danger";
            restore.textContent = "Восстановить данные";
            restore.disabled = !backup.can_restore_data || status.operation.active;
            restore.title = restore.disabled ? (backup.restore_data_reason || status.capabilities.blocked_reason || "Операция недоступна") : "Восстановить только данные ERP";
            restore.addEventListener("click", () => openRecovery({
                kind: "data_restore", backupId: backup.backup_id,
                title: "Восстановить данные",
                confirmation: "ВОССТАНОВИТЬ",
                details: `Backup: ${parts.full}\nРазмер: ${bytes(backup.size)}\nБудут заменены persistent-данные ERP. Код останется текущим.`,
                url: `/api/v1/backups/${encodeURIComponent(backup.backup_id)}/restore`,
            }));
            actions.appendChild(restore);
            if ((backup.backup_type || backup.type) === "manual") {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "backup-button danger";
                remove.textContent = "Удалить";
                remove.disabled = status.operation.active;
                remove.addEventListener("click", async () => {
                    if (!window.confirm(`Удалить ручной бэкап от ${parts.full}? Это действие необратимо.`)) return;
                    remove.disabled = true;
                    try {
                        const response = await fetch(`/api/v1/backups/${encodeURIComponent(backup.backup_id)}`, {
                            method: "DELETE",
                            headers: { Accept: "application/json", "X-CSRF-Token": bootstrap.csrf },
                        });
                        const payload = await response.json();
                        if (!response.ok) throw new Error(payload.message || "Не удалось удалить бэкап");
                        showMessage("Ручной бэкап удалён", false);
                        await refresh();
                    } catch (error) {
                        showMessage(error.message || "Не удалось удалить бэкап", true);
                        remove.disabled = false;
                    }
                });
                actions.appendChild(remove);
            }
            row.appendChild(actions);
            body.appendChild(row);
        });
        const latest = q("[data-last-backup]");
        const latestStrong = latest.querySelector("strong");
        latestStrong.textContent = status.last_backup
            ? `${dateParts(status.last_backup.timestamp).full} · ${bytes(status.last_backup.size)} · ${statusLabels[status.last_backup.status] || status.last_backup.status}`
            : status.backup_directory && !status.backup_directory.available
                ? `Бэкапы недоступны: ${status.backup_directory.message}`
                : "Бэкапы не найдены";
    }

    function renderGit(status) {
        const git = status.git || {};
        const current = q("[data-current-version]");
        const currentStrong = current.querySelector("strong");
        currentStrong.textContent = git.available ? `${git.short || git.commit} · ${git.branch || "detached HEAD"}` : "Git недоступен";
        let details = current.querySelector(".version-details");
        if (!details) { details = document.createElement("div"); details.className = "version-details"; current.appendChild(details); }
        const treeState = git.dirty === true ? "Есть незакоммиченные изменения" : git.dirty === false ? "Рабочее дерево чистое" : "Не удалось проверить рабочее дерево";
        details.textContent = git.available ? `${git.date || "Дата не определена"} · ${git.message || "Без сообщения"} · ${treeState} · Remote: ${git.remote_state === "current" ? "актуален" : git.remote_state === "different" ? "отличается" : git.remote_state === "branch_missing" ? "ветка отсутствует" : "не удалось проверить"}` : "";
        const warning = q("[data-git-warning]");
        warning.hidden = git.dirty === false;
        warning.textContent = git.dirty === true ? "Есть незакоммиченные изменения. Откат кода заблокирован." : git.dirty == null ? "Не удалось подтвердить чистоту production. Откат кода заблокирован." : "";
        const link = q("[data-github-link]");
        link.hidden = !git.remote_url;
        if (git.remote_url) link.href = git.remote_url;
        const body = q("[data-git-rows]");
        body.replaceChildren();
        if (!(git.history || []).length) emptyRow(body, "История Git недоступна", 6);
        (git.history || []).forEach((item) => {
            const row = document.createElement("tr");
            row.append(cell(dateParts(item.date).date), cell(item.short), cell(item.current ? (git.branch || "HEAD") : "commit"), cell(item.message, "message-cell"));
            const state = document.createElement("td");
            state.appendChild(badge(item.current ? "Текущая" : "Доступна", item.current ? "current" : ""));
            row.appendChild(state);
            const action = document.createElement("td");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "backup-button danger";
            button.textContent = "Откатить код";
            button.disabled = !item.can_rollback || status.operation.active;
            button.title = button.disabled ? (item.rollback_reason || status.capabilities.blocked_reason || "Операция недоступна") : "Данные ERP не изменятся";
            button.addEventListener("click", () => openRecovery({
                kind: "code_rollback", title: "Откатить код",
                confirmation: "ОТКАТИТЬ КОД",
                details: `${git.short || git.commit} → ${item.short}\n${item.message}\nДанные ERP не изменяются.`,
                url: `/api/v1/backups/code/${encodeURIComponent(item.commit)}/rollback`,
            }));
            action.appendChild(button);
            row.appendChild(action);
            body.appendChild(row);
        });
    }

    function renderRestorePoints(status) {
        const body = q("[data-restore-rows]");
        body.replaceChildren();
        const points = status.restore_points || [];
        if (!points.length) emptyRow(body, "Подтверждённых точек «код + данные» пока нет", 6);
        points.forEach((point) => {
            const parts = dateParts(point.timestamp);
            const row = document.createElement("tr");
            row.append(cell(parts.date), cell(parts.time), cell((point.git_commit || "").slice(0, 8)), cell(bytes(point.size)));
            const check = document.createElement("td");
            check.appendChild(badge("Проверен", "verified"));
            row.appendChild(check);
            const action = document.createElement("td");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "backup-button danger";
            button.textContent = "Восстановить систему";
            button.disabled = !point.can_restore_system || status.operation.active;
            button.title = button.disabled ? (point.restore_system_reason || status.capabilities.blocked_reason || "Операция недоступна") : "Восстановить код и данные";
            button.addEventListener("click", () => openRecovery({
                kind: "full_restore", backupId: point.backup_id,
                title: "Восстановить систему",
                confirmation: "ВОССТАНОВИТЬ СИСТЕМУ",
                details: `Точка: ${parts.full}\nBackup: ${point.backup_id}\nCommit: ${(point.git_commit || "").slice(0, 12)}\nSchema: ${Object.entries(point.schema_versions || {}).map(([name, version]) => `${name}=${version}`).join(", ") || "Recovery V2 подтверждена"}\nБудут заменены и код, и данные ERP.`,
                url: `/api/v1/backups/${encodeURIComponent(point.backup_id)}/restore-system`,
            }));
            action.appendChild(button);
            row.appendChild(action);
            body.appendChild(row);
        });
        q("[data-capability-block]").textContent = status.capabilities.blocked_reason
            || (points.find((point) => !point.can_restore_system) || {}).restore_system_reason
            || "Backend проверяет backup, commit и совместимость перед каждой операцией.";
    }

    function render(status) {
        text('[data-field="checked-at"]', dateParts(status.checked_at).full);
        renderStorage(status.storage || { categories: {} });
        const card = q("[data-server-card]");
        card.className = `backup-card server-card ${status.overall || "unknown"}`;
        text('[data-field="server-verdict"]', status.overall === "ok" ? "Сервер в норме" : status.overall === "warning" ? "Требуется внимание" : "Обнаружена проблема");
        text('[data-field="server-backup"]', status.last_backup ? dateParts(status.last_backup.timestamp).full : "Не найден");
        text('[data-field="server-backup-age"]', status.last_backup ? age(status.last_backup.timestamp) : "—");
        text('[data-field="server-backup-state"]', status.last_backup ? (statusLabels[status.last_backup.status] || status.last_backup.status) : "Недоступен");
        text('[data-field="server-schedule"]', status.schedule && status.schedule.label || "Расписание не определено");
        text('[data-field="server-service"]', status.service && status.service.active === true ? "Активен" : status.service && status.service.active === false ? "Не активен" : unknown);
        renderBackups(status);
        renderGit(status);
        renderRestorePoints(status);
        const operation = status.operation || {};
        const panel = q("[data-operation]");
        const persistentFailure = ["failed", "critical"].includes(operation.status) && operation.kind !== "manual_backup";
        panel.hidden = !operation.active && !persistentFailure;
        panel.classList.toggle("error", persistentFailure);
        if (operation.active) {
            watchedOperationId = operation.id || watchedOperationId;
        } else if (
            watchedOperationId
            && operation.id === watchedOperationId
            && ["complete", "error", "completed", "failed", "critical"].includes(operation.status)
        ) {
            showMessage(
                operation.message || (["complete", "completed"].includes(operation.status) ? "Операция завершена" : "Операция завершилась с ошибкой"),
                ["error", "failed", "critical"].includes(operation.status)
            );
            watchedOperationId = null;
        }
        const operationTitles = { manual_backup: "Создание бэкапа", data_restore: "Восстановление данных", code_rollback: "Откат кода", full_restore: "Полное восстановление" };
        text("[data-operation-title]", operationTitles[operation.kind] || "Выполняется операция");
        const rollback = operation.automatic_rollback && operation.automatic_rollback.result;
        const rollbackText = rollback === "completed" ? " Предыдущее состояние автоматически восстановлено." : rollback === "failed" ? " Automatic rollback не завершён." : "";
        text("[data-operation-message]", `${operation.message || operation.status || ""}${rollbackText}`);
        const progress = q("[data-operation-progress]");
        progress.replaceChildren();
        (operation.progress || []).forEach((item, index, items) => {
            const node = document.createElement("li");
            node.textContent = item.label || stageLabels[item.stage] || item.stage;
            node.className = index < items.length - 1 ? "done" : (operation.status === "failed" || operation.status === "critical" ? "failed" : "current");
            progress.appendChild(node);
        });
        const create = q("[data-create-backup]");
        create.disabled = operation.active || !status.capabilities.manual_backup;
        create.title = status.capabilities.manual_backup ? "" : "Штатный backup-скрипт недоступен";
    }

    function showMessage(message, error) {
        const node = q("[data-message]");
        node.hidden = false;
        node.className = `backup-notice${error ? " error" : ""}`;
        node.textContent = message;
    }

    function operationKey() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
        return `recovery-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function openRecovery(configuration) {
        pendingRecovery = configuration;
        const dialog = q("[data-recovery-dialog]");
        text("[data-dialog-title]", configuration.title);
        text("[data-dialog-details]", configuration.details);
        text("[data-dialog-label]", `Введите ${configuration.confirmation}`);
        const input = q("[data-dialog-confirmation]");
        input.value = "";
        input.dataset.expected = configuration.confirmation;
        q("[data-dialog-submit]").disabled = true;
        dialog.showModal();
        input.focus();
    }

    q("[data-dialog-confirmation]").addEventListener("input", (event) => {
        q("[data-dialog-submit]").disabled = event.target.value !== event.target.dataset.expected;
    });

    q("[data-dialog-submit]").addEventListener("click", async () => {
        if (!pendingRecovery) return;
        const configuration = pendingRecovery;
        const confirmation = q("[data-dialog-confirmation]").value;
        q("[data-dialog-submit]").disabled = true;
        try {
            const response = await fetch(configuration.url, {
                method: "POST",
                headers: {
                    Accept: "application/json", "Content-Type": "application/json",
                    "X-CSRF-Token": bootstrap.csrf, "X-Idempotency-Key": operationKey(),
                },
                body: JSON.stringify({ confirmation }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.message || "Не удалось запустить recovery");
            watchedOperationId = payload.data.id;
            q("[data-recovery-dialog]").close();
            pendingRecovery = null;
            await refresh();
        } catch (error) {
            q("[data-recovery-dialog]").close();
            pendingRecovery = null;
            showMessage(error.message || "Не удалось запустить recovery", true);
        }
    });

    async function refresh() {
        const response = await fetch("/api/v1/backups/status", { headers: { Accept: "application/json" } });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "Не удалось обновить состояние");
        bootstrap.status = payload.data;
        render(payload.data);
        if (payload.data.operation && payload.data.operation.active) scheduleRefresh(2500);
    }

    function scheduleRefresh(delay) {
        if (refreshTimer) window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(async () => {
            refreshTimer = null;
            try {
                await refresh();
            } catch (error) {
                if (watchedOperationId) scheduleRefresh(2500);
            }
        }, delay);
    }

    q("[data-create-backup]").addEventListener("click", async () => {
        const button = q("[data-create-backup]");
        button.disabled = true;
        try {
            const response = await fetch("/api/v1/backups", {
                method: "POST",
                headers: { Accept: "application/json", "X-CSRF-Token": bootstrap.csrf },
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.message || "Не удалось создать бэкап");
            watchedOperationId = payload.data && payload.data.id || watchedOperationId;
            showMessage("Создание бэкапа запущено", false);
            await refresh();
        } catch (error) {
            showMessage(error.message || "Не удалось создать бэкап", true);
            button.disabled = false;
        }
    });

    render(bootstrap.status || {});
    if (watchedOperationId) {
        scheduleRefresh(2500);
    } else {
        refreshTimer = window.setTimeout(() => refresh().catch(() => showMessage("Не удалось обновить актуальное состояние", true)), 30000);
    }
}());
