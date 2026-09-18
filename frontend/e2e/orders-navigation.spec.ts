import { expect, test } from '@playwright/test';

test('orders reuse one table, preserve scrolling and open exact search without a document reload', async ({
  page,
}) => {
  await page.goto('/app/orders', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('[data-orders-table-scroll]')).toHaveCount(1);
  await page.locator('#ordersLayoutSwitch [data-layout-mode="list"]').click();
  await expect(page.locator('.orders-list-table')).toBeVisible();
  await page.locator('#ordersLayoutSwitch [data-layout-mode="split"]').click();
  const table = await page.locator('[data-orders-table-scroll]').elementHandle();
  const documents: string[] = [];
  page.on('request', (request) => {
    if (request.resourceType() === 'document') documents.push(request.url());
  });
  await page.locator('.orders-split-table a.order-number').first().click();
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
  expect(await table?.evaluate((node) => node.isConnected)).toBe(true);
  await page.locator('#orderSearch').fill('7002');
  await page.locator('#orderSearch').press('Enter');
  await expect(page.locator('.order-detail-panel')).toContainText('7002');
  expect(documents).toEqual([]);
  await expect(page.locator('[data-orders-table-scroll]')).toHaveCount(1);
});

test('a late list response cannot replace a newer selected order', async ({ page }) => {
  await page.goto('/app/orders', { waitUntil: 'domcontentloaded' });
  let started = false;
  await page.route('**/api/orders?**', async (route) => {
    started = true;
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.fulfill({ response });
  });
  await page.locator('[data-status-filter="all"]').click();
  await expect.poll(() => started).toBe(true);
  await page.locator('.orders-split-table a.order-number').first().click();
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
  await page.waitForTimeout(500);
  await expect(page).toHaveURL(/\/order\/7001/);
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
});

test('rapid selections send only the first and final card and history stays partial', async ({
  page,
}) => {
  await page.goto('/app/orders', { waitUntil: 'domcontentloaded' });
  let cardRequests = 0;
  let releaseFirst: () => void = () => {};
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  await page.route('**/order/*', async (route) => {
    if (!route.request().headers()['x-order-detail']) {
      await route.continue();
      return;
    }
    cardRequests += 1;
    const response = await route.fetch();
    if (cardRequests === 1) await firstGate;
    await route.fulfill({ response });
  });
  const rows = page.locator('.orders-split-table a.order-number');
  for (let i = 0; i < 10; i++) await rows.nth(i % 2).press('Enter');
  releaseFirst();
  await expect(page.locator('.order-detail-panel')).toContainText('7002');
  expect(cardRequests).toBeLessThanOrEqual(2);
  const documents: string[] = [];
  page.on('request', (request) => {
    if (request.resourceType() === 'document') documents.push(request.url());
  });
  await page.goBack();
  await expect(page.locator('.order-detail-panel')).toContainText('Выберите заказ');
  expect(documents).toEqual([]);
});

test('history restores list filters without reloading the unchanged card', async ({ page }) => {
  await page.goto('/app/orders', { waitUntil: 'domcontentloaded' });
  await page.locator('.orders-split-table a.order-number').first().click();
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
  const panel = await page.locator('.order-detail-panel').elementHandle();
  let requests = 0;
  page.on('request', (request) => {
    if (request.headers()['x-order-detail']) requests++;
  });
  await page.locator('[data-status-filter="N"]').click();
  await expect(page).toHaveURL(/status=N/);
  await page.locator('[data-status-filter="all"]').click();
  await expect(page).not.toHaveURL(/status=N/);
  await page.goBack();
  await expect(page.locator('[data-status-filter="N"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.orders-split-table a.order-number')).toHaveCount(2);
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
  expect(await panel?.evaluate((node) => node.isConnected)).toBe(true);
  expect(requests).toBe(0);
});

test('history to the current card cancels an unfinished different selection', async ({ page }) => {
  await page.goto('/app/orders', { waitUntil: 'domcontentloaded' });
  const first = page.locator('.orders-split-table a.order-number').first();
  await first.click();
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
  const response = page.waitForResponse((r) => r.request().headers()['x-order-detail'] === '1');
  await first.click();
  await response;
  let started = false;
  await page.route('**/order/7002*', async (route) => {
    const result = await route.fetch();
    started = true;
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.fulfill({ response: result });
  });
  await page.locator('.orders-split-table a.order-number').nth(1).click();
  await expect.poll(() => started).toBe(true);
  await page.goBack();
  await page.waitForTimeout(500);
  await expect(page).toHaveURL(/\/order\/7001/);
  await expect(page.locator('.order-detail-panel')).toContainText('7001');
});

test('Bitrix geography is visibly selected in the sale modal after every opening', async ({
  page,
}) => {
  await page.goto('/order/7902', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-order-sale-action]').click();

  const modal = page.locator('#orderSaleModal');
  await expect(modal).toHaveClass(/is-open/);
  await expect(modal.locator('#orderSaleCountry .brand-combobox-value')).toHaveText('Россия');
  await expect(modal.locator('#orderSaleRegion .brand-combobox-value')).toHaveText('Санкт-Петербург');
  await expect(modal.locator('#orderSaleCity .brand-combobox-value')).toHaveText('Санкт-Петербург');
  await expect(modal.locator('input[name="country"]')).toHaveValue('Россия');
  await expect(modal.locator('input[name="region"]')).toHaveValue('Санкт-Петербург');
  await expect(modal.locator('input[name="city"]')).toHaveValue('Санкт-Петербург');

  await modal.locator('[data-close-sale-dialog]').first().click();
  await page.locator('[data-order-sale-action]').click();
  await expect(modal.locator('#orderSaleCountry .brand-combobox-value')).toHaveText('Россия');
  await expect(modal.locator('#orderSaleRegion .brand-combobox-value')).toHaveText('Санкт-Петербург');
  await expect(modal.locator('#orderSaleCity .brand-combobox-value')).toHaveText('Санкт-Петербург');
});

test('unknown Bitrix geography stays unselected and the manual cascade remains usable', async ({
  page,
}) => {
  await page.goto('/order/7903', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-order-sale-action]').click();

  const modal = page.locator('#orderSaleModal');
  const country = modal.locator('#orderSaleCountry');
  const region = modal.locator('#orderSaleRegion');
  const city = modal.locator('#orderSaleCity');
  await expect(country.locator('.brand-combobox-value')).toHaveText('Выберите страну');
  await expect(region.locator('.brand-combobox-value')).toHaveText('Выберите регион');
  await expect(city.locator('.brand-combobox-value')).toHaveText('Выберите город');

  await country.locator('.brand-combobox-trigger').click();
  await country.locator('[role="option"][data-brand="Россия"]').click();
  await region.locator('.brand-combobox-trigger').click();
  await region.locator('[role="option"][data-brand="Санкт-Петербург"]').click();
  await city.locator('.brand-combobox-trigger').click();
  await city.locator('[role="option"][data-brand="Санкт-Петербург"]').click();

  await expect(country.locator('.brand-combobox-value')).toHaveText('Россия');
  await expect(region.locator('.brand-combobox-value')).toHaveText('Санкт-Петербург');
  await expect(city.locator('.brand-combobox-value')).toHaveText('Санкт-Петербург');
});
