const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function element() {
    return {
        dataset: {}, attributes: {}, textContent: "", listeners: {},
        setAttribute(key, value) { this.attributes[key] = value; },
        getAttribute(key) { return this.attributes[key] || null; },
        removeAttribute(key) { delete this.attributes[key]; },
        addEventListener(event, callback) { this.listeners[event] = callback; },
        appendChild() {},
    };
}

async function main() {
    const nodes = new Map();
    const run = element();
    const root = element();
    root.querySelector = selector => {
        if (!nodes.has(selector)) nodes.set(selector, element());
        return nodes.get(selector);
    };
    root.querySelectorAll = selector => selector === "[data-site-sync-run]" ? [run] : [];
    const document = {
        querySelector: () => root, createElement: element,
        addEventListener() {}, hidden: false,
    };
    let response = {outcome: "success", last_success_at: "2026-09-30T13:00:00Z",
        unknown_statuses: 1, has_data: true};
    let poll;
    const refreshed = [];
    const window = {
        location: {href: "https://erp.test/app/products?site_issue=out_of_stock_active&q=ZENO"},
        setInterval(callback) { poll = callback; },
        async loadWarehouseResultsUrl(url) { refreshed.push(url.href); return true; },
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,
        "../../app/static/js/product-site-status-sync.js"), "utf8"), {
        document, window, URL, Intl, Date, Number, Boolean,
        fetch: async () => ({ok: true, json: async () => ({data: response})}),
    });
    await run.listeners.click();
    assert.equal(root.dataset.syncState, "attention");
    assert.match(nodes.get("[data-site-sync-indicator-label]").textContent, /неизвестных/);
    assert.deepEqual(refreshed, [window.location.href]);
    assert.equal(run.disabled, false);

    poll();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(refreshed.length, 1, "unchanged summary must not reload the table");
    response = {...response, last_success_at: "2026-09-30T13:02:00Z", unknown_statuses: 0};
    poll();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(refreshed.length, 2, "background completion must update the table");

    response = {...response, outcome: "error", stale: true};
    await run.listeners.click();
    assert.equal(root.dataset.syncState, "error");
    assert.match(nodes.get("[data-site-sync-state]").textContent, /Данные устарели/);
    assert.equal(refreshed.length, 2, "failed sync must not refresh as successful");
    console.log("Site-status sync UI checks passed");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
