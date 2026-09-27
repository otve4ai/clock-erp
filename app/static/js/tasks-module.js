/* Isolated Tasks screen. All user text enters the DOM through textContent/value. */
(() => {
    'use strict';
    const api = window.TasksModuleAPI;
    const boot = window.TASKS_MODULE_BOOTSTRAP;
    if (!api || !boot) return;
    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const node = (tag, cls, text) => {
        const result = document.createElement(tag);
        if (cls) result.className = cls;
        if (text !== undefined) result.textContent = text;
        return result;
    };
    const button = (text, action, cls = '') => {
        const result = node('button', cls, text); result.type = 'button';
        result.addEventListener('click', action); return result;
    };
    const statusNames = {new: 'К выполнению', in_progress: 'В работе', waiting: 'Ожидает принятия', done: 'Готово'};
    const scopeNames = {my: 'Задачи, порученные мне', created: 'Поставленные мной', team: 'Задачи команды', all: 'Все доступные задачи'};
    const viewNames = {main: 'Задачи', inbox: 'Входящие', today: 'Сегодня', overdue: 'Просрочено', micro: 'Микрозадачи', delegated_waiting: 'Ожидаю', projects: 'Проекты', project: 'Проект', archive: 'Архив задач'};
    const state = {view: 'main', scope: 'my', microScope: 'my', search: '', filters: {}, page: 0, limit: 30,
        project: null, projectMode: 'list', projectsArchived: false, users: [], projects: new Map(), activeProjects: [],
        generation: 0, microGeneration: 0, summaryGeneration: 0, referenceGeneration: 0, clockOffset: 0, businessDate: '', busy: new Set()};
    let searchTimer, searchGeneration = 0;
    const name = id => (state.users.find(user => user.id === id) || {}).name || `Сотрудник #${id}`;
    const projectName = id => id ? (state.projects.get(id) || {}).name || `Проект #${id}` : 'Без проекта';
    const now = () => Date.now() + state.clockOffset;
    const today = () => new Date(now() + 10800000).toISOString().slice(0, 10);
    const displayDate = value => value ? value.slice(0, 10).split('-').reverse().join('.') : 'Без срока';
    const displayInstant = value => value ? new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short'}).format(new Date(value)) : '—';
    const remaining = value => {
        const delta = Date.parse(value) - now();
        if (delta < 0) {
            const late = Math.floor(-delta / 60000);
            if (!late) return 'Просрочено меньше минуты';
            return late < 60 ? `Просрочено ${late} мин` : `Просрочено ${Math.floor(late / 60)} ч${late % 60 ? ` ${late % 60} м` : ''}`;
        }
        const minutes = Math.ceil(delta / 60000);
        if (minutes < 60) return `${minutes} мин`;
        return `${Math.floor(minutes / 60)} ч${minutes % 60 ? ` ${minutes % 60} м` : ''}`;
    };
    const assignee = id => {
        const result = node('span', 'tm-assignee'); const label = name(id);
        result.append(node('span', 'tm-avatar', label.split(/\s+/).slice(0, 2).map(part => part[0]).join('')), node('span', '', label));
        result.title = label; return result;
    };
    const showError = (error, selector = '#tm-error') => {
        const box = $(selector); const text = error && error.status ? error.message : 'Не удалось загрузить данные. Повторите попытку.';
        (selector === '#tm-error' ? $('span', box) : box).textContent = text;
        box.hidden = false;
    };
    const notice = text => { $('#tm-notice').textContent = text; $('#tm-notice').hidden = !text; };
    const empty = (root, title, detail = '') => { const box = node('div', 'tm-empty'); box.append(node('strong', '', title), node('span', '', detail)); root.replaceChildren(box); };
    const request = (path, values = {}) => api.request(path + api.query(values));
    function userOptions(select, selected, placeholder) {
        select.replaceChildren();
        if (placeholder !== undefined) select.add(new Option(placeholder, ''));
        state.users.forEach(user => select.add(new Option(user.id === boot.userId ? `${user.name} (я)` : user.name, String(user.id))));
        if (selected && !state.users.some(user => user.id === Number(selected))) select.add(new Option(name(Number(selected)), String(selected)));
        select.value = selected === undefined ? '' : String(selected);
    }
    function projectOptions(select, selected, all = false) {
        select.replaceChildren(); select.add(new Option(all ? 'Все проекты' : 'Без проекта', ''));
        if (all) select.add(new Option('Без проекта', 'none'));
        (all ? Array.from(state.projects.values()) : state.activeProjects).forEach(project => select.add(new Option(project.name, String(project.id))));
        if (selected && !Array.from(select.options).some(option => option.value === String(selected))) select.add(new Option(projectName(Number(selected)), String(selected)));
        select.value = selected || '';
    }
    async function allPages(path, options) {
        const items = []; let offset = 0;
        while (true) {
            const page = await request(path, Object.assign({}, options, {limit: 100, offset}));
            items.push(...page.items); offset += page.items.length;
            if (offset >= page.total || !page.items.length) return items;
            if (offset > 1000000) throw new Error('Pagination limit');
        }
    }
    async function referenceData() {
        const generation = ++state.referenceGeneration;
        try {
            const [directory, active, archived] = await Promise.all([request('/directory'), allPages('/projects', {}), allPages('/projects', {archived: 'true'})]);
            if (generation !== state.referenceGeneration) return false;
            state.referenceError = null;
            state.users = directory.items; state.clockOffset = Date.parse(directory.server_now) - Date.now(); state.businessDate = directory.business_date;
            state.activeProjects = active; state.projects = new Map(active.concat(archived).map(project => [project.id, project]));
            userOptions($('#tm-micro-form select'), boot.userId);
            userOptions($('#tm-filters [name=assigned_to]'), state.filters.assigned_to, 'Все исполнители');
            userOptions($('#tm-filters [name=created_by]'), state.filters.created_by, 'Все постановщики');
            projectOptions($('#tm-filters [name=project_id]'), state.filters.project_id || (state.filters.project === 'none' ? 'none' : ''), true);
            const preview = $('#tm-project-preview'); preview.replaceChildren();
            active.slice(0, 6).forEach(project => {
                const row = node('div', 'tm-project-preview');
                const link = node('a', '', project.name); link.href = `?view=project&project=${project.id}`;
                link.addEventListener('click', event => { event.preventDefault(); navigate('project', {project: project.id}); });
                row.append(node('span', '', '▣'), link, node('small', project.counters.overdue ? 'tm-overdue' : '', project.counters.overdue ? `${project.counters.overdue} просрочено` : `${project.counters.open} открыто`)); preview.append(row);
            });
            if (!active.length) empty(preview, 'Нет активных проектов');
            const people = $('#tm-people'); people.replaceChildren();
            state.users.slice(0, 6).forEach(user => { const item = button('', () => navigate('main', {scope: 'team', filters: {assigned_to: String(user.id)}}), 'tm-person'); item.append(assignee(user.id)); people.append(item); });
        } catch (error) {
            if (generation !== state.referenceGeneration) return false;
            state.referenceError = error; showError(error);
        }
        return true;
    }
    async function summaries() {
        const generation = ++state.summaryGeneration;
        const data = await request('/tasks/summary');
        if (generation !== state.summaryGeneration) return;
        Object.keys(data).forEach(key => { const counter = $(`[data-count="${key}"]`); if (counter && key !== 'inbox') counter.textContent = data[key]; });
        const counts = data.inbox_counts || {normal: data.inbox, micro: 0};
        $$('[data-tasks-module-badge],#tm-inbox-nav-count,[data-count="inbox"]').forEach(item => {
            const micro = item.dataset.tasksModuleBadge === 'micro'; const count = micro ? counts.micro : counts.normal;
            item.textContent = micro ? `⚡ ${count}` : String(count); item.hidden = !count;
            item.setAttribute('aria-label', `${micro ? 'Невыполненные микрозадачи' : 'Обычные входящие'}: ${count}`);
        });
        $('#tm-overview-text').textContent = `${data.today} на сегодня · ${data.overdue} просрочено · ${data.delegated_waiting} ожидаю`;
    }
    function taskRow(task, micro = false) {
        const row = node('div', `tm-row${micro ? ' tm-row-micro' : ''}`); row.dataset.taskId = task.id;
        const check = node('input'); check.type = 'checkbox'; check.checked = task.status === 'done';
        check.setAttribute('aria-label', `${check.checked ? 'Вернуть в работу' : 'Завершить'}: ${task.title}`);
        check.disabled = !task.permissions.change_status;
        check.addEventListener('change', () => { const target = check.checked ? 'done' : 'new'; check.checked = task.status === 'done'; changeStatus(task, target); });
        const title = button(task.title, () => window.TasksModuleDialogs.task(task.id), 'tm-task-title'); title.title = task.title;
        row.append(check, title);
        if (!micro) { const priority = node('span', `tm-priority tm-priority-${task.priority}`); priority.title = {low: 'Низкий', normal: 'Обычный', high: 'Высокий'}[task.priority]; row.append(priority); }
        row.append(assignee(task.assigned_to));
        const late = task.status !== 'done' && (micro ? Date.parse(task.micro_deadline_at) < now() : task.deadline_date && task.deadline_date < today());
        const date = node('span', `tm-deadline${late ? ' tm-overdue' : ''}`, micro ? (task.status === 'done' ? 'Готово' : remaining(task.micro_deadline_at)) : task.deadline_date === today() ? 'Сегодня' : displayDate(task.deadline_date));
        date.title = micro ? displayInstant(task.micro_deadline_at) : displayDate(task.deadline_date); row.append(date);
        if (micro && task.status !== 'done') date.dataset.microDeadline = task.micro_deadline_at;
        if (!micro) row.append(node('span', `tm-status tm-status-${task.status}`, statusNames[task.status]));
        const menu = node('details', 'tm-menu'); const summary = node('summary', '', '⋮'); summary.setAttribute('aria-label', `Действия: ${task.title}`);
        const actions = node('div'); actions.append(button('Открыть карточку', () => { menu.open = false; window.TasksModuleDialogs.task(task.id); }));
        if (task.permissions.reassign) actions.append(button('Переназначить', () => { menu.open = false; window.TasksModuleDialogs.task(task.id, 'assigned_to'); }));
        if (micro && task.permissions.edit) actions.append(button('Сделать обычной задачей', () => { menu.open = false; mutateTask(task, '/microtasks/' + task.id + '/convert', {}, 'Задача преобразована'); }));
        if (task.permissions.delete) actions.append(button('Удалить…', () => { menu.open = false; window.TasksModuleDialogs.task(task.id, 'delete'); }, 'tm-danger'));
        menu.append(summary, actions); row.append(menu); return row;
    }
    async function mutateTask(task, path, values, message) {
        if (state.busy.has(task.id)) return;
        state.busy.add(task.id);
        try {
            await api.request(path, 'POST', Object.assign({version: task.version}, values)); notice(message || 'Изменение сохранено'); await refresh(!!task.project_id);
        } catch (error) {
            if (error.status === 409) { notice('Задача уже изменена. Список обновлён; повторите действие.'); await refresh(); }
            else showError(error);
        } finally { state.busy.delete(task.id); }
    }
    const changeStatus = (task, status) => mutateTask(task, `/tasks/${task.id}/status`, {status}, status === 'done' ? 'Задача завершена' : 'Статус изменён');
    let microCollapsed = false;
    try { microCollapsed = localStorage.getItem(`tasks-module:micro-collapsed:${boot.userId}`) === 'true'; } catch (_) { /* Storage may be disabled. */ }
    function collapseMicro(value) {
        microCollapsed = value; $('#tm-micro-body').hidden = value; $('#tm-micro-brief').hidden = !value; $('#tm-micro-add').hidden = !value;
        $('#tm-micro-scopes').hidden = value; $('#tm-micro-toggle').setAttribute('aria-expanded', String(!value));
        $('#tm-micro-toggle').setAttribute('aria-label', value ? 'Развернуть микрозадачи' : 'Свернуть микрозадачи'); $('#tm-micro-toggle').textContent = value ? '⌄' : '⌃';
        try { localStorage.setItem(`tasks-module:micro-collapsed:${boot.userId}`, String(value)); } catch (_) { /* Optional preference. */ }
    }
    async function microPreview() {
        const generation = ++state.microGeneration;
        try {
            const [list, summary] = await Promise.all([request('/microtasks', {scope: state.microScope, limit: 4}), request('/microtasks/summary')]);
            if (generation !== state.microGeneration) return;
            $('#tm-micro-error').hidden = true;
            const root = $('#tm-micro-list'); root.replaceChildren(...list.items.map(task => taskRow(task, true)));
            if (!list.items.length) empty(root, 'Нет активных микрозадач', 'Добавьте небольшое поручение на ближайшие 24 часа.');
            $('#tm-micro-count').textContent = list.total;
            $$('[data-micro-count]').forEach(item => { item.textContent = summary[item.dataset.microCount]; });
            $$('[data-micro-scope]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.microScope === state.microScope)));
            $('#tm-micro-brief').textContent = `${summary.my_active} моих · ${summary.my_overdue} просрочено${summary.nearest_my_deadline_at ? ` · ближайшая через ${remaining(summary.nearest_my_deadline_at)}` : ''}`;
        } catch (error) {
            if (generation !== state.microGeneration) return;
            empty($('#tm-micro-list'), 'Микрозадачи недоступны');
            showError(error, '#tm-micro-error');
        }
    }
    function listQuery() {
        const values = Object.assign({scope: state.scope, search: state.search, limit: state.limit, offset: state.page * state.limit}, state.filters);
        if (['today', 'overdue', 'delegated_waiting', 'archive'].includes(state.view)) values.view = state.view;
        if (state.view === 'project') { delete values.project; values.project_id = state.project.id; }
        if (state.view === 'micro') { ['view', 'priority', 'project', 'project_id', 'date_from', 'date_to'].forEach(key => delete values[key]); }
        return values;
    }
    function syncChrome() {
        const project = state.view === 'project'; const archive = state.view === 'archive'; const micros = state.view === 'micro';
        const hasFilters = !['inbox', 'projects'].includes(state.view);
        $('#tm-title').textContent = project ? state.project.name : viewNames[state.view];
        $('#tm-subtitle').textContent = archive ? 'Завершённые задачи и история работы' : state.view === 'delegated_waiting' ? 'Задачи, которые вы поручили другим и ждёте выполнения' : 'Мои задачи, микрозадачи, проекты и работа команды';
        $('#tm-micro').hidden = state.view !== 'main'; $('#tm-right').hidden = state.view !== 'main';
        $('#tm-content-layout').classList.toggle('tm-full', state.view !== 'main');
        $('#tm-toolbar').hidden = !hasFilters; $('#tm-periods').hidden = !archive;
        $('#tm-project-controls').hidden = !project; $('#tm-project-list-controls').hidden = state.view !== 'projects';
        $('#tm-search').disabled = state.view === 'inbox';
        $('#tm-create').hidden = project && !!state.project.archived_at;
        $('#tm-create').textContent = micros ? '＋ Микрозадача' : '＋ Новая задача';
        $('#tm-list-title').textContent = project ? 'Задачи проекта' : state.view === 'main' ? scopeNames[state.scope] : viewNames[state.view];
        $('#tm-sort-label').textContent = archive ? 'Последние завершённые' : micros ? 'Сначала по сроку' : ['inbox', 'projects'].includes(state.view) || (project && state.projectMode === 'board') ? '' : 'Сначала по сроку';
        if (!hasFilters) $('#tm-filters').hidden = true;
        if (archive) $('#tm-filters').hidden = false;
        $$('[data-filter=period]').forEach(item => { item.hidden = !archive; });
        $('[data-filter=status]').hidden = archive;
        $('[data-filter=priority]').hidden = micros || archive;
        $('[data-filter=project]').hidden = micros || project;
        $('[data-filter=overdue]').hidden = !micros;
        const status = $('#tm-filters [name=status]');
        Array.from(status.options).forEach(option => { option.hidden = micros && ['in_progress', 'waiting'].includes(option.value); });
        $$('[data-scope]').forEach(item => item.setAttribute('aria-pressed', String(state.scope === item.dataset.scope)));
        $$('.tm-nav [data-view]').forEach(item => { if (item.dataset.view === state.view) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current'); });
        if (project) {
            $('#tm-project-settings').hidden = !state.project.permissions.manage;
            $('#tm-project-archive-label').textContent = state.project.archived_at ? 'Проект в архиве' : '';
            $$('[data-project-mode]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.projectMode === state.projectMode)));
        }
        $('#tm-filters-toggle').setAttribute('aria-expanded', String(!$('#tm-filters').hidden));
    }
    function pagination(data) {
        $('#tm-pagination').hidden = data.total <= state.limit; $('#tm-prev').disabled = state.page === 0;
        $('#tm-next').disabled = (state.page + 1) * state.limit >= data.total;
        $('#tm-page').textContent = `${state.page + 1} / ${Math.max(1, Math.ceil(data.total / state.limit))}`;
    }
    function renderTasks(root, data) {
        if (!data.items.length) { empty(root, 'Задач не найдено', 'Измените фильтры или создайте новую задачу.'); return; }
        if (state.view === 'micro') { root.append(...data.items.map(task => taskRow(task, true))); return; }
        if (state.view === 'archive') { renderArchive(root, data.items); return; }
        const groups = new Map(); data.items.forEach(task => { const key = task.project_id; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(task); });
        groups.forEach((tasks, id) => {
            const group = node('details', 'tm-group'); group.open = true; const title = node('summary', '', projectName(id));
            title.append(node('span', '', tasks.length)); group.append(title, ...tasks.map(task => taskRow(task))); root.append(group);
        });
    }
    function renderArchive(root, tasks) {
        const table = node('table', 'tm-table'); const head = node('thead'); const labels = node('tr');
        ['Задача', 'Проект', 'Исполнитель', 'Поставил', 'Завершена', 'Статус'].forEach(label => labels.append(node('th', '', label))); head.append(labels);
        const body = node('tbody'); tasks.forEach(task => {
            const row = node('tr'); const title = node('td'); title.append(button(task.title, () => window.TasksModuleDialogs.task(task.id)));
            row.append(title, node('td', '', projectName(task.project_id)), node('td', '', name(task.assigned_to)), node('td', '', name(task.created_by)), node('td', '', displayInstant(task.completed_at)));
            const status = node('td'); status.append(node('span', 'tm-status tm-status-done', 'Готово')); row.append(status);
            Array.from(row.children).forEach((cell, index) => { cell.dataset.label = ['Задача', 'Проект', 'Исполнитель', 'Поставил', 'Завершена', 'Статус'][index]; }); body.append(row);
        }); table.append(head, body); root.append(table);
    }
    function renderProjects(root, data) {
        if (!data.items.length) { empty(root, 'Проектов пока нет', 'Создайте проект для совместной работы.'); return; }
        data.items.forEach(project => {
            state.projects.set(project.id, project);
            const row = node('div', 'tm-inbox-row'); const content = node('div');
            content.append(button(project.name, () => navigate('project', {project: project.id}), 'tm-link'), node('p', '', `Владелец: ${name(project.owner_id)}`));
            row.append(content, node('span', 'tm-muted', `${project.counters.open} открыто · ${project.counters.in_progress} в работе`));
            if (project.counters.overdue) row.append(node('span', 'tm-overdue', `${project.counters.overdue} просрочено`)); root.append(row);
        });
    }
    function renderInbox(root, data) {
        if (!data.items.length) { empty(root, 'Входящих нет', 'Новые назначения появятся здесь.'); return; }
        data.items.forEach(event => {
            const micro = event.task_type === 'micro';
            const row = node('div', `tm-inbox-row${micro ? ' tm-inbox-micro' : ''}`); const content = node('div');
            row.dataset.inboxTask = event.task_id;
            content.append(node('strong', 'tm-inbox-kind', micro ? '⚡ МИКРОЗАДАЧА · 24 ЧАСА' : 'Обычная задача'), node('strong', '', event.title), node('p', '', `${event.event_type === 'task_reassigned' ? 'Переназначение' : 'Новое поручение'} от ${name(event.actor_id)} · ${displayInstant(event.created_at)}`));
            if (micro) {
                const timer = node('strong', 'tm-deadline', remaining(event.micro_deadline_at)); timer.dataset.microDeadline = event.micro_deadline_at;
                timer.classList.toggle('tm-overdue', Date.parse(event.micro_deadline_at) < now());
                content.append(timer, node('p', 'tm-muted', `Срок: ${displayInstant(event.micro_deadline_at)}. 24 часа от создания; остаётся во входящих до выполнения.`));
            }
            const open = button('Ознакомиться', async () => {
                open.disabled = true;
                try {
                    const task = await request(`/tasks/${event.task_id}`);
                    window.TasksModuleDialogs.preview(task);
                } catch (error) { showError(error); } finally { open.disabled = false; }
            });
            const act = button(micro ? 'Готово' : 'Взять в работу', async () => {
                act.disabled = true;
                try { await inboxAction({id: event.task_id, task_type: event.task_type, version: event.version}); }
                catch (error) { showError(error); await refresh(); }
                finally { act.disabled = false; }
            }, 'tm-primary');
            row.append(content, open, act); root.append(row);
        });
    }
    async function inboxAction(task) {
        const path = task.task_type === 'micro' ? `/microtasks/${task.id}/complete` : `/tasks/${task.id}/accept`;
        const result = await api.request(path, 'POST', {version: task.version});
        await refresh(); return result;
    }
    async function renderBoard(root, generation) {
        const base = listQuery(); delete base.status; base.offset = 0; base.limit = 30;
        const statuses = Object.keys(statusNames);
        const pages = await Promise.all(statuses.map(status => request('/tasks', Object.assign({}, base, {status}))));
        if (generation !== state.generation) return;
        root.replaceChildren(); const board = node('div', 'tm-board'); let dragging = null;
        statuses.forEach((status, index) => {
            const column = node('section', 'tm-board-column'); column.dataset.status = status;
            const header = node('h3', '', `${statusNames[status]} · ${pages[index].total}`); const cards = node('div');
            const append = tasks => tasks.forEach(task => {
                const card = node('article', 'tm-board-card'); card.dataset.taskId = task.id; card.draggable = task.permissions.change_status;
                card.addEventListener('dragstart', event => { dragging = task; event.dataTransfer.setData('text/plain', String(task.id)); event.dataTransfer.effectAllowed = 'move'; });
                card.addEventListener('dragend', () => { dragging = null; $$('.tm-drop-target').forEach(item => item.classList.remove('tm-drop-target')); });
                card.append(button(task.title, () => window.TasksModuleDialogs.task(task.id)), assignee(task.assigned_to), node('small', 'tm-muted', displayDate(task.deadline_date)));
                const select = node('select'); select.setAttribute('aria-label', `Статус: ${task.title}`); statuses.forEach(key => select.add(new Option(statusNames[key], key))); select.value = task.status; select.disabled = !task.permissions.change_status;
                select.addEventListener('change', () => { const target = select.value; select.value = task.status; changeStatus(task, target); }); card.append(select); cards.append(card);
            });
            append(pages[index].items); let offset = pages[index].items.length;
            const more = button('Ещё задачи', async () => {
                more.disabled = true; try { const page = await request('/tasks', Object.assign({}, base, {status, offset})); if (generation !== state.generation) return; append(page.items); offset += page.items.length; more.hidden = offset >= page.total; } catch (error) { showError(error); } finally { more.disabled = false; }
            }); more.hidden = offset >= pages[index].total;
            column.addEventListener('dragover', event => { if (dragging && dragging.status !== status) { event.preventDefault(); column.classList.add('tm-drop-target'); } });
            column.addEventListener('dragleave', () => column.classList.remove('tm-drop-target'));
            column.addEventListener('drop', event => { event.preventDefault(); column.classList.remove('tm-drop-target'); if (dragging && dragging.status !== status) changeStatus(dragging, status); dragging = null; });
            column.append(header, cards, more); board.append(column);
        });
        root.append(board); $('#tm-list-total').textContent = pages.reduce((sum, page) => sum + page.total, 0); $('#tm-pagination').hidden = true;
    }
    async function loadList() {
        const generation = ++state.generation; const root = $('#tm-list'); root.setAttribute('aria-busy', 'true'); $('#tm-error').hidden = !state.referenceError;
        if (state.referenceError) showError(state.referenceError);
        try {
            if (state.view === 'project') { const project = await request(`/projects/${state.project.id}`); if (generation !== state.generation) return; state.project = project; }
            syncChrome();
            if (state.view === 'project' && state.projectMode === 'board') { await renderBoard(root, generation); return; }
            const data = state.view === 'inbox' ? await request('/inbox', {limit: state.limit, offset: state.page * state.limit}) : state.view === 'projects' ? await request('/projects', {search: state.search, archived: String(state.projectsArchived), limit: state.limit, offset: state.page * state.limit}) : await request(state.view === 'micro' ? '/microtasks' : '/tasks', listQuery());
            if (generation !== state.generation) return;
            if (state.page > 0 && state.page * state.limit >= data.total) { state.page = Math.max(0, Math.ceil(data.total / state.limit) - 1); await loadList(); return; }
            root.replaceChildren(); $('#tm-list-total').textContent = data.total;
            if (state.view === 'inbox') renderInbox(root, data); else if (state.view === 'projects') renderProjects(root, data); else renderTasks(root, data);
            pagination(data);
        } catch (error) { if (generation === state.generation) { empty(root, 'Данные недоступны'); showError(error); } }
        finally { if (generation === state.generation) root.setAttribute('aria-busy', 'false'); }
    }
    async function refresh(references = false) {
        if (references && !await referenceData()) return;
        await Promise.all([loadList(), summaries().catch(showError), microPreview().catch(error => showError(error, '#tm-micro-error'))]);
    }
    function navigate(view, options = {}, push = true) {
        clearTimeout(searchTimer); searchGeneration += 1;
        notice('');
        state.view = Object.hasOwn(viewNames, view) ? view : 'main'; state.scope = options.scope || (['archive', 'project', 'delegated_waiting'].includes(state.view) ? 'all' : 'my');
        state.filters = options.filters || {}; state.page = 0; state.search = options.search || ''; $('#tm-search').value = state.search;
        state.project = state.view === 'project' ? {id: Number(options.project)} : null; state.projectMode = 'list';
        if (state.view === 'project' && !(state.project.id > 0)) state.view = 'projects';
        $('#tm-filters').reset(); $('#tm-filters').hidden = true; $('#tm-filters-toggle').setAttribute('aria-expanded', 'false');
        $$('[data-period]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.period === 'all')));
        Object.entries(state.filters).forEach(([key, value]) => { const control = $(`[name="${key}"]`, $('#tm-filters')); if (control) control.value = value; });
        if (push) { const params = new URLSearchParams({view: state.view}); if (state.project) params.set('project', state.project.id); history.pushState(null, '', '?' + params); }
        loadList();
    }
    function applyFilters() {
        state.filters = {};
        for (const [key, value] of new FormData($('#tm-filters'))) {
            const control = $(`[name="${key}"]`, $('#tm-filters')); if (!value || control.closest('[hidden]')) continue;
            if (key === 'project_id' && value === 'none') state.filters.project = 'none'; else state.filters[key] = value;
        }
        syncPeriods(); state.page = 0; loadList();
    }
    function syncPeriods() {
        const start = $('#tm-filters [name=date_from]').value, end = $('#tm-filters [name=date_to]').value;
        $$('[data-period]').forEach(item => {
            const date = new Date(today() + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() - Number(item.dataset.period) + 1);
            const matches = item.dataset.period === 'all' ? !start && !end : end === today() && start === date.toISOString().slice(0, 10);
            item.setAttribute('aria-pressed', String(matches));
        });
    }
    async function initialize() {
        collapseMicro(microCollapsed);
        $$('[data-view]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); navigate(link.dataset.view); }));
        $$('[data-scope]').forEach(item => item.addEventListener('click', () => { state.scope = item.dataset.scope; state.page = 0; loadList(); }));
        $$('[data-micro-scope]').forEach(item => item.addEventListener('click', () => { state.microScope = item.dataset.microScope; microPreview().catch(error => showError(error, '#tm-micro-error')); }));
        $$('[data-metric]').forEach(item => item.addEventListener('click', () => { const key = item.dataset.metric; navigate(key === 'in_progress' ? 'main' : key, key === 'in_progress' ? {filters: {status: 'in_progress'}} : {}); }));
        $('#tm-micro-toggle').addEventListener('click', () => collapseMicro(!microCollapsed));
        $('#tm-micro-add').addEventListener('click', () => { collapseMicro(false); $('#tm-micro-form input').focus(); });
        $('#tm-micro-form').addEventListener('submit', async event => {
            event.preventDefault(); const form = event.currentTarget; const submit = $('button', form); if (submit.disabled) return; submit.disabled = true; $('#tm-micro-error').hidden = true;
            try { await api.request('/microtasks', 'POST', {title: form.elements.title.value, assigned_to: Number(form.elements.assigned_to.value)}); form.elements.title.value = ''; await refresh(); }
            catch (error) { showError(error, '#tm-micro-error'); } finally { submit.disabled = false; }
        });
        $('#tm-create').addEventListener('click', () => window.TasksModuleDialogs.create(state.view === 'micro', state.view === 'project' ? state.project.id : null));
        $('#tm-new-project').addEventListener('click', () => window.TasksModuleDialogs.project());
        $('#tm-project-settings').addEventListener('click', () => window.TasksModuleDialogs.project(state.project.id));
        $('#tm-projects-archived').addEventListener('change', event => { state.projectsArchived = event.target.checked; state.page = 0; loadList(); });
        $$('[data-project-mode]').forEach(item => item.addEventListener('click', () => { state.projectMode = item.dataset.projectMode; state.page = 0; loadList(); }));
        $('#tm-filters-toggle').addEventListener('click', () => { const form = $('#tm-filters'); form.hidden = !form.hidden; $('#tm-filters-toggle').setAttribute('aria-expanded', String(!form.hidden)); });
        $('#tm-filters').addEventListener('submit', event => { event.preventDefault(); applyFilters(); });
        $('#tm-reset-filters').addEventListener('click', () => { $('#tm-filters').reset(); syncPeriods(); state.filters = {}; state.page = 0; loadList(); });
        ['date_from', 'date_to'].forEach(field => $(`#tm-filters [name=${field}]`).addEventListener('change', syncPeriods));
        $$('[data-period]').forEach(item => item.addEventListener('click', () => {
            const period = item.dataset.period; const end = today(); let start = '';
            if (period !== 'all') { const date = new Date(end + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() - Number(period) + 1); start = date.toISOString().slice(0, 10); }
            $('#tm-filters [name=date_from]').value = start; $('#tm-filters [name=date_to]').value = period === 'all' ? '' : end;
            $$('[data-period]').forEach(other => other.setAttribute('aria-pressed', String(other === item))); applyFilters();
        }));
        $('#tm-search').addEventListener('input', event => {
            clearTimeout(searchTimer); const value = event.target.value; const generation = ++searchGeneration;
            searchTimer = setTimeout(() => {
                if (generation !== searchGeneration || $('#tm-search').value !== value) return;
                state.search = value; state.page = 0; loadList();
            }, 250);
        });
        $('#tm-prev').addEventListener('click', () => { if (state.page) { state.page -= 1; loadList(); } });
        $('#tm-next').addEventListener('click', () => { state.page += 1; loadList(); });
        $('#tm-retry').addEventListener('click', () => refresh(true));
        const fromURL = () => { const params = new URLSearchParams(location.search); navigate(params.get('view') || 'main', {project: params.get('project')}, false); const id = Number(params.get('task')); if (Number.isSafeInteger(id) && id > 0) window.TasksModuleDialogs.task(id); const previewId = Number(params.get('preview')); if (Number.isSafeInteger(previewId) && previewId > 0) request(`/tasks/${previewId}`).then(task => window.TasksModuleDialogs.preview(task)).catch(showError); };
        window.addEventListener('popstate', fromURL);
        await referenceData();
        fromURL(); await Promise.all([summaries().catch(showError), microPreview().catch(error => showError(error, '#tm-micro-error'))]);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(true); });
        const clockTimer = setInterval(() => {
            if (document.hidden) return;
            $$('[data-micro-deadline]').forEach(item => { item.textContent = remaining(item.dataset.microDeadline); item.classList.toggle('tm-overdue', Date.parse(item.dataset.microDeadline) < now()); });
            if (state.businessDate && state.businessDate !== today()) refresh(true);
        }, 60000);
        window.addEventListener('pagehide', () => clearInterval(clockTimer), {once: true});
    }
    window.TasksModuleUI = {$, $$, node, button, state, request, name, projectName, assignee, statusNames, userOptions, projectOptions,
        displayDate, displayInstant, remaining, showError, notice, refresh, navigate, allPages, boot, inboxAction};
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
})();
