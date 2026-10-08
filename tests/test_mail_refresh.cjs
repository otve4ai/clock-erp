const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

function fixture() {
    const source = fs.readFileSync('app/static/js/mail.js', 'utf8');
    const pending = [];
    const nodes = new Map();
    const node = (key) => {
        if (!nodes.has(key)) nodes.set(key, {
            value: '', hidden: false, innerHTML: 'saved messages', textContent: '',
            setAttribute() {}, querySelector: node, querySelectorAll: () => [],
        });
        return nodes.get(key);
    };
    const context = vm.createContext({
        URLSearchParams, document: {hidden: false}, workspace: node('workspace'),
        list: node('list'), syncStatus: node('syncStatus'), serverWarning: node('warning'),
        boot: {}, view: 'inbox', page: 1, loadVersion: 0, loading: false,
        $: node, api: () => new Promise((resolve, reject) => pending.push({resolve, reject})),
        emptyListMarkup: () => 'empty', formatDate: String, escapeHtml: String,
        statusLabels: {}, setConnected() {},
    });
    const start = source.indexOf('    async function load(');
    vm.runInContext(source.slice(start, source.indexOf('    const drawerTriggers', start)), context);
    return {context, pending, node};
}

const result = {account: {enabled: true, initial_sync_complete: true}, rows: [], pages: 1, page: 1};

test('background refresh keeps messages visible while pending and on failure, then recovers', async () => {
    const {context, pending, node} = fixture();
    const first = context.load(true);
    assert.equal(node('list').innerHTML, 'saved messages');
    await context.load(true);
    assert.equal(pending.length, 1);
    pending[0].reject(new Error('offline'));
    await first;
    assert.equal(node('list').innerHTML, 'saved messages');
    assert.match(node('syncStatus').textContent, /Повторим автоматически/);
    const retry = context.load(true);
    pending[1].resolve(result);
    await retry;
    assert.equal(node('list').innerHTML, 'empty');
});

test('a stale background response cannot overwrite a new filter result', async () => {
    const {context, pending, node} = fixture();
    const background = context.load(true);
    const filtered = context.load();
    pending[1].resolve(result);
    await filtered;
    node('list').innerHTML = 'new filter result';
    pending[0].resolve(result);
    await background;
    assert.equal(node('list').innerHTML, 'new filter result');
});

test('hidden tabs skip polling and initial loading still shows errors', async () => {
    const {context, pending, node} = fixture();
    context.document.hidden = true;
    await context.load(true);
    assert.equal(pending.length, 0);
    const initial = context.load();
    assert.match(node('list').innerHTML, /Загружаем/);
    pending[0].reject(new Error('offline'));
    await initial;
    assert.match(node('list').innerHTML, /Не удалось загрузить/);
});
