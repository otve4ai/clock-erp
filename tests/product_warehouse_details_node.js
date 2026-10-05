const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.attributes = {}; this.value = ''; this.className = ''; this.open = false; }
    set textContent(value) { this.value = String(value); this.children = []; }
    get textContent() { return this.value + this.children.map(child => child.textContent).join('\n'); }
    set innerHTML(value) { throw new Error('HTML interpolation is forbidden'); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.value = ''; this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
}

const calls = [];
let reply;
const context = {
    window: {}, document: {createElement: tag => new Element(tag)},
    fetch: (url, options) => { calls.push({url, options}); return reply(url); },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/js/product-warehouse-details.js'), 'utf8'), context);
const api = context.window.ProductWarehouseDetails;
const response = data => ({ok: true, json: async () => ({data})});
const visibleText = element => element.value + (element.tag === 'details' && !element.open
    ? element.children.filter(child => child.tag === 'summary') : element.children).map(visibleText).join('\n');
const sample = [
    {id: 'default', name: 'TTT', quantity: 0, in_transit: 0, confirmed: true, active: true},
    {id: 'hong-kong', name: 'Гонконг', quantity: 2, in_transit: 3, confirmed: true, active: true},
];

(async () => {
    const container = new Element();
    reply = async () => response(sample);
    await api.loadStocks(container, 12, 'hong-kong');
    assert.equal(container.children.length, 2);
    assert.match(container.textContent, /TTT\n0 шт\./);
    assert.match(container.textContent, /Гонконг\n2 шт\./);
    assert.match(container.textContent, /В пути на склад: 3 шт\./);
    assert.equal(container.children[0].className.includes('is-selected'), false);
    assert.equal(container.children[1].tag, 'details');
    assert.equal(container.children[1].open, false);
    assert.equal(container.children[1].children[0].tag, 'summary');
    assert.equal(container.children[1].children[1].children[0].className.includes('is-selected'), true);
    assert.match(visibleText(container), /Гонконг: 2 шт\./);
    assert.match(visibleText(container), /в пути: 3/);
    assert.doesNotMatch(visibleText(container), /Выбранный склад/);
    assert.equal(container.attributes['aria-busy'], 'false');
    assert.equal(calls[0].options.cache, 'no-store');
    assert.equal(calls[0].url, '/api/v1/products/12/warehouse-stocks');
    await api.loadStocks(container, 12, 'all');
    assert.equal(calls.length, 2); // Reopening never reuses a stale stock cache.
    assert.equal(container.children.filter(row => row.className.includes('is-selected')).length, 0);
    assert.match(visibleText(container), /Другие склады · 1/);

    // The number of warehouses must not expand the card by default.
    const many = Array.from({length: 10}, (_, index) => ({...sample[1], id: `extra-${index}`, name: `Склад ${index}`, quantity: index + 1, in_transit: 0}));
    reply = async () => response([sample[0], ...many]);
    await api.loadStocks(container, 12, 'default');
    assert.equal(container.children.length, 2);
    assert.equal(container.children[1].children[1].children.length, 10);
    assert.match(visibleText(container), /TTT\n0 шт\./);
    assert.match(visibleText(container), /Другие склады · 10/);
    assert.doesNotMatch(visibleText(container), /Склад 9/);
    container.children[1].open = true;
    assert.match(visibleText(container), /Склад 9\n10 шт\./);
    await api.loadStocks(container, 12, 'extra-9');
    assert.equal(container.children[1].open, false);
    assert.match(visibleText(container), /Склад 9: 10 шт\. · ещё 9/);

    // Empty secondary warehouses leave no rows or misleading presence markers.
    reply = async () => response([sample[0], {...sample[1], quantity: 0, in_transit: 0}]);
    await api.loadStocks(container, 12, 'default');
    assert.equal(container.children.length, 1);
    assert.doesNotMatch(visibleText(container), /Другие|Гонконг/);
    await api.loadStocks(container, 12, 'hong-kong');
    assert.match(visibleText(container), /Гонконг: 0 шт\./); // Preserve explicitly selected zero stock.
    assert.equal(container.children[1].open, false);

    reply = async () => response([sample[0], {...sample[1], quantity: 0, in_transit: 4}]);
    await api.loadStocks(container, 12, 'default');
    assert.match(visibleText(container), /Другие склады · 1 · в пути: 4/);
    container.children[1].open = true;
    assert.match(visibleText(container), /Гонконг\n0 шт\./);
    assert.match(visibleText(container), /В пути на склад: 4 шт\./);

    reply = async () => response([{...sample[0], quantity: null, confirmed: false, name: '<img src=x onerror=bad()>'}]);
    await api.loadStocks(container, 12, 'default');
    assert.match(container.textContent, /Не подтверждён/);
    assert.match(container.textContent, /<img src=x onerror=bad\(\)>/);
    assert.equal(container.children[0].children[0].tag, 'span');
    assert.doesNotMatch(container.textContent, /0 шт\.|В пути/);

    reply = async () => response([
        {label: 'Приход', warehouse_name: 'Гонконг', diff: 2, stock_before: 0, stock_after: 2, reason: '<script>bad()</script>', document_number: 'ПР-1'},
        {type: 'product_photo', label: 'Фото', source: 'Фото', reason: 'Замена'},
        {label: 'Продажа', warehouse_id: 'new-warehouse', diff: -1, stock_before: 2, stock_after: 1},
    ]);
    await api.loadHistory(container, 12);
    assert.match(container.children[0].textContent, /Приход: \+2 шт\./);
    assert.match(container.children[0].textContent, /Склад: Гонконг · 0 → 2 шт\./);
    assert.match(container.children[0].textContent, /Документ: ПР-1/);
    assert.match(container.children[0].textContent, /<script>bad\(\)<\/script>/);
    assert.doesNotMatch(container.children[1].textContent, /Склад|шт\.|→/);
    assert.match(container.children[2].textContent, /Продажа: -1 шт\./);
    assert.match(container.children[2].textContent, /Склад: new-warehouse/);
    assert.equal(calls[calls.length - 1].url, '/api/v1/products/12/movements?limit=10');

    for (const failure of [
        async () => ({ok: false, json: async () => ({data: []})}),
        async () => ({ok: true, json: async () => ({data: null})}),
        async () => { throw new Error('network'); },
        async () => ({ok: true, json: async () => { throw new Error('invalid JSON'); }}),
    ]) {
        reply = failure;
        await api.loadStocks(container, 12, 'default');
        assert.match(container.textContent, /Не удалось загрузить остатки/);
        assert.doesNotMatch(container.textContent, /0 шт\./);
        assert.equal(container.attributes['aria-busy'], 'false');
    }
    await api.loadHistory(container, 12);
    assert.match(container.textContent, /Не удалось загрузить историю/);

    // A late response (even for the same product) cannot overwrite the latest open.
    for (const method of ['loadStocks', 'loadHistory']) {
        let resolveOld;
        reply = () => new Promise(resolve => { resolveOld = resolve; });
        const old = api[method](container, 12, 'default');
        reply = async () => response([]);
        await api[method](container, 12, 'hong-kong');
        const latest = container.textContent;
        resolveOld(response(method === 'loadStocks' ? sample : [{label: 'Stale'}]));
        await old;
        assert.equal(container.textContent, latest);
    }
    console.log('warehouse detail checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
