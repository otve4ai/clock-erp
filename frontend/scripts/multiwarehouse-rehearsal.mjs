/* global process, performance, crypto, document, console */

import { chromium } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const baseURL = process.env.REHEARSAL_BASE_URL || 'http://127.0.0.1:5097';
const outputDir = path.resolve(
  process.env.REHEARSAL_SCREENSHOT_DIR || '../QA_EVIDENCE/multiwarehouse-rehearsal',
);
await mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.setDefaultTimeout(7000);
const results = { pages: {}, checks: {} };

const visit = async (name, url) => {
  const started = performance.now();
  const response = await page.goto(`${baseURL}${url}`, {
    waitUntil: 'domcontentloaded', timeout: 15000,
  });
  results.pages[name] = {
    status: response?.status(),
    duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
  };
  if (!response?.ok()) throw new Error(`${name}: HTTP ${response?.status()}`);
};

await visit('products', '/app/products');
await page.screenshot({ path: path.join(outputDir, '01-products-udelnaya.png'), fullPage: true });
await page.locator('#warehouseSelector summary').click();
await page.screenshot({ path: path.join(outputDir, '02-warehouse-selector.png'), fullPage: true });

const choices = page.locator('[data-warehouse-choice]');
results.checks.warehouse_count_before_create = await choices.count();
for (let index = 0; index < await choices.count(); index += 1) {
  if (!(await choices.nth(index).isChecked())) await choices.nth(index).check();
}
await page.waitForLoadState('networkidle');
results.checks.selected_warehouses = await page.locator('[data-warehouse-choice]:checked').count();

await visit('search', '/app/products?q=No.1%20BLACK');
results.checks.search_rows = await page.locator('tr[data-product-id]').count();
await page.screenshot({ path: path.join(outputDir, '03-search-two-warehouses.png'), fullPage: true });

await visit('in_stock', '/app/products?stock_state=in');
results.checks.in_stock_rows = await page.locator('tr[data-product-id]').count();
await visit('out_of_stock', '/app/products?stock_state=out');
results.checks.out_of_stock_rows = await page.locator('tr[data-product-id]').count();
await page.screenshot({ path: path.join(outputDir, '04-out-of-stock.png'), fullPage: true });
await visit('pagination', '/app/products?page=2&per_page=50');
results.checks.pagination_rows = await page.locator('tr[data-product-id]').count();

await visit('products_for_admin', '/app/products');
const warehousesResponse = await page.request.get(`${baseURL}/api/v1/warehouses`);
const warehousesPayload = await warehousesResponse.json();
let rehearsalWarehouse = warehousesPayload.warehouses.find(
  (warehouse) => warehouse.name === 'QA Rehearsal Final',
);
if (!rehearsalWarehouse) {
  const createResponse = await page.request.post(`${baseURL}/api/v1/warehouses`, {
    data: { name: 'QA Rehearsal Final' },
  });
  const createPayload = await createResponse.json();
  if (!createResponse.ok() || !createPayload.ok) throw new Error('warehouse create failed');
  rehearsalWarehouse = createPayload.warehouse;
}
results.checks.created_warehouse_id = rehearsalWarehouse.id;
await page.reload({ waitUntil: 'networkidle' });
await page.locator('#warehouseSelector summary').click();
results.checks.new_warehouse_unselected = !(await page.locator(
  `[data-warehouse-choice][value="${rehearsalWarehouse.id}"]`,
).isChecked());
await page.screenshot({ path: path.join(outputDir, '05-third-warehouse.png'), fullPage: true });

const searchResponse = await page.request.get(
  `${baseURL}/api/v1/warehouse-products/search?q=No.1&warehouse_id=1`,
);
const searchPayload = await searchResponse.json();
const product = searchPayload.items.find((item) => !item.is_bundle && Number(item.stock) >= 1);
if (!product) throw new Error('transfer fixture product not found');
const transfer = async (from, to) => {
  const response = await page.request.post(`${baseURL}/api/v1/warehouse-transfers`, {
    data: {
      from_warehouse_id: from,
      to_warehouse_id: to,
      product_id: product.id,
      quantity: 1,
      comment: 'Production-copy rehearsal',
      idempotency_key: crypto.randomUUID(),
    },
  });
  const payload = await response.json();
  if (!response.ok() || !payload.ok) throw new Error(payload.message || 'transfer failed');
  return payload;
};
await transfer(1, 2);
await transfer(2, 1);
results.checks.transfer_round_trip = true;
await page.reload({ waitUntil: 'networkidle' });
await page.locator('#warehouseSelector summary').click();
await page.getByRole('button', { name: 'Переместить товар' }).click();
await page.screenshot({ path: path.join(outputDir, '06-transfer-dialog.png'), fullPage: true });
await page.keyboard.press('Escape');
results.checks.keyboard_escape = !(await page.locator('#warehouseTransferDialog').evaluate(
  (dialog) => dialog.open,
));

for (const [name, url] of Object.entries({
  receipts: '/app/receipts',
  sales: '/app/sales',
  inventory: '/app/inventory',
  writeoffs: '/app/writeoffs',
  analytics: '/app/products?view=analytics',
})) await visit(name, url);

await page.setViewportSize({ width: 390, height: 844 });
await visit('products_mobile', '/app/products');
await page.screenshot({ path: path.join(outputDir, '07-products-mobile.png'), fullPage: true });
results.checks.mobile_no_horizontal_document_overflow = await page.evaluate(
  () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
);

await writeFile(
  path.join(outputDir, 'results.json'),
  `${JSON.stringify(results, null, 2)}\n`,
  'utf8',
);
await browser.close();
console.log(JSON.stringify(results, null, 2));
