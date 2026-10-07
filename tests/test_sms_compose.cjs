const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

function setup() {
  const element = () => ({value: '', hidden: true, disabled: false, textContent: '', innerHTML: '',
    handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; }, focus() {}});
  const fields = Object.fromEntries(['recipient_name_override', 'cdek_selection', 'text', 'phone', 'order_search', 'customer_id',
    'customer_name', 'order_id', 'order_number', 'template_id'].map(name => [name, element()]));
  fields.recipient_name_override.disabled = true;
  const selectors = new Map();
  const form = element();
  form.elements = fields;
  form.querySelector = selector => {
    if (!selectors.has(selector)) selectors.set(selector, element());
    return selectors.get(selector);
  };
  const timers = new Map(), requests = [];
  let timerId = 0;
  const context = {
    window: {SMS_BOOTSTRAP: {}}, document: {body: {dataset: {}},
      getElementById: id => id === 'smsComposeForm' ? form : null,
      querySelector: () => null, querySelectorAll: () => []},
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: url => new Promise(resolve => requests.push({url, resolve})),
  };
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../app/static/js/sms.js'), 'utf8'), context);
  const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
  return {fields, form, selectors, requests, flush,
    input(number) { fields.order_search.value = number; fields.order_search.handlers.input(); },
    runTimer() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); },
    clickOrder(index = 0) { selectors.get('[data-order-results]').handlers.click({target: {closest: () => ({dataset: {orderIndex: String(index)}})}}); },
    async respond(index, data, ok = true) { requests[index].resolve({ok, json: async () => ({data, message: 'Поиск недоступен'})}); await flush(); },
  };
}

test('select order by number, populate recipient, clear stale customer, then switch to nameless order', async () => {
  const ui = setup();
  ui.fields.customer_id.value = 'old-customer';
  ui.input('00551'); ui.runTimer();
  assert.equal(ui.requests[0].url, '/api/v1/sms/orders?q=00551');
  await ui.respond(0, [{id: 'local-1', number: '00551', phone: '+79991234567', name: 'Анна'}]);
  ui.clickOrder();
  assert.equal(ui.fields.order_id.value, 'local-1');
  assert.equal(ui.fields.phone.value, '+79991234567');
  assert.equal(ui.fields.customer_name.value, 'Анна');
  assert.equal(ui.selectors.get('[data-recipient-name]').value, 'Анна');
  assert.equal(ui.fields.customer_id.value, '');
  ui.input('2');
  assert.equal(ui.fields.phone.value, '');
  assert.equal(ui.fields.order_id.value, '');
  ui.runTimer();
  await ui.respond(1, [{id: '2', number: '2', phone: '', name: ''}]);
  ui.clickOrder();
  assert.equal(ui.fields.customer_name.value, '');
  assert.equal(ui.selectors.get('[data-recipient-name]').value, '');
  assert.match(ui.selectors.get('[data-recipient-name-hint]').textContent, /без обращения/);
  assert.equal(ui.fields.phone.value, '');
  assert.match(ui.selectors.get('[data-order-selection]').textContent, /Телефон отсутствует/);
  ui.selectors.get('[data-clear-order]').handlers.click();
  assert.equal(ui.fields.order_id.value, '');
  assert.equal(ui.fields.order_search.value, '');
  assert.equal(ui.requests.length, 2); // No send request.
});

test('late search response cannot overwrite newer order results; errors are visible', async () => {
  const ui = setup();
  ui.input('1'); ui.runTimer();
  ui.input('2'); ui.runTimer();
  await ui.respond(1, [{id: '2', number: '2', phone: '222', name: ''}]);
  await ui.respond(0, [{id: '1', number: '1', phone: '111', name: ''}]);
  ui.clickOrder();
  assert.equal(ui.fields.order_id.value, '2');
  assert.equal(ui.fields.phone.value, '222');
  ui.input('3'); ui.runTimer();
  await ui.respond(2, [], false);
  assert.equal(ui.selectors.get('[data-order-selection]').textContent, 'Поиск недоступен');
  assert.equal(ui.fields.order_id.value, '');
});

test('direct CDEK selection fills phone and clears its token when changing number', async () => {
  const ui = setup();
  ui.input('1234567890');
  assert.equal(ui.selectors.get('[data-cdek-search]').hidden, false);
  ui.selectors.get('[data-cdek-search]').handlers.click();
  assert.equal(ui.requests[0].url, '/api/v1/sms/cdek?number=1234567890');
  await ui.respond(0, {id: '', number: 'SHOP-7', tracking: '1234567890', name: 'Анна', phone: '+79991234567', cdek_selection: 'token'});
  assert.equal(ui.fields.cdek_selection.value, 'token');
  assert.equal(ui.fields.order_id.value, '');
  assert.equal(ui.fields.phone.value, '+79991234567');
  ui.input('551');
  assert.equal(ui.fields.cdek_selection.value, '');
  assert.equal(ui.fields.phone.value, '');
});

 test('manual name updates template values and is reset for another recipient', () => {
  const ui = setup();
  const input = ui.selectors.get('[data-recipient-name]');
  input.handlers.input({target: {value: 'Мария'}});
  assert.equal(ui.fields.customer_name.value, 'Мария');
  assert.equal(ui.fields.recipient_name_override.value, 'Мария');
  assert.equal(ui.fields.recipient_name_override.disabled, false);
  input.handlers.input({target: {value: ''}});
  assert.equal(ui.fields.recipient_name_override.value, '');
  assert.match(ui.selectors.get('[data-recipient-name-hint]').textContent, /убрано/);
  ui.input('551');
  assert.equal(ui.fields.recipient_name_override.disabled, true);
});
