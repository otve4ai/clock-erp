const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.attributes = {}; this.value = ''; this.className = ''; this.hidden = false; this.listeners = {}; this.dataset = {}; this.style = {}; this.offsetTop = 0; this.offsetHeight = 36; this.scrollTop = 0; this.clientHeight = 240; }
    set textContent(value) { this.value = String(value); this.children = []; }
    get textContent() { return this.value + this.children.map(child => child.textContent).join('\n'); }
    set innerHTML(value) { throw new Error('HTML interpolation is forbidden'); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.value = ''; this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, handler) { this.listeners[type] = handler; }
    contains(target) { return this === target || this.children.some(child => child.contains(target)); }
    getBoundingClientRect() { return {top: 200, bottom: 255}; }
    focus() { context.document.activeElement = this; }
    fire(type, props = {}) { const event = {preventDefault() {}, stopPropagation() {}, ...props}; this.listeners[type]?.(event); }
}

const calls = [];
let reply;
const context = {
    window: {innerHeight: 900}, document: {createElement: tag => new Element(tag), listeners: {}, addEventListener(type, handler) { this.listeners[type] = handler; }},
    fetch: (url, options) => { calls.push({url, options}); return reply(url); },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/js/product-warehouse-details.js'), 'utf8'), context);
const api = context.window.ProductWarehouseDetails;
const response = data => ({ok: true, json: async () => ({data})});
const visibleText = element => element.hidden ? '' : element.value + element.children.map(visibleText).join('\n');
const picker = container => {
    const root = container.children[0];
    const [trigger, amount, transit, list] = root.children;
    return {root, trigger, amount, transit, list, options: list.children};
};
const sample = [
    {id: 'default', name: 'TTT', quantity: 0, in_transit: 0, confirmed: true, active: true},
    {id: 'hong-kong', name: 'Гонконг', quantity: 2, in_transit: 3, confirmed: true, active: true},
];

(async () => {
    const editContainer = new Element();
    const editInput = new Element('input');
    editInput.name = 'stock';
    const label = new Element('label');
    let blocked = 0;
    let saving = false;
    const editor = {input: editInput, label, isSaving: () => saving, onBlocked: () => blocked++};
    reply = async () => response(sample);
    await api.loadStocks(editContainer, 12, 'hong-kong', editor);
    assert.equal(label.textContent, 'Остаток Гонконг');
    assert.equal(editInput.value, '2');
    assert.equal(editInput.dataset.warehouseId, 'hong-kong');
    editInput.value = '8';
    const body = new Map();
    api.appendStockChange(body, editInput);
    assert.deepEqual([...body], [['stock', '8'], ['stock_warehouse_id', 'hong-kong'], ['stock_expected', '2'], ['stock_reason', 'Редактирование карточки товара']]);
    picker(editContainer).options[1].fire('click');
    assert.equal(editInput.value, '8'); // Re-selecting the same option preserves the draft.
    picker(editContainer).options[0].fire('click');
    assert.equal(blocked, 1);
    assert.equal(editInput.dataset.warehouseId, 'hong-kong');
    assert.equal(editInput.value, '8'); // Do not silently discard or apply a draft to TTT.
    editInput.value = editInput.dataset.originalValue; // Cancel.
    picker(editContainer).options[0].fire('click');
    assert.equal(label.textContent, 'Остаток TTT');
    assert.equal(editInput.value, '0');
    api.appendStockChange(body, editInput);
    assert.equal(body.size, 0);
    saving = true;
    picker(editContainer).options[1].fire('click');
    assert.equal(editInput.dataset.warehouseId, 'default');
    saving = false;
    editInput.value = '1';
    editInput.readOnly = true;
    api.appendStockChange(body, editInput);
    assert.equal(body.size, 0);
    editInput.readOnly = false;
    for (const value of ['', '-1', '2.5']) {
        editInput.value = value;
        assert.throws(() => api.appendStockChange(body, editInput));
    }
    reply = async () => { throw new Error('offline'); };
    await api.loadStocks(editContainer, 12, 'default', editor);
    assert.equal(editInput.disabled, true);
    assert.equal(editInput.dataset.warehouseId, '');
    api.appendStockChange(body, editInput);
    assert.equal(body.size, 0);
    reply = async () => response([{...sample[0], editable: false}]);
    await api.loadStocks(editContainer, 12, 'default', editor);
    assert.equal(editInput.disabled, true);
    calls.length = 0;
    const container = new Element();
    reply = async () => response(sample);
    await api.loadStocks(container, 12, 'hong-kong');
    let p = picker(container);
    assert.equal(container.children.length, 1);
    assert.equal(p.trigger.children[0].textContent, 'Гонконг');
    assert.equal(p.amount.textContent, '2 шт.');
    assert.equal(p.transit.textContent, 'В пути: 3 шт.');
    assert.equal(p.list.hidden, true);
    assert.equal(p.trigger.attributes['aria-expanded'], 'false');
    assert.equal(p.trigger.attributes['aria-controls'], p.list.id);
    assert.equal(p.options[0].attributes['aria-selected'], 'false');
    assert.equal(p.options[1].attributes['aria-selected'], 'true');
    assert.equal(p.options[0].children[1].textContent, 'TTT');
    assert.doesNotMatch(visibleText(container), /TTT/);
    assert.equal(container.attributes['aria-busy'], 'false');
    assert.equal(calls[0].options.cache, 'no-store');
    assert.equal(calls[0].url, '/api/v1/products/12/warehouse-stocks');

    p.trigger.fire('click');
    assert.equal(p.list.hidden, false);
    assert.equal(context.document.activeElement, p.options[1]);
    assert.match(visibleText(container), /В пути на склад: 3 шт\./);
    p.root.fire('keydown', {key: 'Home'});
    assert.equal(context.document.activeElement, p.options[0]);
    p.options[0].fire('click');
    assert.equal(p.amount.textContent, '0 шт.');
    assert.equal(p.transit.textContent, '');
    assert.equal(p.list.hidden, true);
    assert.equal(context.document.activeElement, p.trigger);
    assert.equal(calls.length, 1); // Selection is client-side only; never submits a stock write.
    await api.loadStocks(container, 12, 'hong-kong');
    p = picker(container);
    assert.equal(p.trigger.children[0].textContent, 'TTT'); // Preserve choice when saving the same card.
    await api.loadStocks(container, 12, 'all');
    p = picker(container);
    assert.equal(calls.length, 3); // Refresh never reuses a stale stock cache.
    assert.equal(p.trigger.children[0].textContent, 'TTT'); // No fabricated company total in the picker.

    // The number of warehouses must not expand the card by default.
    const many = Array.from({length: 10}, (_, index) => ({...sample[1], id: `extra-${index}`, name: `Склад ${index}`, quantity: index + 1, in_transit: 0}));
    reply = async () => response([sample[0], ...many]);
    await api.loadStocks(container, 12, 'default');
    p = picker(container);
    assert.equal(container.children.length, 1);
    assert.equal(p.list.children.length, 11);
    assert.equal(p.amount.textContent, '0 шт.');
    assert.doesNotMatch(visibleText(container), /Склад 9/);
    p.root.fire('keydown', {key: 'ArrowDown'});
    assert.equal(p.list.hidden, false);
    assert.match(visibleText(container), /Склад 9\n10 шт\./);
    p.options[10].offsetTop = 360;
    p.root.fire('keydown', {key: 'End'});
    assert.equal(context.document.activeElement, p.options[10]);
    assert.equal(p.list.scrollTop, 156);
    p.root.fire('keydown', {key: 'ArrowDown'});
    assert.equal(context.document.activeElement, p.options[0]);
    assert.equal(p.list.scrollTop, 0);
    p.root.fire('keydown', {key: 'ArrowUp'});
    assert.equal(context.document.activeElement, p.options[10]);
    p.root.fire('keydown', {key: 'Escape'});
    assert.equal(p.list.hidden, true);
    assert.equal(context.document.activeElement, p.trigger);
    p.trigger.fire('click');
    context.document.listeners.pointerdown({target: new Element()});
    assert.equal(p.list.hidden, true);
    p.trigger.fire('click');
    p.root.fire('keydown', {key: 'Tab'});
    assert.equal(p.list.hidden, true);
    p.trigger.fire('click');
    context.document.listeners.focusin({target: new Element()});
    assert.equal(p.list.hidden, true);
    await api.loadStocks(container, 12, 'extra-9');
    p = picker(container);
    assert.equal(p.list.hidden, true);
    assert.equal(p.trigger.children[0].textContent, 'Склад 9');
    assert.equal(p.amount.textContent, '10 шт.');

    // Low viewport space opens upwards and bounds menu height.
    context.window.innerHeight = 300;
    p.trigger.fire('click');
    assert.equal(p.list.dataset.direction, 'up');
    assert.equal(p.list.style.maxHeight, '120px');
    context.window.innerHeight = 900;

    // Every warehouse is available in the menu, even with zero stock.
    reply = async () => response([sample[0], {...sample[1], quantity: 0, in_transit: 0}]);
    await api.loadStocks(container, 12, 'default');
    p = picker(container);
    assert.equal(p.options.length, 2);
    assert.doesNotMatch(visibleText(container), /Гонконг/);
    p.trigger.fire('click');
    assert.match(visibleText(container), /Гонконг\n0 шт\./);
    await api.loadStocks(container, 12, 'hong-kong');
    p = picker(container);
    assert.equal(p.amount.textContent, '0 шт.');
    assert.equal(p.list.hidden, true);

    reply = async () => response([sample[0], {...sample[1], quantity: 0, in_transit: 4}]);
    await api.loadStocks(container, 12, 'default');
    p = picker(container);
    p.trigger.fire('click');
    assert.match(visibleText(container), /Гонконг\n0 шт\./);
    assert.match(visibleText(container), /В пути на склад: 4 шт\./);

    reply = async () => response([{...sample[0], quantity: null, confirmed: false, name: '<img src=x onerror=bad()>'}]);
    await api.loadStocks(container, 12, 'default');
    assert.match(container.textContent, /Не подтверждён/);
    assert.match(container.textContent, /<img src=x onerror=bad\(\)>/);
    assert.equal(picker(container).trigger.children[0].tag, 'span');
    assert.doesNotMatch(container.textContent, /0 шт\.|В пути/);

    reply = async () => response([{...sample[1], active: false}, sample[0]]);
    await api.loadStocks(container, 13, 'missing');
    p = picker(container);
    assert.equal(p.trigger.children[0].textContent, 'TTT');
    assert.equal(p.options[0].children[1].textContent, 'TTT');
    assert.match(p.options[1].textContent, /Гонконг · отключён/);

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
