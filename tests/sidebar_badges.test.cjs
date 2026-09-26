const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');

const source = readFileSync(require.resolve('../app/static/js/sidebar-badges.js'), 'utf8');
const flush = () => new Promise(setImmediate);

function fixture(respond, readyState = 'loading', kinds = ['tasks', 'inbox']) {
    const nodes = kinds.map(kind => ({
        dataset: {sidebarBadge: kind}, hidden: true, textContent: '', attributes: {},
        setAttribute(key, value) { this.attributes[key] = value; },
    }));
    const requests = [], listeners = {}, timers = new Map();
    let nextTimer = 0;
    const window = {
        addEventListener(name, callback) { listeners[name] = callback; },
        setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
    };
    const context = {
        window, AbortController,
        document: {readyState, querySelectorAll(selector) {
            assert.equal(selector, '[data-sidebar-badge]');
            return nodes;
        }},
        fetch: async (url, options) => {
            requests.push({url, options});
            return respond(url, options);
        },
    };
    runInNewContext(source, context);
    return {nodes, requests, listeners, timers, context};
}

test('E: badge HTTP failure stays local while the other badge renders', async () => {
    const f = fixture(async url => url.includes('/tasks/') ? {ok: false} : {
        ok: true, json: async () => ({data: {count: 3}}),
    });
    f.listeners.load();
    await flush();
    assert.equal(f.nodes[0].hidden, true);
    assert.equal(f.nodes[1].textContent, '3');
    assert.equal(f.nodes[1].hidden, false);
    assert.equal(f.timers.size, 0);
});

test('desktop and mobile copies share one request per badge', async () => {
    const f = fixture(async () => ({ok: true, json: async () => ({data: {count: 2}})}),
        'loading', ['tasks', 'inbox', 'tasks', 'inbox']);
    f.listeners.load();
    await flush();
    assert.equal(f.requests.length, 2);
    assert.ok(f.nodes.every(node => !node.hidden && node.textContent === '2'));
});

test('E: rejected network request and invalid JSON do not escape to global handlers', async () => {
    const f = fixture(async url => {
        if (url.includes('/tasks/')) throw new Error('offline');
        return {ok: true, json: async () => { throw new Error('invalid JSON'); }};
    });
    f.listeners.load();
    await flush();
    assert.ok(f.nodes.every(node => node.hidden));
    assert.equal(f.timers.size, 0);
});

test('requests only start after window load and never repeat on duplicate script inclusion', async () => {
    const f = fixture(async () => ({ok: true, json: async () => ({data: {count: 2}})}));
    assert.equal(f.requests.length, 0);
    runInNewContext(source, f.context);
    f.listeners.load();
    await flush();
    assert.equal(f.requests.length, 2);
    assert.equal(f.requests[0].options.headers['X-Vechasu-Notify'], 'off');
    assert.equal(f.requests[0].options.credentials, 'same-origin');
});

test('hanging request is aborted without changing its badge or producing an unhandled rejection', async () => {
    const f = fixture((url, options) => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }));
    f.listeners.load();
    const timeouts = [...f.timers.values()];
    assert.equal(timeouts.length, 2);
    timeouts.forEach(timer => { assert.equal(timer.delay, 1500); timer.callback(); });
    await flush();
    assert.ok(f.nodes.every(node => node.hidden));
    assert.equal(f.timers.size, 0);
});

test('zero and malformed counts remain hidden; valid counts get accessible labels', async () => {
    for (const count of [0, -1, '3', null, 1.5]) {
        const f = fixture(async () => ({ok: true, json: async () => ({data: {count}})}));
        f.listeners.load();
        await flush();
        assert.ok(f.nodes.every(node => node.hidden));
    }
    const f = fixture(async () => ({ok: true, json: async () => ({data: {count: 1200}})}));
    f.listeners.load();
    await flush();
    assert.equal(f.nodes[0].textContent, '999+');
    assert.match(f.nodes[0].attributes['aria-label'], /1200/);
});

test('script loaded after window load starts asynchronously', async () => {
    const f = fixture(async () => ({ok: true, json: async () => ({data: {count: 1}})}), 'complete');
    assert.equal(f.requests.length, 0);
    const startup = [...f.timers.entries()][0];
    f.timers.delete(startup[0]);
    assert.equal(startup[1].delay, 0);
    startup[1].callback();
    await flush();
    assert.equal(f.requests.length, 2);
});
