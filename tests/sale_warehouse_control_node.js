/* Local, no-DOM dependency regression for the sale warehouse adapter. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'app/templates/sales.html'), 'utf8');
const catalog = fs.readFileSync(path.join(root, 'app/static/js/catalog-combobox.js'), 'utf8');
const input = {value: '', disabled: true};
const trigger = {disabled: true};
const caption = {textContent: ''};
let resets = 0;
let validations = 0;
let dropdownClosed = 0;
const options = [
    ['default', 'Основной TTT'], ['hong-kong', 'Гонконг'],
    ['future-id', 'Ещё один склад с длинным названием'],
].map(([id, name]) => ({
    dataset: {brand: id},
    classList: {toggle(_name, active) { this.active = active; }},
    setAttribute(name, value) { this[name] = value; },
    querySelector() { return {textContent: name}; },
}));
const combobox = {
    dataset: {allLabel: 'Выберите склад списания'},
    querySelector(selector) {
        return {
            '.brand-combobox-hidden': input,
            '.brand-combobox-trigger': trigger,
            '.brand-combobox-value': caption,
        }[selector] || null;
    },
    querySelectorAll() { return options; },
    dispatchEvent(event) { context.onSaleWarehouseChange(event); },
};
const context = vm.createContext({
    window: {
        crypto: {randomUUID: () => 'new-request'},
        setBrandDropdownOpen() { dropdownClosed++; },
    },
    document: {getElementById: (id) => id === 'saleWarehouse' ? input : combobox},
    manualSaleForm: {dataset: {editing: '0', warehouseId: 'hong-kong'}},
    manualSaleRequestId: {value: 'original-request'},
    saleSourceInput: {value: 'Amazon'},
    modalDescription: {textContent: ''},
    resetProductFilters() { resets++; },
    validateSaleQuantity() { validations++; },
    CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
});
for (const name of ['setBrandComboboxValue', 'setCatalogComboboxDisabled']) {
    const start = catalog.indexOf(`    window.${name} = function(`);
    assert(start >= 0, name);
    vm.runInContext(catalog.slice(start, catalog.indexOf('\n    };', start) + 7), context);
}
for (const name of ['setSaleWarehouseControl', 'onSaleWarehouseChange']) {
    const start = template.indexOf(`    function ${name}(`);
    assert(start >= 0, name);
    vm.runInContext(template.slice(start, template.indexOf('\n    }', start) + 6), context);
}

// Source initialization synchronizes the label and submitted ID without resets.
assert.equal(context.setSaleWarehouseControl('hong-kong', false), 'Гонконг');
assert.equal(input.value, 'hong-kong');
assert.equal(caption.textContent, 'Гонконг');
assert.equal(input.disabled, false);
assert.equal(trigger.disabled, false);
assert.equal(resets, 0);
assert.equal(options[1]['aria-selected'], 'true');

// A real user selection uses the shared component's event (not native change).
context.window.setBrandComboboxValue(combobox, 'default', 'Основной TTT');
assert.equal(context.manualSaleForm.dataset.warehouseId, 'default');
assert.equal(context.manualSaleRequestId.value, 'new-request');
assert.equal(resets, 1);
assert.equal(validations, 1);
assert.equal(context.modalDescription.textContent, 'Источник: Amazon. Склад: Основной TTT.');
context.window.setBrandComboboxValue(combobox, 'default', 'Основной TTT');
assert.equal(resets, 1, 'Re-selecting the same warehouse must not clear the product');

context.manualSaleForm.dataset.warehouseId = 'future-id';
context.setSaleWarehouseControl('future-id', false);
assert.equal(caption.textContent, options[2].querySelector().textContent);
assert.equal(resets, 1, 'Programmatic channel sync must not duplicate resets');

// A saved sale cannot change warehouse; its submitted field stays disabled.
context.manualSaleForm.dataset.editing = '1';
context.manualSaleForm.dataset.warehouseId = 'hong-kong';
context.setSaleWarehouseControl('hong-kong', true);
assert.equal(input.disabled, true);
assert.equal(trigger.disabled, true);
assert.equal(caption.textContent, 'Гонконг');
context.onSaleWarehouseChange({detail: {displayValue: 'Основной TTT'}});
assert.equal(resets, 1);

// Reset without a channel clears the visible label as well as the hidden input.
context.manualSaleForm.dataset.editing = '0';
context.setSaleWarehouseControl('', true);
assert.equal(input.value, '');
assert.equal(caption.textContent, 'Выберите склад списания');
assert.equal(resets, 1);
assert(dropdownClosed >= 2);
assert(options.every((option) => option['aria-selected'] === 'false'));

context.manualSaleForm.dataset.warehouseId = 'default';
context.setSaleWarehouseControl('default', false);
assert.equal(input.value, 'default');
assert.equal(caption.textContent, 'Основной TTT');
assert.equal(trigger.disabled, false);
console.log('Sale warehouse control: initialization, selection, reset, edit lock and labels passed');
