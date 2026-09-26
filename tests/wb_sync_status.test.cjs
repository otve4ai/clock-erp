const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');

test('recovery diagnostics separate remarks while retaining pending, API and full-sync failures', async () => {
    const fields = new Map();
    const field = selector => {
        if (!fields.has(selector)) fields.set(selector, {hidden: true, textContent: '',
            addEventListener() {}, classList: {toggle(key, value) {this[key] = value;}}});
        return fields.get(selector);
    };
    const root = {dataset: {}, querySelector: field};
    const listeners = {};
    const document = {querySelector: () => root, dispatchEvent() {},
        addEventListener(name, callback) {listeners[name] = callback;}};
    let diagnostics = {outcome: 'success', full_outcome: 'success', last_success_at: new Date().toISOString(), attention: 63};
    const context = {document, window: {setInterval() {}}, Intl, Date,
        CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
        fetch: async () => ({ok: true, json: async () => ({ok: true, diagnostics})})};
    for (const script of ['wb-sync-status', 'wb-recovery']) {
        runInNewContext(readFileSync(require.resolve(`../app/static/js/${script}.js`), 'utf8'), context);
    }
    await new Promise(setImmediate);
    assert.match(field('[data-wb-health]').textContent, /^Синхронизирован/);
    assert.equal(field('[data-wb-diagnostic]').classList.warning, false);
    for (const issue of [{pending: [{error: 'API'}]}, {errors: [{error: 'API'}]},
        {full_outcome: 'error'}, {error_count: 1}, {last_success_at: 'invalid'},
        {last_success_at: new Date(Date.now() - 16 * 60000).toISOString()}]) {
        const original = diagnostics;
        diagnostics = {...original, ...issue};
        await listeners['orders:wb-refresh']();
        assert.match(field('[data-wb-health]').textContent, /^Требует внимания/);
        assert.equal(field('[data-wb-diagnostic]').classList.warning, true);
        diagnostics = original;
    }
    diagnostics = {...diagnostics, outcome: 'running'};
    await listeners['orders:wb-refresh']();
    assert.equal(field('[data-wb-health]').textContent, 'Обновляется…');
    diagnostics = {...diagnostics, outcome: 'success', attention: 0};
    await listeners['orders:wb-refresh']();
    assert.match(field('[data-wb-health]').textContent, /^Синхронизирован/);
});
