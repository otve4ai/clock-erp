const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');

function element(dataset = {}) {
    return {dataset, hidden: true, textContent: '', attributes: {}, listeners: {},
        classList: {toggle() {}},
        setAttribute(key, value) {this.attributes[key] = value;},
        addEventListener(key, callback) {this.listeners[key] = callback;},
        focus() {this.focused = true;},
    };
}

async function fixture(initial = {outcome: 'success', last_success_at: Date.now() / 1000}) {
    const fields = Object.fromEntries(['orders-sync-toggle', 'orders-sync-panel', 'orders-sync-label',
        'sync-latest', 'ttt-diagnostic', 'sync-diagnostics', 'wb-recovery-message'].map(key => [key, element()]));
    const rows = ['tictactoy', 'wildberries'].map(source => {
        const row = element({syncRow: source, state: 'unknown'});
        const state = element(), time = element();
        row.querySelector = selector => selector === '[data-sync-state]' ? state : time;
        return row;
    });
    const buttons = ['all', 'tictactoy', 'wildberries'].map(source => element({syncSource: source}));
    const root = element({csrf: 'test'});
    root.querySelector = selector => fields[selector.slice(6, -1)];
    root.querySelectorAll = selector => selector === '[data-sync-row]' ? rows : selector === '[data-sync-source]' ? buttons : [];
    root.contains = target => Object.values(fields).includes(target) || buttons.includes(target);
    const document = element();
    document.querySelector = selector => selector === '[data-orders-sync]' ? root : null;
    document.dispatchEvent = event => document.listeners[event.type]?.(event);
    const requests = [];
    let responder = async () => ({ok: true, result: initial});
    const fetch = async (url, options) => {
        requests.push({url, ...options});
        const payload = await responder(url, options);
        return {ok: payload.ok, status: payload.ok ? 200 : 503, json: async () => payload};
    };
    const context = {document, window: {setInterval() {}}, fetch, Intl, Date, Event};
    runInNewContext(readFileSync(require.resolve('../app/static/js/wb-sync-status.js'), 'utf8'), context);
    runInNewContext(readFileSync(require.resolve('../app/static/js/orders-sync.js'), 'utf8'), context);
    await new Promise(setImmediate);
    return {root, rows, fields, buttons, document, requests,
        respond(callback) {responder = callback;},
        wb(data) {document.dispatchEvent({type: 'orders:wb-diagnostics', detail: data});},
    };
}

test('aggregate covers unknown, success, stale, partial, running and error without invented success', async () => {
    const f = await fixture({outcome: 'unknown'});
    f.wb({outcome: 'unknown'});
    assert.equal(f.root.dataset.syncState, 'unknown');
    const g = await fixture();
    g.wb({outcome: 'success', last_success_at: Date.now() / 1000});
    assert.equal(g.root.dataset.syncState, 'success');
    g.wb({outcome: 'success', last_success_at: (Date.now() - 16 * 60000) / 1000});
    assert.equal(g.root.dataset.syncState, 'attention');
    g.wb({outcome: 'partial', last_success_at: Date.now() / 1000});
    assert.equal(g.root.dataset.syncState, 'attention');
    g.wb({outcome: 'running'});
    assert.equal(g.root.dataset.syncState, 'running');
    g.wb({outcome: 'error'});
    assert.equal(g.root.dataset.syncState, 'error');
    assert.match(g.fields['orders-sync-label'].textContent, /Ошибка/);
});

test('WB product remarks are separate from sync failures and clear on next diagnostics', async () => {
    const f = await fixture();
    const healthy = {outcome: 'success', full_outcome: 'success', last_success_at: Date.now() / 1000, attention: 63};
    f.wb(healthy);
    assert.equal(f.root.dataset.syncState, 'success');
    for (const problem of [{pending: [{error: 'details unavailable'}]}, {errors: [{error: 'API'}]},
        {full_errors: [{error: 'API'}]}, {error_count: 1}, {full_outcome: 'partial'}, {full_outcome: 'error'}]) {
        f.wb({...healthy, ...problem});
        assert.equal(f.root.dataset.syncState, 'attention', JSON.stringify(problem));
    }
    f.wb({...healthy, attention: 0, missing: ['imported-now'], supplies: [{missing: 1}]});
    assert.equal(f.root.dataset.syncState, 'success');
    f.wb({attention: 63});
    assert.equal(f.root.dataset.syncState, 'unknown');
});

test('manual WB refresh renders nested diagnostics immediately after success', async () => {
    const f = await fixture();
    f.respond(async () => ({ok: true, result: {outcome: 'success', recovery: {
        outcome: 'success', full_outcome: 'success', last_success_at: new Date().toISOString(), attention: 63,
    }}}));
    await f.buttons[2].listeners.click();
    assert.equal(f.root.dataset.syncState, 'success');
});

test('open/close never starts sync; Escape returns focus and outside click closes', async () => {
    const f = await fixture();
    const toggle = f.fields['orders-sync-toggle'];
    toggle.listeners.click();
    assert.equal(f.fields['orders-sync-panel'].hidden, false);
    assert.equal(toggle.attributes['aria-expanded'], 'true');
    f.document.dispatchEvent({type: 'keydown', key: 'Escape'});
    assert.equal(f.fields['orders-sync-panel'].hidden, true);
    assert.equal(toggle.focused, true);
    toggle.listeners.click();
    f.document.dispatchEvent({type: 'click', target: {}});
    assert.equal(f.fields['orders-sync-panel'].hidden, true);
    assert.equal(f.requests.filter(request => request.method === 'POST').length, 0);
});

test('refresh all preserves requests, duplicate guard and error over concurrent running state', async () => {
    const f = await fixture();
    let finish;
    const waiting = new Promise(resolve => {finish = resolve;});
    f.respond(async url => {
        if (url.includes('wildberries')) await waiting;
        return url.includes('tictactoy') ? {ok: false, error: {message: 'Failure'}}
            : {ok: true, result: {outcome: 'success', last_success_at: Date.now() / 1000}};
    });
    const refresh = f.buttons[0].listeners.click();
    assert.equal(f.root.dataset.syncState, 'running');
    assert.equal(f.buttons[0].disabled, true);
    await f.buttons[0].listeners.click();
    await new Promise(setImmediate);
    assert.equal(f.root.dataset.syncState, 'error');
    finish();
    await refresh;
    assert.equal(f.root.dataset.syncState, 'error');
    assert.equal(f.buttons[0].disabled, false);
    const posts = f.requests.filter(request => request.method === 'POST');
    assert.deepEqual(posts.map(request => request.url).sort(), ['/api/orders/tictactoy/sync', '/api/orders/wildberries/sync']);
    assert.ok(posts.every(request => request.headers['X-CSRF-Token'] === 'test'));
    assert.notEqual(f.fields['sync-latest'].textContent, 'Нет данных');
});

test('status label follows selection and replaced AJAX filters; dropdown closes with focus', async () => {
    const f = await fixture();
    const label = element(), summary = element();
    let count = element();
    count.textContent = '7504';
    let selected = {firstChild: {textContent: 'Собран '}, querySelector: () => count};
    let statuses = {...element(), open: true, contains: () => false,
        querySelector: selector => selector === '[aria-pressed="true"]' ? selected : selector === 'summary' ? summary : label};
    f.document.querySelector = selector => selector === '[data-status-more]' ? statuses : f.root;
    f.document.dispatchEvent({type: 'orders:status-selected'});
    assert.equal(label.textContent, 'Статус: Собран · 7504');
    f.document.dispatchEvent({type: 'keydown', key: 'Escape'});
    assert.equal(statuses.open, false);
    assert.equal(summary.focused, true);
    statuses = {...statuses, open: true};
    selected = {firstChild: {textContent: 'Все'}, querySelector: () => null};
    f.document.dispatchEvent({type: 'orders:filters-updated'});
    assert.equal(label.textContent, 'Статус: Все');
    f.document.dispatchEvent({type: 'click', target: {}});
    assert.equal(statuses.open, false);
});
