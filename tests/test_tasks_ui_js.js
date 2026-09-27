/* Run with node. Network adapters only; browser workflows are tested separately. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..', 'app', 'static', 'js');
const response = (data, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => data});
let checks = 0;
async function apiTests() {
    const calls = [];
    const sandbox = {AbortController, URLSearchParams, window: {TASKS_MODULE_BOOTSTRAP: {csrf: 'synthetic-csrf'}, setTimeout, clearTimeout},
        fetch: async (...args) => { calls.push(args); return response({data: {id: 3}}); }};
    vm.runInNewContext(fs.readFileSync(path.join(root, 'tasks-module-api.js'), 'utf8'), sandbox);
    const api = sandbox.window.TasksModuleAPI;
    assert.equal((await api.request('/tasks', 'POST', {title: '🙂 العربية 日本語'})).id, 3);
    assert.equal(calls[0][0], '/api/v1/tasks-module/tasks');
    assert.equal(calls[0][1].headers['X-CSRF-Token'], 'synthetic-csrf');
    assert.equal(calls[0][1].credentials, 'same-origin');
    assert.equal(JSON.parse(calls[0][1].body).title, '🙂 العربية 日本語'); checks += 1;
    const query = api.query({search: '&scope=all<script>', project_id: '', status: null, offset: 0});
    assert.equal(new URLSearchParams(query).get('search'), '&scope=all<script>');
    assert.equal(new URLSearchParams(query).get('scope'), null); checks += 1;
    for (const status of [400, 401, 403, 404, 409, 413, 422, 503]) {
        sandbox.fetch = async () => response({message: 'secret SQL /internal/path', fields: {title: 'Invalid'}}, status);
        await assert.rejects(api.request('/tasks'), failure => failure.status === status && !failure.message.includes('secret') && !failure.message.includes('/internal'));
        checks += 1;
    }
    sandbox.fetch = async () => { const error = new Error('private detail'); error.name = 'AbortError'; throw error; };
    await assert.rejects(api.request('/tasks'), error => error.message.includes('Сервер не ответил')); checks += 1;
}
async function notificationTests() {
    let calls = [], notifications = [], time = 100000;
    const elements = [{hidden: true, textContent: ''}, {hidden: true, textContent: '', dataset: {tasksModuleBadge: 'micro'}}]; const listeners = {};
    const sandbox = {AbortController, Number, Date: {now: () => time}, document: {visibilityState: 'visible', querySelectorAll: () => elements, addEventListener: (type, callback) => { listeners[type] = callback; }},
        window: {ERP_TASKS_OPTIONAL: {csrf: 'synthetic'}, setTimeout, clearTimeout, VechasuNotify: {info: (title, options) => notifications.push({title, options})}},
        fetch: async (url, options) => {
            calls.push({url, options});
            if (url.endsWith('/badge')) return response({data: {count: 5, normal: 2, micro: 3}});
            if (url.endsWith('/directory')) return response({data: {items: [{id: 2, name: 'Actor'}]}});
            return response({data: {items: [{task_id: 8, actor_id: 2, title: '<img src=x>', task_type: 'normal'}]}});
        }};
    const code = fs.readFileSync(path.join(root, 'tasks-module-notifications.js'), 'utf8');
    vm.runInNewContext(code, sandbox);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements[0].textContent, '2'); assert.equal(elements[0].hidden, false);
    assert.equal(elements[1].textContent, '⚡'); assert.equal(elements[1].hidden, false);
    assert.equal(notifications.length, 1); assert.equal(notifications[0].options.detail, 'От Actor · <img src=x>');
    assert.equal(notifications[0].options.action.href, '/app/tasks-module?view=inbox&preview=8');
    assert.equal(calls.find(item => item.url.endsWith('/claim')).options.headers['X-CSRF-Token'], 'synthetic'); checks += 1;
    listeners.visibilitychange(); await new Promise(resolve => setImmediate(resolve)); assert.equal(calls.length, 3); checks += 1;
    vm.runInNewContext(code, sandbox); await new Promise(resolve => setImmediate(resolve)); assert.equal(calls.length, 3); checks += 1;
    time += 31000; sandbox.fetch = async url => response({data: url.endsWith('/badge') ? {count: 0, normal: 0, micro: 0} : {items: []}});
    listeners.visibilitychange(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements[1].hidden, true); assert.equal(elements[1].textContent, '');
    time += 31000; sandbox.fetch = async () => { throw new Error('offline'); };
    listeners.visibilitychange(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements[0].hidden, true); assert.equal(notifications.length, 1); checks += 1;
}
(async () => { await apiTests(); await notificationTests(); console.log('Tasks UI adapters: ' + checks + ' checks PASS'); })().catch(error => { console.error(error); process.exitCode = 1; });
