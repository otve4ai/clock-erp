// Run the production controller and warehouse settings in a no-network DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

class Element {
    constructor(tag = 'div', key) {
        this.tagName = tag.toUpperCase();
        this.dataset = key ? {columnKey: key} : {};
        this.children = [];
        this.listeners = {};
        this.style = {setProperty(key, value) { this[key] = value; }, removeProperty(key) { delete this[key]; }};
        this.classList = {add() {}, remove() {}, toggle() {}};
        this.hidden = false;
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    removeEventListener() {}
    appendChild(child) { this.children = this.children.filter(item => item !== child); this.children.push(child); }
    replaceChildren() { this.children = []; }
    setAttribute() {}
    closest() { return null; }
    querySelectorAll(selector) {
        const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
        if (selector === '*') return descendants;
        if (selector === 'input[data-column-key]') return descendants.filter(child => child.tagName === 'INPUT');
        return [];
    }
    querySelector(selector) {
        const key = /data-column-key="([^"]+)"/.exec(selector)?.[1];
        return key ? this.children.find(child => child.dataset.columnKey === key) || null : null;
    }
}

const elements = {};
const document = {
    getElementById: id => elements[id] || null,
    createElement: tag => new Element(tag),
    createTextNode: text => Object.assign(new Element(), {textContent: text}),
    addEventListener() {},
};
globalThis.document = document;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.ErpTableLayout = require(path.join(root, 'app/static/js/erp-table-layout.js'));
const columns = require(path.join(root, 'app/static/js/erp-native-table-columns.js'));
const storage = new Map();
const context = {
    document, window: {},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
};
const template = fs.readFileSync(path.join(root, 'app/templates/warehouse.html'), 'utf8');
const settings = template.slice(template.indexOf('const warehouseTableLegacyStorageKey'),
    template.indexOf('function initializeWarehouseTableView()'));
vm.createContext(context);
vm.runInContext(settings, context);
assert(template.includes('contextHiddenColumns: warehouseContextHiddenColumns(table)'));
assert(template.includes('currentTable.dataset.siteStatusHidden === incomingTable.dataset.siteStatusHidden'));
const storageKey = 'vechasu.warehouse.table-view.v3';

function tableFor(view, hidden, width) {
    const table = new Element('table');
    table.dataset.siteStatusHidden = String(hidden);
    table.parentElement = {clientWidth: width};
    const colgroup = new Element('colgroup');
    const header = new Element('tr');
    const body = new Element('tr');
    for (const key of view.order) {
        colgroup.appendChild(new Element('col', key));
        header.appendChild(new Element('th', key));
        body.appendChild(new Element('td', key));
    }
    table.querySelector = selector => selector === 'colgroup' ? colgroup : selector === 'thead' ? header : null;
    table.querySelectorAll = selector => {
        if (selector === 'thead th[data-column-key]') return header.children;
        if (selector === 'tr') return [header, body];
        if (selector === 'tbody tr') return [body];
        const key = /data-column-key="([^"]+)"/.exec(selector)?.[1];
        return key ? [colgroup, header, body].flatMap(row => row.children.filter(cell => cell.dataset.columnKey === key)) : [];
    };
    return table;
}

function createController(table, view, scoped = true) {
    return columns.create({table, view, actionWidth: 82,
        contextHiddenColumns: scoped ? context.warehouseContextHiddenColumns(table) : undefined});
}

// Both responsive and manually resized layouts exclude the automatic column
// from the width calculation, without touching its saved position or width.
for (const width of [320, 768, 1440]) {
    for (const fixed of [false, true]) {
        storage.clear();
        let view = context.readWarehouseTableView();
        view.widths.site_status = 137;
        view.order.splice(view.order.indexOf('site_status'), 1);
        view.order.push('site_status');
        if (fixed) view.customWidths = ['site_status'];
        const before = JSON.stringify(view);
        for (const hidden of [false, true, false, true, false]) { // TTT -> HK -> TTT -> all -> TTT
            const table = tableFor(view, hidden, width);
            const controller = createController(table, view);
            controller.applyView(true);
            const actual = controller.getActualWidths();
            assert.equal(Object.hasOwn(actual, 'site_status'), !hidden);
            for (const cell of table.querySelectorAll('[data-column-key="site_status"]')) assert.equal(cell.hidden, hidden);
            assert(Math.abs(parseFloat(table.style.width) - Object.values(actual).reduce((sum, value) => sum + value, 82)) < 0.001);
            controller.applyLayout(true); // Filter/pagination refresh and resize.
            context.saveWarehouseTableView(view);
            assert.equal(storage.get(storageKey), before);
            view = context.readWarehouseTableView();
        }
    }
}

storage.clear();
const view = context.readWarehouseTableView();
const table = tableFor(view, true, 390);
const controller = createController(table, view);
for (const id of ['warehouseColumnSettingsTrigger', 'warehouseColumnSettingsPanel',
    'warehouseColumnSettingsList', 'warehouseColumnSettingsClose', 'warehouseTableReset']) elements[id] = new Element();
const list = elements.warehouseColumnSettingsList;
elements.warehouseColumnSettingsPanel.appendChild(list);
context.initializeWarehouseColumnSettings(table, view, {}, controller);
const inputs = list.querySelectorAll('input[data-column-key]');
assert(!inputs.some(input => input.dataset.columnKey === 'site_status'));
const price = inputs.find(input => input.dataset.columnKey === 'price');
price.checked = false;
price.listeners.change();
assert(JSON.parse(storage.get(storageKey)).hidden.includes('price'));
assert(!JSON.parse(storage.get(storageKey)).hidden.includes('site_status'));
// The automatically hidden status must not count as a visible content column.
view.hidden = view.order.filter(key => !['photo', 'name', 'price', 'site_status'].includes(key));
price.checked = false;
price.listeners.change();
assert.equal(price.checked, true);
elements.warehouseTableReset.listeners.click();
assert(!view.hidden.includes('site_status'));
assert(table.querySelectorAll('[data-column-key="site_status"]').every(cell => cell.hidden));
assert(!storage.has(storageKey));
// Existing user preferences still work on TTT and unscoped tables (sales).
view.hidden.push('site_status');
const ttt = tableFor(view, false, 1440);
createController(ttt, view, false).applyView(true);
assert(ttt.querySelectorAll('[data-column-key="site_status"]').every(cell => cell.hidden));
view.hidden = view.hidden.filter(key => key !== 'site_status');
createController(ttt, view, false).applyView(true);
assert(ttt.querySelectorAll('[data-column-key="site_status"]').every(cell => !cell.hidden));
console.log('Warehouse site column: layout, reset, settings and saved preferences OK');
