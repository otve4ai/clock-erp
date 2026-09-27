/* Task and project drawers use the API's policy capabilities, never client roles. */
(() => {
    'use strict';
    const ui = window.TasksModuleUI, api = window.TasksModuleAPI;
    if (!ui || !api) return;
    const {$, $$, node, button} = ui;
    const dialog = $('#tm-dialog'), body = $('#tm-dialog-body');
    let generation = 0, previousFocus = null, submitting = false;
    function open(title, kicker) {
        generation += 1; submitting = false;
        if (dialog.hidden) previousFocus = document.activeElement;
        dialog.hidden = false; $('#tm-backdrop').hidden = false;
        $('#tm-dialog-close').disabled = false;
        document.querySelector('.app').inert = true;
        $('#tm-dialog-title').textContent = title; $('#tm-dialog-kicker').textContent = kicker;
        $('#tm-dialog-error').hidden = true; body.replaceChildren(); dialog.focus();
        return generation;
    }
    function close() {
        if (submitting) return;
        generation += 1; dialog.hidden = true; $('#tm-backdrop').hidden = true; document.querySelector('.app').inert = false;
        if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    }
    $('#tm-dialog-close').addEventListener('click', close); $('#tm-backdrop').addEventListener('click', close);
    dialog.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); close(); }
        if (event.key !== 'Tab') return;
        const focusable = $$('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]', dialog).filter(item => item.getClientRects().length);
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (!first) { event.preventDefault(); dialog.focus(); }
        else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) { event.preventDefault(); first.focus(); }
    });
    function field(form, name, label, type = 'text', value = '', wide = false) {
        const wrapper = node('label', `tm-field${wide ? ' tm-wide' : ''}`); const control = node(type === 'select' ? 'select' : type === 'textarea' ? 'textarea' : 'input');
        if (!['select', 'textarea'].includes(type)) control.type = type;
        control.name = name; control.value = value === null ? '' : value;
        wrapper.append(node('span', '', label), control); form.append(wrapper); return control;
    }
    function options(select, values, selected) { Object.entries(values).forEach(([value, label]) => select.add(new Option(label, value))); select.value = selected; }
    function error(error) {
        ui.showError(error, '#tm-dialog-error');
        if (error.status === 422 && error.fields) {
            const labels = {title: 'Название', description: 'Описание', assigned_to: 'Исполнитель', project_id: 'Проект', name: 'Название', user_id: 'Участник'};
            const text = Object.entries(error.fields).map(([key, value]) => `${labels[key] || 'Данные'}: ${value}`).join(' ');
            if (text) $('#tm-dialog-error').append(node('p', '', text));
        }
    }
    async function submit(operation, success, conflict) {
        if (submitting) return;
        const current = generation; submitting = true; $('#tm-dialog-error').hidden = true;
        const enabled = $$('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)', dialog);
        enabled.forEach(control => { control.disabled = true; });
        try {
            const result = await operation(); if (current !== generation) return;
            submitting = false; await success(result);
        } catch (failure) {
            if (current !== generation) return;
            submitting = false;
            if (failure.status === 409 && conflict) { await conflict(); error(failure); }
            else error(failure);
        } finally {
            if (current === generation) { submitting = false; enabled.forEach(control => { control.disabled = false; }); }
        }
    }
    function actions(form, submitLabel) {
        const result = node('div', 'tm-form-actions tm-wide');
        const save = node('button', 'tm-primary', submitLabel); save.type = 'submit';
        result.append(button('Закрыть', close), save); form.append(result); return result;
    }
    function create(micro = false, projectId = null) {
        open(micro ? 'Новая микрозадача' : 'Новая задача', micro ? 'Срок — 24 часа с момента создания' : 'Короткое поручение');
        const form = node('form', 'tm-form'); const title = field(form, 'title', 'Название', 'text', '', true); title.required = true; title.maxLength = 500;
        ui.userOptions(field(form, 'assigned_to', 'Исполнитель', 'select'), ui.boot.userId);
        if (!micro) { field(form, 'deadline_date', 'Срок', 'date'); ui.projectOptions(field(form, 'project_id', 'Проект', 'select', '', true), projectId); }
        actions(form, 'Создать'); body.append(form); title.focus();
        form.addEventListener('submit', event => {
            event.preventDefault(); const values = {title: form.elements.title.value, assigned_to: Number(form.elements.assigned_to.value)};
            if (!micro) { values.deadline_date = form.elements.deadline_date.value || null; values.project_id = Number(form.elements.project_id.value) || null; }
            submit(() => api.request(micro ? '/microtasks' : '/tasks', 'POST', values), async task => { showTask(task); ui.notice('Задача создана'); await ui.refresh(true); });
        });
    }
    const events = {created: 'Создана', content_changed: 'Изменено содержимое', reassigned: 'Изменён исполнитель', deadline_changed: 'Изменён срок', priority_changed: 'Изменён приоритет', status_changed: 'Изменён статус', completed: 'Завершена', reopened: 'Возвращена в работу', deleted: 'Удалена', restored: 'Восстановлена', project_changed: 'Изменён проект', converted_to_normal: 'Преобразована в обычную задачу', renamed: 'Проект переименован', archived: 'Проект архивирован', member_added: 'Добавлен участник', member_removed: 'Удалён участник'};
    const fieldNames = {title: 'Название', description: 'Описание', assigned_to: 'Исполнитель', deadline_date: 'Срок', priority: 'Приоритет', status: 'Статус', project_id: 'Проект', name: 'Название'};
    function history(path, current) {
        const section = node('section', 'tm-history'); section.append(node('h3', '', 'История'));
        const list = node('ol'); const message = node('p', 'tm-muted', 'Загрузка…'); let offset = 0;
        const more = button('Ещё события', load); more.hidden = true; section.append(message, list, more); body.append(section);
        async function load() {
            more.disabled = true;
            try {
                const page = await ui.request(path, {limit: 30, offset}); if (current !== generation) return;
                page.items.forEach(event => {
                    const entry = node('li'); entry.append(node('span', '', `${ui.displayInstant(event.timestamp)} · ${ui.name(event.actor)} · ${events[event.event_type] || 'Изменение задачи'}`));
                    Object.entries(event.payload || {}).forEach(([key, value]) => {
                        if (fieldNames[key] && value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, 'after')) {
                            const display = raw => raw === null ? '—' : key === 'assigned_to' ? ui.name(raw) : key === 'project_id' ? ui.projectName(raw) : key === 'status' ? ui.statusNames[raw] : String(raw);
                            entry.append(node('p', '', `${fieldNames[key]}: ${display(value.before)} → ${display(value.after)}`));
                        }
                        if (key === 'user_id') entry.append(node('p', '', ui.name(value)));
                    }); list.append(entry);
                });
                offset += page.items.length; more.hidden = page.items.length < 30; message.textContent = offset ? '' : 'История пока пуста';
            } catch (_) { if (current === generation) { message.textContent = 'Не удалось загрузить историю.'; more.textContent = 'Повторить'; more.hidden = false; } }
            finally { more.disabled = false; }
        }
        load();
    }
    function confirmDelete(task) {
        if ($('#tm-delete-confirm', body)) return;
        const confirm = node('div', 'tm-confirm'); confirm.id = 'tm-delete-confirm';
        confirm.append(node('p', '', 'Удалить задачу? Она исчезнет из списков. История сохранится; задачу можно восстановить из этой карточки.'));
        const yes = button('Удалить задачу', () => submit(() => api.request(`/tasks/${task.id}/delete`, 'POST', {version: task.version}), async result => { showTask(result); ui.notice('Задача удалена'); await ui.refresh(true); }, () => taskById(task.id)), 'tm-danger');
        confirm.append(button('Отмена', () => confirm.remove()), yes); body.prepend(confirm); yes.focus();
    }
    function preview(task) {
        const micro = task.task_type === 'micro';
        const current = open(task.title, micro ? '⚡ Микрозадача · 24 часа' : 'Ознакомление с задачей');
        const details = node('div', 'tm-details tm-task-preview');
        details.append(node('p', '', `Поставил: ${ui.name(task.created_by)}`), node('p', '', `Исполнитель: ${ui.name(task.assigned_to)}`),
            node('p', '', `Статус: ${micro ? (task.status === 'done' ? 'Готово' : 'К выполнению') : ui.statusNames[task.status]}`));
        if (micro) {
            details.append(node('p', '', `Срок: ${ui.displayInstant(task.micro_deadline_at)}`), node('p', '', '24 часа отсчитываются от создания. Во входящих остаётся до выполнения.'));
            if (task.status !== 'done') { const timer = node('strong', 'tm-deadline', ui.remaining(task.micro_deadline_at)); timer.dataset.microDeadline = task.micro_deadline_at; timer.classList.toggle('tm-overdue', timer.textContent.startsWith('Просрочено')); details.append(timer); }
        } else {
            details.append(node('p', '', `Срок: ${ui.displayDate(task.deadline_date)}`), node('p', '', `Проект: ${ui.projectName(task.project_id)}`),
                node('p', '', `Приоритет: ${{low: 'Низкий', normal: 'Обычный', high: 'Высокий'}[task.priority]}`), node('p', 'tm-preview-description', task.description || 'Без описания'));
        }
        body.append(details);
        const controls = node('div', 'tm-form-actions'); controls.append(button('Закрыть', close));
        if (!task.deleted_at && task.status !== 'done' && task.assigned_to === ui.boot.userId && (micro || task.permissions.accept)) {
            controls.append(button(micro ? 'Готово' : 'Взять в работу', () => submit(() => ui.inboxAction(task), async () => close(), async () => preview(await ui.request(`/tasks/${task.id}`))), 'tm-primary'));
        }
        body.append(controls); history(`/tasks/${task.id}/activity`, current);
    }
    function showTask(task, focus) {
        const micro = task.task_type === 'micro'; const current = open(task.title, `${micro ? 'Микрозадача' : 'Задача'} #${task.id}${task.deleted_at ? ' · удалена' : ''}`);
        const form = node('form', 'tm-form'); const controls = {};
        controls.title = field(form, 'title', 'Название', 'text', task.title, true); controls.title.required = true; controls.title.maxLength = 500;
        controls.status = field(form, 'status', 'Статус', 'select'); options(controls.status, micro ? {new: 'К выполнению', done: 'Готово'} : ui.statusNames, task.status);
        controls.assigned_to = field(form, 'assigned_to', 'Исполнитель', 'select'); ui.userOptions(controls.assigned_to, task.assigned_to);
        if (!micro) {
            controls.deadline_date = field(form, 'deadline_date', 'Срок', 'date', task.deadline_date);
            controls.priority = field(form, 'priority', 'Приоритет', 'select'); options(controls.priority, {low: 'Низкий', normal: 'Обычный', high: 'Высокий'}, task.priority);
            controls.project_id = field(form, 'project_id', 'Проект', 'select', '', true); ui.projectOptions(controls.project_id, task.project_id);
            controls.description = field(form, 'description', 'Описание', 'textarea', task.description, true); controls.description.maxLength = 50000;
        }
        Object.values(controls).forEach(control => { control.disabled = !task.permissions.edit; });
        controls.assigned_to.disabled = !task.permissions.reassign; controls.status.disabled = !task.permissions.change_status;
        if (task.permissions.edit) actions(form, 'Сохранить');
        else { const text = node('p', 'tm-muted tm-wide', task.deleted_at ? 'Задача удалена. Доступно чтение истории.' : 'У вас есть доступ к просмотру этой задачи.'); form.append(text); }
        body.append(form);
        const meta = node('div', 'tm-details'); meta.append(node('div', '', `Поставил: ${ui.name(task.created_by)}`), node('div', '', `Создана: ${ui.displayInstant(task.created_at)}`));
        if (micro) meta.append(node('div', '', `Срок: ${ui.displayInstant(task.micro_deadline_at)}${task.status !== 'done' ? ` · ${ui.remaining(task.micro_deadline_at)}` : ''}`));
        if (task.completed_at) meta.append(node('div', '', `Завершена: ${ui.displayInstant(task.completed_at)}`)); body.append(meta);
        const secondary = node('div', 'tm-form-actions');
        if (micro && task.permissions.edit) secondary.append(button('Сделать обычной задачей', () => submit(() => api.request(`/microtasks/${task.id}/convert`, 'POST', {version: task.version}), async result => { showTask(result); await ui.refresh(); }, () => taskById(task.id))));
        if (task.permissions.delete) secondary.append(button('Удалить…', () => confirmDelete(task), 'tm-danger'));
        if (task.permissions.restore) secondary.append(button('Восстановить задачу', () => submit(() => api.request(`/tasks/${task.id}/restore`, 'POST', {version: task.version}), async result => { showTask(result); ui.notice('Задача восстановлена'); await ui.refresh(true); }, () => taskById(task.id)), 'tm-primary'));
        body.append(secondary); history(`/tasks/${task.id}/activity`, current);
        form.addEventListener('submit', event => {
            event.preventDefault(); const values = {version: task.version};
            Object.entries(controls).forEach(([key, control]) => {
                if (control.disabled) return; let value = control.value;
                if (key === 'project_id') value = Number(value) || null;
                if (key === 'assigned_to') value = Number(value);
                if (key === 'deadline_date') value = value || null;
                if (value !== task[key]) values[key] = value;
            });
            if (Object.keys(values).length === 1) { ui.notice('Нет изменений для сохранения'); return; }
            submit(() => api.request(`/${micro ? 'microtasks' : 'tasks'}/${task.id}`, 'PATCH', values), async result => { showTask(result); ui.notice('Изменения сохранены'); await ui.refresh(true); }, () => taskById(task.id));
        });
        if (focus === 'delete') confirmDelete(task); else if (controls[focus] && !controls[focus].disabled) controls[focus].focus();
    }
    async function taskById(id, focus) {
        const current = open('Загрузка задачи…', 'Карточка'); body.append(node('p', 'tm-loading', 'Загрузка…'));
        try { const task = await ui.request(`/tasks/${id}`); if (current === generation) showTask(task, focus); }
        catch (failure) { if (current === generation) { body.replaceChildren(); error(failure); } }
    }
    async function project(id) {
        const current = open(id ? 'Настройки проекта' : 'Новый проект', 'Проект');
        let value = null;
        if (id) {
            try { value = await ui.request(`/projects/${id}`); if (current !== generation) return; }
            catch (failure) { if (current === generation) error(failure); return; }
        }
        const manageable = !value || value.permissions.manage;
        const form = node('form', 'tm-form'); const title = field(form, 'name', 'Название проекта', 'text', value ? value.name : '', true);
        title.required = true; title.maxLength = 200; title.disabled = !manageable;
        if (manageable) actions(form, id ? 'Сохранить название' : 'Создать проект'); body.append(form);
        form.addEventListener('submit', event => {
            event.preventDefault(); if (value && value.name === title.value) { ui.notice('Название не изменилось'); return; }
            const payload = {name: title.value}; if (value) payload.version = value.version;
            submit(() => api.request(id ? `/projects/${id}` : '/projects', id ? 'PATCH' : 'POST', payload), async result => { close(); await ui.refresh(true); ui.navigate('project', {project: result.id}); }, () => project(id));
        });
        if (!id) { title.focus(); return; }
        body.append(node('p', 'tm-details', `Владелец: ${ui.name(value.owner_id)}`));
        if (manageable) {
            body.append(button(value.archived_at ? 'Восстановить проект' : 'Архивировать проект', () => submit(() => api.request(`/projects/${id}/${value.archived_at ? 'restore' : 'archive'}`, 'POST', {version: value.version}), async () => { close(); await ui.refresh(true); ui.notice(value.archived_at ? 'Проект восстановлен' : 'Проект архивирован. Статусы задач сохранены.'); }, () => project(id))));
        }
        const members = node('section', 'tm-history'); members.append(node('h3', '', 'Участники проекта')); body.append(members);
        try {
            const items = await ui.allPages(`/projects/${id}/members`, {}); if (current !== generation) return;
            items.forEach(member => {
                const row = node('div', 'tm-member'); row.append(ui.assignee(member.user_id));
                if (manageable) row.append(button('Убрать', () => submit(() => api.request(`/projects/${id}/members/${member.user_id}`, 'DELETE', {version: value.version}), async () => { await project(id); await ui.refresh(true); }, () => project(id)), 'tm-link'));
                members.append(row);
            });
            if (!items.length) members.append(node('p', 'tm-muted', 'Пока только владелец'));
            if (manageable) {
                const add = node('form', 'tm-quick'); const select = node('select'); select.setAttribute('aria-label', 'Новый участник');
                ui.state.users.filter(user => user.id !== value.owner_id && !items.some(member => member.user_id === user.id)).forEach(user => select.add(new Option(user.name, String(user.id))));
                const save = node('button', '', 'Добавить'); save.type = 'submit'; save.disabled = !select.options.length; add.append(select, save); members.append(add);
                add.addEventListener('submit', event => { event.preventDefault(); const userId = Number(select.value); submit(() => api.request(`/projects/${id}/members`, 'POST', {version: value.version, user_id: userId}), async () => { await project(id); await ui.refresh(true); }, () => project(id)); });
            }
        } catch (failure) { if (current === generation) error(failure); }
        if (current === generation) history(`/projects/${id}/activity`, current);
    }
    window.TasksModuleDialogs = Object.freeze({task: taskById, showTask, preview, create, project, close});
})();
