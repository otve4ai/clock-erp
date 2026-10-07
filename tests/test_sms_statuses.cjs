const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('refresh updates only status cells, pauses while hidden, and survives network errors', async () => {
  const cells = Object.fromEntries(['.sms-status', '[data-sms-segments]', '[data-sms-cost]'].map(key => [key, {textContent: 'old'}]));
  const row = {dataset: {messageId: '7'}, querySelector: key => cells[key]};
  const note = {textContent: 'Automatic'};
  const summary = {dataset: {smsSummary: 'delivered'}, textContent: '0'};
  const handlers = {}, timers = new Map();
  let serial = 0, calls = 0, fail = false;
  const document = {hidden: false,
    querySelector: () => note,
    querySelectorAll: selector => selector === 'tr[data-message-id]' ? [row] : [summary],
    addEventListener: (name, fn) => { handlers[name] = fn; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/js/sms-statuses.js'), 'utf8'), {
    document, URLSearchParams, AbortController,
    setTimeout: (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      calls++;
      assert.equal(url, '/api/v1/sms/statuses?id=7');
      assert.equal(options.method, undefined); // GET only; no provider sync or SMS send.
      if (fail) throw new Error('offline');
      return {ok: true, status: 200, json: async () => ({data: {
        messages: [{id: 7, status: 'delivered', status_label: 'Доставлено', segments: 2, cost: 5, currency: 'RUB'}],
        summary: {delivered: 1},
      }})};
    },
  });
  const tick = async () => { const timer = [...timers.values()].find(value => value.delay === 30000); assert.ok(timer); await timer.fn(); };
  await tick();
  assert.equal(cells['.sms-status'].textContent, 'Доставлено');
  assert.equal(cells['.sms-status'].className, 'sms-status is-delivered');
  assert.equal(cells['[data-sms-cost]'].textContent, '5 RUB');
  assert.equal(summary.textContent, 1);
  document.hidden = true;
  handlers.visibilitychange();
  assert.equal(timers.size, 0);
  assert.equal(calls, 1);
  document.hidden = false;
  fail = true;
  await handlers['sms:statuses-synced']();
  assert.match(note.textContent, /30/);
  assert.equal(cells['.sms-status'].textContent, 'Доставлено');
  fail = false;
  await tick();
  assert.equal(note.textContent, 'Automatic');
});
