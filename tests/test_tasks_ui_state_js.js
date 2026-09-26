/* Controlled DOM/transport harness executing the unmodified production controller.
 * No real network, timers or database. Browser integration is tested separately. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../app/static/js/tasks-module.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.options = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.attributes = {}; this.classList = {toggle() {}, remove() {}, add() {}}; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.options = []; }
    add(option) { this.options.push(option); }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(key, fn) { this.listeners[key] = fn; }
    querySelector() { if (!this.child) this.child = new Element('span'); return this.child; }
    reset() {} closest() { return null; }
}
function harness() {
    const elements = new Map(), listeners = {}, timers = new Map(), cancelled = [], calls = [], shown = [];
    let timerId = 0;
    const element = selector => { if (!elements.has(selector)) elements.set(selector, new Element(selector)); return elements.get(selector); };
    const periods = ['30', '90', '365', 'all'].map(period => { const item = new Element('button'); item.dataset.period = period; return item; });
    element('#tm-filters').reset = () => ['date_from', 'date_to'].forEach(key => { element('#tm-filters [name=' + key + ']').value = ''; });
    const env = {calls, shown, element, timers, cancelled};
    env.periods = periods;
    env.respond = async url => {
        const parsed = new URL(url, 'https://test.invalid');
        if (parsed.pathname === '/directory') return {items: [{id: 1, name: 'Initial'}], server_now: new Date().toISOString(), business_date: '2026-09-27'};
        if (parsed.pathname === '/tasks/summary') return {inbox: 1};
        if (parsed.pathname === '/microtasks/summary') return {};
        if (parsed.pathname === '/inbox') return {items: [{id: 7, task_id: 8, payload: {title: 'Assignment'}, actor_id: 2, created_at: '2026-09-27T00:00:00Z'}], total: 1};
        if (parsed.pathname === '/tasks/8') return {id: 8, title: 'Task'};
        if (parsed.pathname === '/inbox/7/read') throw {status: 503, message: 'Unavailable'};
        return {items: [], total: 0};
    };
    const document = {querySelector: element, querySelectorAll: selector => selector === '[data-period]' ? periods : [], createElement: tag => new Element(tag),
        addEventListener: (key, fn) => { listeners[key] = fn; }};
    const sandbox = {document, URLSearchParams, URL, Date, Intl, Map, Set, Option: function(text, value) { this.text = text; this.value = value; },
        localStorage: {getItem() {}, setItem() {}}, history: {pushState() {}}, location: {search: ''},
        setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => { if (timers.has(id)) cancelled.push(timers.get(id)); timers.delete(id); },
        setInterval: () => 1, clearInterval() {}, FormData: function() { return []; },
        window: {TASKS_MODULE_BOOTSTRAP: {userId: 1}, addEventListener() {}, TasksModuleDialogs: {showTask: task => shown.push(task)},
            TasksModuleAPI: {query: values => '?' + new URLSearchParams(values), request: (...args) => { calls.push(args); return env.respond(...args); }}}};
    vm.runInNewContext(code, sandbox);
    env.ui = sandbox.window.TasksModuleUI;
    env.initialize = () => listeners.DOMContentLoaded();
    return env;
}
async function inboxFailure() {
    const h = harness(); await h.initialize(); h.ui.navigate('inbox'); await flush();
    const row = h.element('#tm-list').children[0];
    await row.children.find(child => child.textContent === 'Открыть').listeners.click();
    assert.equal(h.shown.length, 1); assert.equal(h.shown[0].id, 8);
    assert.equal(h.element('#tm-list').children[0], row, 'Failed read must leave pending event visible');
    assert.match(h.element('#tm-notice').textContent, /осталось во входящих/);
    assert.equal(h.calls.filter(call => call[0].startsWith('/inbox/7/read')).length, 1);
}
async function searchNavigation() {
    const h = harness(); await h.initialize();
    const input = h.element('#tm-search'); input.value = 'foo'; input.listeners.input({target: input});
    const staleCallback = Array.from(h.timers.values())[0];
    h.ui.navigate('archive'); await flush();
    assert.equal(h.timers.size, 0); assert.equal(h.cancelled.length, 1);
    staleCallback(); await flush(); // Even a queued callback that evaded cancellation is invalidated.
    assert.equal(h.ui.state.search, ''); assert.equal(input.value, '');
    assert.equal(new URL(h.calls.filter(call => call[0].startsWith('/tasks?')).at(-1)[0], 'https://test.invalid').searchParams.get('search'), '');
}
async function referencesOutOfOrder(oldFails) {
    const h = harness(); await h.initialize();
    const original = h.respond, old = deferred(), fresh = deferred(), oldProjects = deferred(), freshProjects = deferred();
    let directoryCalls = 0, projectCalls = 0;
    h.respond = url => {
        if (url.startsWith('/directory')) return ++directoryCalls === 1 ? old.promise : fresh.promise;
        if (url.startsWith('/projects?') && !url.includes('archived')) return ++projectCalls === 1 ? oldProjects.promise : freshProjects.promise;
        return original(url);
    };
    const olderRefresh = h.ui.refresh(true), newerRefresh = h.ui.refresh(true);
    fresh.resolve({items: [{id: 1, name: 'Newest'}], business_date: '2026-09-28', server_now: '2026-09-28T10:00:00Z'});
    freshProjects.resolve({items: [{id: 2, name: 'New project', counters: {open: 0}}], total: 1});
    await newerRefresh;
    const offset = h.ui.state.clockOffset;
    if (oldFails) old.reject({status: 503, message: 'Old failure'});
    else old.resolve({items: [{id: 1, name: 'Stale'}], business_date: '2026-09-27', server_now: '2026-09-27T10:00:00Z'});
    oldProjects.resolve({items: [{id: 3, name: 'Stale project', counters: {open: 0}}], total: 1});
    await olderRefresh;
    assert.equal(h.ui.state.users[0].name, 'Newest'); assert.equal(h.ui.state.businessDate, '2026-09-28');
    assert.equal(h.ui.state.clockOffset, offset); assert.equal(h.ui.state.referenceError, null);
    assert.equal(h.ui.state.projects.has(2), true); assert.equal(h.ui.state.projects.has(3), false);
    assert.equal(h.ui.state.activeProjects[0].id, 2);
    assert.equal(h.element('#tm-project-preview').children[0].children[1].textContent, 'New project');
    assert.equal(h.element('#tm-people').children[0].children[0].children[1].textContent, 'Newest');
}
async function finalPolish() {
    const h = harness(); await h.initialize(); h.ui.navigate('archive'); await flush();
    h.periods[0].listeners.click(); await flush();
    assert.equal(h.periods[0].attributes['aria-pressed'], 'true');
    h.element('#tm-filters [name=date_from]').value = '2020-01-01';
    h.element('#tm-filters [name=date_from]').listeners.change();
    assert.ok(h.periods.every(item => item.attributes['aria-pressed'] === 'false'));
    h.element('#tm-reset-filters').listeners.click(); await flush();
    assert.equal(h.periods[3].attributes['aria-pressed'], 'true');
    assert.equal(h.periods[0].attributes['aria-pressed'], 'false');
    h.ui.notice('Previous success'); h.ui.navigate('main'); await flush();
    assert.equal(h.element('#tm-notice').hidden, true);
    const stamp = Date.now() + h.ui.state.clockOffset;
    const remaining = offset => h.ui.remaining(new Date(stamp + offset).toISOString());
    assert.equal(remaining(-1000), 'Просрочено меньше минуты');
    assert.equal(remaining(-61000), 'Просрочено 1 мин');
    assert.equal(remaining(-3599000), 'Просрочено 59 мин');
    assert.equal(remaining(-3661000), 'Просрочено 1 ч 1 м');
    assert.equal(remaining(61000), '2 мин');
}
async function microRecovery() {
    const h = harness(); await h.initialize(); const original = h.respond;
    h.respond = url => url.startsWith('/microtasks') ? Promise.reject({status: 503, message: 'Unavailable'}) : original(url);
    await h.ui.refresh(); assert.equal(h.element('#tm-micro-error').hidden, false);
    assert.equal(h.element('#tm-micro-list').children[0].children[0].textContent, 'Микрозадачи недоступны');
    h.respond = original; await h.ui.refresh(); assert.equal(h.element('#tm-micro-error').hidden, true);
    const old = deferred(); let calls = 0;
    h.respond = url => url.startsWith('/microtasks?') && ++calls === 1 ? old.promise : original(url);
    const stale = h.ui.refresh(); await h.ui.refresh(); old.reject({status: 503, message: 'Old error'}); await stale;
    assert.equal(h.element('#tm-micro-error').hidden, true);
}
async function projectCountersAfterStatus() {
    const h = harness(), original = h.respond; let done = false;
    h.respond = (url, method) => {
        if (url === '/tasks/8/status' && method === 'POST') { done = true; return Promise.resolve({}); }
        if (url.startsWith('/tasks?')) return Promise.resolve({items: done ? [] : [{id: 8, title: 'Project task', task_type: 'normal',
            assigned_to: 1, project_id: 1, status: 'new', priority: 'normal', version: 1, permissions: {change_status: true}}], total: done ? 0 : 1});
        if (url.startsWith('/projects?') && !url.includes('archived')) return Promise.resolve({items: [{id: 1, name: 'Project', counters: {open: done ? 0 : 1}}], total: 1});
        return original(url, method);
    };
    await h.initialize(); await flush();
    assert.equal(h.ui.state.activeProjects[0].counters.open, 1);
    const checkbox = h.element('#tm-list').children[0].children[1].children[0];
    checkbox.checked = true; checkbox.listeners.change(); await flush();
    assert.equal(done, true);
    assert.equal(h.ui.state.activeProjects[0].counters.open, 0);
    assert.equal(h.element('#tm-project-preview').children[0].children[2].textContent, '0 открыто');
}
(async () => {
    await inboxFailure(); await searchNavigation(); await referencesOutOfOrder(false); await referencesOutOfOrder(true);
    await finalPolish(); await microRecovery(); await projectCountersAfterStatus();
    console.log('Tasks UI state regressions: 7 checks PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
