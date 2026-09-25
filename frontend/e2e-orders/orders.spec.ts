import { expect, test } from '@playwright/test';

test('sources, counts and scoped searches', async ({ page }) => {
  await page.goto('/app/orders');
  await expect(page.locator('[data-source-filter="all"]')).toHaveText('Все заказы 128');
  await expect(page.locator('[data-source-filter="tictactoy"]')).toHaveText('TicTacToy 3');
  await expect(page.locator('[data-source-filter="wildberries"]')).toHaveText('Wildberries 125');
  await page.locator('[data-source-filter="wildberries"]').click();
  await expect(page.locator('[data-status-filter="N"]')).toHaveCount(0);
  await page.locator('[data-status-filter="WB_SOLD"]').click();
  await expect(page).toHaveURL(/status=WB_SOLD/);
  await expect(page.locator('.orders-split-table tbody tr').first()).toContainText('Получен покупателем');
  await page.locator('[data-source-filter="tictactoy"]').click();
  await expect(page.locator('.orders-split-table tbody tr')).toHaveCount(3);
  await page.locator('#orderSearch').fill('WATCH');
  await expect(page.locator('.erp-no-results-state')).toBeVisible();
  await expect(page).toHaveURL(/q=WATCH/);
  await page.locator('[data-source-filter="wildberries"]').click();
  await expect(page.locator('#orderSearch')).toHaveValue('WATCH');
  await expect(page.locator('.orders-split-table tbody tr')).toHaveCount(50);
});

test('distant pages, card selection and scroll survive back', async ({ page }) => {
  await page.goto('/app/orders?source=wildberries&page_size=20&page=4');
  await expect(page.locator('.list-footer')).toContainText('Показано 61–80 из 125');
  await page.locator('.orders-split-table-scroll').evaluate(el => { el.scrollTop = 420; });
  const number = await page.locator('.orders-split-table .order-number').nth(4).textContent();
  await page.locator('.orders-split-table .order-number').nth(4).click();
  await expect(page.locator('.card-title h2')).toContainText(number!.trim());
  await expect(page).toHaveURL(/page=4/);
  expect(await page.locator('.orders-split-table-scroll').evaluate(el => el.scrollTop)).toBeGreaterThan(0);
  await page.goBack();
  await expect(page.locator('.list-footer')).toContainText('Показано 61–80 из 125');
  expect(await page.locator('.orders-split-table-scroll').evaluate(el => el.scrollTop)).toBeGreaterThan(0);
  await page.locator('[aria-label="Страница 7"]').click();
  await expect(page.locator('.list-footer')).toContainText('Показано 121–125 из 125');
});

test('WB products, sale action, history and source-safe selection', async ({ page }) => {
  await page.goto('/order/wildberries/9001?source=wildberries');
  await expect(page.locator('.order-control-actions').getByRole('link', { name: 'Открыть продажу', exact: true })).toBeVisible();
  await expect(page.locator('[data-open-sale-dialog]')).toHaveCount(0);
  await expect(page.locator('.order-products img')).toBeVisible();
  await expect(page.locator('.order-wb-history')).toContainText('Заказ восстановлен из Wildberries');
  await expect(page.locator('[data-entity-tasks]')).toHaveCount(0);
  await expect(page.getByRole('link', { name: '+ Создать задачу', exact: true })).toHaveCount(0);
  await expect(page.getByText('Только чтение', { exact: true })).toHaveCount(0);
  await expect(page.locator('.order-technical-fields')).toBeHidden();
  await page.locator('.order-technical summary').click();
  await expect(page.locator('.order-technical-fields')).toContainText('supplier_status');
  await page.locator('[data-source-filter="tictactoy"]').click();
  await expect(page.locator('.card-title h2')).toHaveCount(0);
});

test('diagnostics retain errors, import and sync', async ({ page }) => {
  await page.goto('/app/orders?source=wildberries');
  await expect(page.locator('[data-wb-health]')).toContainText('Требует внимания');
  await expect(page.locator('[data-wb-preview-form]')).toBeHidden();
  await page.locator('[data-sync-row="wildberries"] [data-sync-details]').click();
  await expect(page.getByRole('heading', { name: 'Диагностика Wildberries' })).toBeVisible();
  await expect(page.locator('[data-wb-missing]')).toContainText('Тестовая ошибка API');
  await expect(page.getByRole('button', { name: 'Обновить Wildberries', exact: true })).toBeVisible();
  await page.route('**/api/orders/wildberries/recovery/preview', route => route.fulfill({json:{ok:true,report:{rows:[],wb_count:1,importable:1,counts:{READY:1},errors:[],confirmation:'fixture-only'}}}));
  await page.locator('[name="supply_id"]').fill('WB-GI-TEST');
  await page.getByRole('button', { name: 'Проверить поставку' }).click();
  await expect(page.locator('[data-wb-import]')).toBeVisible();
});

test('responsive layout and explicit units', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({width,height:900});
    await page.goto('/order/wildberries/9001?source=wildberries');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator('.orders-split-table .order-units').first()).toHaveText('1 шт.');
    await page.screenshot({path:`test-results/orders-progressive-${width}.png`,fullPage:true});
  }
  expect(errors).toEqual([]);
});

test('order action toolbar keeps readable fixed controls at every responsive width', async ({ page }) => {
  for (const width of [1920, 1440, 1200, 900, 760, 620, 560, 440, 360]) {
    await page.setViewportSize({width, height: 900});
    await page.goto('/order/7002');

    const panel = page.locator('.order-control-panel');
    const controls = [
      page.locator('#orderStatusSelect'),
      panel.getByRole('button', {name: 'Обновить из Bitrix', exact: true}),
      panel.getByRole('button', {name: 'Провести продажу', exact: true}),
      panel.getByRole('button', {name: '⋯ Ещё', exact: false}),
    ];
    await expect(panel).toBeVisible();
    for (const control of controls) await expect(control).toBeVisible();

    const [panelBox, ...boxes] = await Promise.all([
      panel.boundingBox(),
      ...controls.map(control => control.boundingBox()),
    ]);
    expect(panelBox).not.toBeNull();
    expect(boxes.every(Boolean)).toBe(true);
    for (const box of boxes) {
      expect(box!.x).toBeGreaterThanOrEqual(panelBox!.x);
      expect(box!.x + box!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width + 1);
    }
    for (let left = 0; left < boxes.length; left += 1) {
      for (let right = left + 1; right < boxes.length; right += 1) {
        const a = boxes[left]!;
        const b = boxes[right]!;
        const horizontal = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
        const vertical = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
        expect(horizontal > 0 && vertical > 0).toBe(false);
      }
    }

    await expect(controls[1]).toHaveCSS('white-space', 'nowrap');
    expect(Math.round(boxes[1]!.width)).toBe(148);
    expect(await controls[1].evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    await controls[3].click();
    const menu = page.locator('[data-order-actions-dropdown]');
    await expect(menu).toBeVisible();
    const menuBox = await menu.boundingBox();
    expect(menuBox).not.toBeNull();
    expect(menuBox!.x).toBeGreaterThanOrEqual(0);
    expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width);
    expect(menuBox!.y).toBeGreaterThanOrEqual(0);
    expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(900);
  }
});

test('order calculation follows its narrow card width at the reported viewport', async ({ page }) => {
  await page.setViewportSize({width:1656,height:696});
  await page.goto('/app/orders?source=tictactoy');
  await page.locator('.orders-split-table .order-number').first().click();
  const calculation = page.locator('.order-calculation');
  const summary = calculation.locator('.summary');
  const alerts = calculation.locator('.order-calculation-alerts');
  await expect(calculation).toBeVisible();
  await expect(alerts.locator('.warning')).toHaveCount(1);
  await expect(alerts.locator('.warning')).toContainText('сначала подтвердите заказ');
  await calculation.evaluate(element => { element.style.width = '560px'; });
  const [sectionBox, summaryBox, alertsBox] = await Promise.all([
    calculation.boundingBox(),
    summary.boundingBox(),
    alerts.boundingBox(),
  ]);
  expect(sectionBox!.width).toBeLessThan(620);
  expect(alertsBox!.y).toBeGreaterThanOrEqual(summaryBox!.y + summaryBox!.height);
  expect(alertsBox!.x).toBeGreaterThanOrEqual(sectionBox!.x);
  expect(alertsBox!.x + alertsBox!.width).toBeLessThanOrEqual(sectionBox!.x + sectionBox!.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('sale submit uses existing endpoint and prevents repeated submit', async ({ page }) => {
  let submits = 0;
  await page.route('**/order/wildberries/9003/conduct-sale', async route => {
    submits += 1;
    await route.fulfill({json:{ok:true,sale_id:'fixture-sale',message:'Продажа проведена'}});
  });
  await page.goto('/order/wildberries/9003?source=wildberries');
  await page.locator('[data-open-sale-dialog]').click();
  await page.locator('[data-order-sale-form] button[type="submit"]').click();
  await expect(page.locator('.order-control-actions').getByRole('link', {name:'Открыть продажу',exact:true})).toHaveAttribute('href','/sales?source=wildberries&sale_id=fixture-sale');
  await expect(page.locator('[data-open-sale-dialog]')).toHaveCount(0);
  expect(submits).toBe(1);
});

test('card selection keeps the list DOM and uses one detail request', async ({ page }) => {
  await page.goto('/app/orders?source=wildberries');
  await page.locator('.orders-split-table-scroll').evaluate(el => {
    el.scrollTop = 350;
    el.setAttribute('data-preserved-list', 'yes');
  });
  const requests: string[] = [];
  page.on('request', request => requests.push(request.url()));
  const firstNumber = (await page.locator('.orders-split-table .order-number').nth(4).textContent())!.trim();
  const secondNumber = (await page.locator('.orders-split-table .order-number').nth(5).textContent())!.trim();
  await page.locator('.orders-split-table .order-number').nth(4).click();
  await expect(page.locator('.card-title h2')).toBeVisible();
  await expect(page.locator('[data-preserved-list]')).toHaveCount(1);
  expect(await page.locator('[data-preserved-list]').evaluate(el => el.scrollTop)).toBeGreaterThan(0);
  expect(requests.filter(url => /\/order\/wildberries\//.test(url))).toHaveLength(1);
  expect(requests.filter(url => /\/api\/orders(?:\?|$)|\/static\//.test(url))).toHaveLength(0);
  await page.locator('.orders-split-table .order-number').nth(5).click();
  await expect(page.locator('.card-title h2')).toContainText(secondNumber);
  await page.goBack();
  await expect(page.locator('.card-title h2')).toContainText(firstNumber);
  await expect(page.locator('[data-preserved-list]')).toHaveCount(1);
});

test('unified synchronization prevents duplicates and retains partial failure', async ({ page }) => {
  const posts: string[] = [];
  let finishWb!: () => void;
  const holdWb = new Promise<void>(resolve => { finishWb = resolve; });
  await page.route('**/api/orders/tictactoy/sync', async route => {
    if (route.request().method() === 'POST') posts.push('tictactoy');
    await route.fulfill({json:{ok:true,result:{outcome:'success',last_success_at:Date.now()/1000}}});
  });
  await page.route('**/api/orders/wildberries/sync', async route => {
    posts.push('wildberries');
    await holdWb;
    await route.fulfill({status:503,json:{ok:false,error:{message:'Тестовый сбой WB'}}});
  });
  await page.goto('/app/orders?source=wildberries&page=2&page_size=20');
  await page.locator('[data-sync-source="all"]').click();
  await expect(page.locator('[data-sync-row="wildberries"]')).toHaveAttribute('data-state','running');
  await expect(page.locator('[data-sync-source="all"]')).toBeDisabled();
  await expect(page.locator('[data-sync-source="wildberries"]')).toBeDisabled();
  finishWb();
  await expect(page.locator('[data-sync-row="wildberries"]')).toHaveAttribute('data-state','error');
  await expect(page.locator('[data-sync-row="tictactoy"]')).toHaveAttribute('data-state','success');
  await expect(page.locator('.list-footer')).toContainText('Показано 21–40 из 125');
  expect(posts.sort()).toEqual(['tictactoy','wildberries']);
  await page.locator('[data-sync-source="tictactoy"]').click();
  await expect.poll(() => posts.filter(x => x === 'tictactoy').length).toBe(2);
  expect(posts.filter(x => x === 'wildberries')).toHaveLength(1);
  await page.route('**/api/orders/wildberries/recovery', route => route.fulfill({json:{ok:true,diagnostics:{outcome:'success',last_success_at:new Date().toISOString()}}}));
  await page.route('**/api/orders/wildberries/sync', route => route.fulfill({json:{ok:true,result:{outcome:'success',last_success_at:new Date().toISOString()}}}));
  await page.locator('[data-sync-source="wildberries"]').click();
  await expect(page.locator('[data-sync-row="wildberries"]')).toHaveAttribute('data-state','success');
});

test('reference header fits target viewports and overflow statuses remain usable', async ({ page }) => {
  for (const [width,height] of [[1920,1080],[1440,900],[1366,768]]) {
    await page.setViewportSize({width,height});
    await page.goto('/app/orders');
    await expect(page.locator('[data-orders-sync]')).toBeVisible();
    await expect(page.locator('[data-wb-sync]')).toHaveCount(0);
    await expect(page.getByText('Обновить Tictactoy',{exact:true})).toHaveCount(0);
    const header = await page.locator('.orders-reference-header').boundingBox();
    expect(header!.height).toBeLessThan(245);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const overflow = page.locator('[data-overflow-statuses] [data-status-filter]').first();
    if (await overflow.count()) {
      await page.locator('[data-status-more] > summary').click();
      const status = await overflow.getAttribute('data-status-filter');
      await overflow.click();
      await expect(page).toHaveURL(new RegExp(`status=${status}`));
    }
    await page.screenshot({path:`test-results/orders-reference-${width}.png`,fullPage:true});
  }
});


test('page size, modes and synchronization work after lazy card selection', async ({ page }) => {
  await page.route('**/api/orders/tictactoy/sync', route => route.fulfill({json:{ok:true,result:{outcome:'success',last_success_at:Date.now()/1000}}}));
  await page.goto('/app/orders?source=wildberries');
  await page.locator('[data-orders-page-size]').selectOption('20');
  await expect(page.locator('.list-footer')).toContainText('Показано 1–20 из 125');
  await page.locator('.orders-split-table .order-number').first().click();
  await expect(page.locator('.card-title h2')).toBeVisible();
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/orders');
  await page.locator('[data-sync-source="tictactoy"]').click();
  await refreshed;
  await expect(page.locator('.list-footer')).toContainText('Показано 1–20 из 125');
  for (const mode of ['list','card','split']) {
    await page.locator(`#ordersLayoutSwitch [data-layout-mode="${mode}"]`).click();
    await expect(page.locator('#ordersWorkspace')).toHaveAttribute('data-layout-mode',mode);
    await expect(page.locator(`#ordersLayoutSwitch [data-layout-mode="${mode}"]`)).toHaveAttribute('aria-checked','true');
  }
});

test('compact rows retain density, disclosure and independent scrolling', async ({ page }) => {
  for (const width of [1920, 1440, 1376, 1280, 1024, 390]) {
    await page.setViewportSize({ width, height: width === 1376 ? 717 : 900 });
    await page.goto('/app/orders?source=wildberries');
    const row = page.locator('.orders-split-table tbody tr').first();
    await expect(row).toBeVisible();
    if (width >= 1280) {
      const density = await page.locator('.orders-split-table-scroll').evaluate(el => {
        const viewport = el.getBoundingClientRect();
        return [...el.querySelectorAll('tbody tr')].filter(row => {
          const box = row.getBoundingClientRect();
          return box.top >= viewport.top && box.bottom <= viewport.bottom;
        }).length;
      });
      // Two visible order positions intentionally make list rows taller.
      expect(density).toBeGreaterThanOrEqual(3);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const before = await row.boundingBox();
    await row.locator('summary').click();
    await expect(row.locator('details')).toHaveAttribute('open', '');
    await expect(row.locator('details').getByRole('link', {name:'Открыть карточку'})).toBeVisible();
    await expect(page.locator('.card-title h2')).toHaveCount(0);
    await row.locator('summary').click();
    await row.locator('.list-product-name').evaluate(el => { el.textContent = 'Очень длинное название товара '.repeat(20); });
    await row.locator('.order-row-customer').evaluate(el => { el.textContent = 'Длинное имя покупателя '.repeat(10); });
    const after = await row.boundingBox();
    expect(after!.height).toBeLessThanOrEqual(before!.height + 30);
    if (width >= 1280) {
      const panel = await page.locator('.order-detail-panel').boundingBox();
      await page.locator('.orders-split-table-scroll').evaluate(el => { el.scrollTop = el.scrollHeight; });
      expect(await page.locator('.order-detail-panel').boundingBox()).toEqual(panel);
      await expect(page.locator('.list-footer')).toBeInViewport();
    }
    await page.screenshot({path: 'test-results/orders-compact-' + width + '.png', fullPage: true});
  }
});
