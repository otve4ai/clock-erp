import { expect, test, type Page } from '@playwright/test';
async function selectProduct(page: Page, article: string) {
  await page.locator('#supply-product-search').fill(article);
  await page.locator('#supply-product-results .picker-result').first().click();
}
async function addProduct(page: Page, article: string, quantity = '1') {
  await page.locator('#add-item').click();
  await selectProduct(page, article);
  await page.locator('#add-quantity').fill(quantity);
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#add-item-dialog')).not.toBeVisible();
  await expect(page.locator('#add-item')).toBeEnabled();
}
async function openNewSupply(page: Page) {
  await page.locator('.add-menu summary').click();
  await page.locator('#new-supply').click();
}

test('supply posts two local movements and remains read-only', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/app/receipts');
  await expect(page.locator('[data-tab="all"]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#records')).toContainText('21096');
  await page.locator('[data-tab="supplies"]').click();
  await openNewSupply(page);
  await page.locator('#title').fill('Casio — сентябрь 2026');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Черновик сохранён');
  await addProduct(page, 'SUP-90101');
  await addProduct(page, 'SUP-90102');
  await page.locator('[data-quantity="0"]').fill('5');
  await page.locator('[data-quantity="1"]').fill('6');
  await expect(page.locator('[data-after="0"]')).toHaveText('8');
  await expect(page.locator('[data-after="1"]')).toHaveText('6');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Остаток не изменён');
  await page.locator('#post-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Поставка проведена');
  await expect(page.locator('#post-supply')).toBeHidden();
  await expect(page.locator('#title')).toBeEnabled();
  await expect(page.locator('[data-quantity]')).toHaveCount(0);
  await page.locator('#close-supply').click();
  await page.locator('[data-tab="all"]').click();
  await page.locator('#query').fill('Casio — сентябрь');
  await page.locator('#filters').getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('#records tbody tr')).toHaveCount(1);
  await page.locator('#records [data-open]').first().click();
  await expect(page.locator('#title')).toBeEnabled();
  await page.locator('#close-supply').click();
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(() => page.locator('main').evaluate((el) => el.getBoundingClientRect().width))
      .toBeGreaterThan(width > 767 ? width - 250 : width - 20);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    );
    expect(overflow).toBe(false);
  }
  expect(errors).toEqual([]);
  await page.screenshot({ path: '/tmp/clock-erp-supplies-mobile.png', fullPage: true });
});

test('draft validation, duplicate prevention, search and pagination', async ({ page, request }) => {
  for (let i = 0; i < 28; i++) {
    const response = await request.post('/api/v1/receipts/supplies', {
      data: { title: `Pagination supply ${i}` },
    });
    expect(response.status()).toBe(201);
  }
  await page.goto('/app/receipts?tab=supplies');
  await page.locator('#query').fill('Pagination supply');
  await page.locator('#filters').getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('#count')).toHaveText('28');
  await expect(page.locator('#quantity')).toHaveText('0');
  await expect(page.locator('#records tbody tr')).toHaveCount(25);
  await page.locator('#next').click();
  await expect(page.locator('#records tbody tr')).toHaveCount(3);
  await openNewSupply(page);
  await page.locator('#title').fill('Duplicate protection');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Черновик сохранён');
  await addProduct(page, 'SUP-90101');
  await page.locator('#add-item').click();
  await selectProduct(page, 'SUP-90101');
  await expect(page.locator('#duplicate-confirmation')).toContainText('Этот товар уже есть');
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#add-item-dialog')).not.toBeVisible();
  await expect(page.locator('#supply-totals')).toContainText('Единиц: 2');
  await expect(page.locator('#items tbody tr')).toHaveCount(1);
  await page.locator('[data-quantity="0"]').fill('0');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('целым положительным');
  await page.locator('[data-quantity="0"]').fill('2');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Черновик сохранён');
  await page.screenshot({ path: '/tmp/clock-erp-supplies-desktop.png', fullPage: true });
  await page.locator('#delete-supply').click();
  await page.locator('#continue-delete').click();
  await expect(page.locator('#delete-preview-dialog')).toContainText('Черновик будет удалён');
  await page.locator('#confirm-delete').click();
  await expect(page.locator('#supply-dialog')).not.toBeVisible();
  await page.locator('[data-tab="cancellations"]').click();
  await page.locator('#filters').getByRole('button', { name: 'Сбросить' }).click();
  await expect(page.locator('#records')).toContainText('21096');
  await expect(page.locator('#count')).toHaveText('1');
});

test('period filter uses the displayed local calendar day', async ({ page }) => {
  await page.route('**/api/v1/receipts/documents', (route) =>
    route.fulfill({
      json: {
        ok: true,
        data: [
          {
            id: 'midnight',
            source_type: 'supply',
            title: 'After midnight',
            number: 'QA-1',
            created_at: '2026-09-07T22:30:00+00:00',
            total_quantity: 1,
            position_count: 1,
            status: 'posted',
          },
        ],
      },
    }),
  );
  await page.goto('/app/receipts');
  await page.locator('#date-from').fill('2026-09-08');
  await page.locator('#date-to').fill('2026-09-08');
  await page.locator('#filters').getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('#count')).toHaveText('1');
  await expect(page.locator('#records')).toContainText('08.09.2026');
});

test('receipt tabs share one table grid on desktop and mobile', async ({
  page,
  request,
}, testInfo) => {
  const tabs = [
    { key: 'all', headers: ['Дата', 'Тип', 'Номер', 'Название / описание', 'Склад'] },
    { key: 'supplies', headers: ['Дата', 'Тип', 'Номер', 'Название / описание', 'Склад'] },
    { key: 'receipts', headers: ['Дата', 'Тип', 'Номер', 'Название / описание', 'Склад'] },
    { key: 'cancellations', headers: ['Дата', 'Тип', 'Номер', 'Название / описание', 'Склад'] },
  ];

  await request.post('/api/v1/receipts/supplies', {
    data: {
      title: 'Table alignment fixture',
      comment: 'Длинный комментарий для проверки безопасного ограничения текста в ячейке',
    },
  });
  await page.setViewportSize({ width: 1536, height: 960 });
  await page.goto('/app/receipts');
  const tableFrame = page.locator('.main > .table-scroll');
  const initialFrame = await tableFrame.boundingBox();
  expect(initialFrame).not.toBeNull();

  for (const currentTab of tabs) {
    await page.locator(`[data-tab="${currentTab.key}"]`).click();
    await expect(page.locator('#records thead th')).toHaveCount(10);
    await expect(page.locator('#records thead th')).toContainText(currentTab.headers);
    const frame = await tableFrame.boundingBox();
    expect(frame?.x).toBe(initialFrame?.x);
    expect(frame?.width).toBe(initialFrame?.width);
    const aligned = await page.locator('#records').evaluate((table) => {
      const headers = Array.from(table.querySelectorAll('thead th'));
      const cells = Array.from(table.querySelectorAll('tbody tr:first-child td'));
      if (cells.length === 1 && cells[0].hasAttribute('colspan')) {
        return Number(cells[0].getAttribute('colspan')) === headers.length;
      }
      return headers.every((header, index) => {
        const headerBox = header.getBoundingClientRect();
        const cellBox = cells[index]?.getBoundingClientRect();
        return (
          cellBox &&
          Math.abs(headerBox.x - cellBox.x) < 0.5 &&
          Math.abs(headerBox.width - cellBox.width) < 0.5
        );
      });
    });
    expect(aligned).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`receipt-${currentTab.key}-desktop.png`),
      fullPage: true,
    });
  }

  await page.locator('[data-tab="supplies"]').click();
  await expect(page.locator('#records')).toContainText('Table alignment fixture');
  const typeNumberGap = await page.locator('#records').evaluate((table) => {
    const type = table.querySelector('tbody tr:first-child td[data-column="type"]');
    const numberCell = table.querySelector('tbody tr:first-child td[data-column="number"]');
    if (!type || !numberCell) return null;
    const typeBox = type.getBoundingClientRect();
    const numberBox = numberCell.getBoundingClientRect();
    return numberBox.x - (typeBox.x + typeBox.width);
  });
  expect(typeNumberGap).not.toBeNull();
  expect(typeNumberGap as number).toBeGreaterThanOrEqual(0);

  const authorHeader = page.locator('#records th[data-column="author"]');
  const authorWidth = await authorHeader.evaluate(
    (element) => element.getBoundingClientRect().width,
  );
  const resizeHandle = authorHeader.locator('.erp-column-resize-handle');
  const resizeBox = await resizeHandle.boundingBox();
  expect(resizeBox).not.toBeNull();
  await page.mouse.move(resizeBox!.x + resizeBox!.width / 2, resizeBox!.y + resizeBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    resizeBox!.x + resizeBox!.width / 2 + 28,
    resizeBox!.y + resizeBox!.height / 2,
  );
  await page.mouse.up();
  await expect
    .poll(() => authorHeader.evaluate((element) => element.getBoundingClientRect().width))
    .toBeGreaterThan(authorWidth + 20);

  const authorDragBox = await authorHeader.boundingBox();
  const numberHeader = page.locator('#records th[data-column="number"]');
  const numberDragBox = await numberHeader.boundingBox();
  expect(authorDragBox && numberDragBox).toBeTruthy();
  await page.mouse.move(
    authorDragBox!.x + authorDragBox!.width / 2,
    authorDragBox!.y + authorDragBox!.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(numberDragBox!.x + numberDragBox!.width - 8, numberDragBox!.y + 10, {
    steps: 8,
  });
  await page.mouse.up();
  await expect
    .poll(() =>
      page
        .locator('#records thead th')
        .evaluateAll((headers) => headers.map((header) => header.getAttribute('data-column'))),
    )
    .toEqual([
      'date',
      'type',
      'number',
      'author',
      'title',
      'warehouse',
      'positions',
      'quantity',
      'status',
      'actions',
    ]);

  await page.locator('#filters details summary').click();
  await page.locator('#columns input[data-col="warehouse"]').uncheck();
  await expect(page.locator('#records th[data-column="warehouse"]')).toBeHidden();
  await page.reload();
  await expect(page.locator('#records th[data-column="warehouse"]')).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  for (const currentTab of tabs) {
    await page.locator(`[data-tab="${currentTab.key}"]`).click();
    await expect(page.locator('#records')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(
      false,
    );
    expect(await tableFrame.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
      true,
    );
    await page.screenshot({
      path: testInfo.outputPath(`receipt-${currentTab.key}-mobile.png`),
      fullPage: true,
    });
  }
});

test('admin edits and deletes a posted supply through the two-stage preview', async ({
  page,
  request,
}) => {
  const product = (await (await request.post('/api/v1/receipts/bitrix/90101')).json()).data;
  const supply = (
    await (
      await request.post('/api/v1/receipts/supplies', {
        data: {
          title: 'Delete preview smoke',
          comment: 'Before edit',
          items: [{ product_id: product.id, quantity: 2 }],
        },
      })
    ).json()
  ).data;
  await request.post(`/api/v1/receipts/supplies/${supply.id}/post`);

  await page.goto('/app/receipts?tab=supplies');
  await page
    .locator('#records tr')
    .filter({ hasText: 'Delete preview smoke' })
    .getByRole('button', { name: 'Открыть' })
    .click();
  await page.locator('#title').fill('Delete preview edited');
  await page.locator('#comment').fill('Only neutral fields changed');
  await page.locator('#save-supply').click();
  await expect(page.locator('#dialog-message')).toContainText('Остаток не изменён');

  await page.locator('#delete-supply').click();
  await expect(page.locator('#delete-confirm-dialog')).toBeVisible();
  await page.locator('#delete-confirm-dialog [data-close-delete]').last().click();
  await expect(page.locator('#delete-confirm-dialog')).not.toBeVisible();
  await page.locator('#delete-supply').click();
  await page.locator('#continue-delete').click();
  await expect(page.locator('#delete-preview-dialog')).toBeVisible();
  await expect(page.locator('#delete-preview-dialog')).toContainText('Остаток будет уменьшен');
  await expect(page.locator('#delete-preview-items tr')).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(
    false,
  );
  const dialog = await page.locator('#delete-preview-dialog').boundingBox();
  const footer = await page.locator('.delete-preview-actions').boundingBox();
  expect(dialog && dialog.width <= 390).toBe(true);
  expect(footer && footer.y + footer.height <= 845).toBe(true);

  let deleteRequests = 0;
  page.on('request', (event) => {
    if (
      event.method() === 'DELETE' &&
      event.url().endsWith(`/supplies/${encodeURIComponent(supply.id)}`)
    ) {
      deleteRequests += 1;
    }
  });
  await page.locator('#confirm-delete').evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(page.locator('#delete-preview-dialog')).not.toBeVisible();
  expect(deleteRequests).toBe(1);
  await expect(page.locator('#message')).toContainText('Поставка удалена');
  await expect(page.locator('#records')).not.toContainText('Delete preview edited');
});

test('append goods to a posted supply using the shared ERP picker', async ({ page, request }) => {
  const product = (await (await request.post('/api/v1/receipts/bitrix/90101')).json()).data;
  const other = (await (await request.post('/api/v1/receipts/bitrix/90102')).json()).data;
  const supply = (
    await (
      await request.post('/api/v1/receipts/supplies', {
        data: { title: 'Additional goods smoke', items: [{ product_id: product.id, quantity: 3 }] },
      })
    ).json()
  ).data;
  await request.post(`/api/v1/receipts/supplies/${supply.id}/post`);
  const writes: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST') writes.push(r.url());
  });
  await page.goto('/app/receipts?tab=supplies');
  await page
    .locator('#records tr')
    .filter({ hasText: 'Additional goods smoke' })
    .getByRole('button', { name: 'Открыть' })
    .click();
  await page.locator('#add-item').click();
  await selectProduct(page, 'SUP-90102');
  await expect(page.locator('#selected-product')).toContainText('Остаток:');
  await page.locator('#add-quantity').fill('2');
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#add-item-dialog')).not.toBeVisible();
  await expect(page.locator('#items tbody tr')).toHaveCount(2);
  await expect(page.locator('#items')).toContainText('Casio F91W');
  await expect(page.locator('#supply-totals')).toHaveText('Позиций: 2 · Единиц: 5');
  await expect(page.locator('#dialog-message')).toContainText('+2 шт.');
  await page.locator('#add-item').click();
  await expect(page.locator('#confirm-add-item')).toBeDisabled();
  await selectProduct(page, 'SUP-90102');
  await page.locator('#add-quantity').fill('2');
  await expect(page.locator('#duplicate-confirmation')).toContainText(
    'Сейчас: 2 шт. Добавить ещё 2 шт.?',
  );
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#supply-totals')).toHaveText('Позиций: 2 · Единиц: 7');
  await expect(page.locator('#post-supply')).toBeHidden();
  const result = (await (await request.get(`/api/v1/receipts/supplies/${supply.id}`)).json()).data;
  expect(result.status).toBe('posted');
  expect(result.items.find((i: { product_id: number }) => i.product_id === other.id).quantity).toBe(
    4,
  );
  expect(writes).toHaveLength(2);
  expect(writes.every((url) => url.endsWith('/items'))).toBe(true);
  await page.locator('#add-item').click();
  await page.locator('#supply-product-search').fill('NONEXISTENT-SUPPLY-SKU');
  await expect(page.locator('#add-item-form')).toContainText(
    'Выберите источник Bitrix для поиска и импорта',
  );
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(
      false,
    );
  }
});

test('a lost response and refresh retry the same addition exactly once', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => Object.defineProperty(crypto, 'randomUUID', { value: undefined }));
  const p = (await (await request.post('/api/v1/receipts/bitrix/90101')).json()).data;
  const supply = (
    await (
      await request.post('/api/v1/receipts/supplies', {
        data: { title: 'Retry smoke', items: [{ product_id: p.id, quantity: 1 }] },
      })
    ).json()
  ).data;
  await request.post(`/api/v1/receipts/supplies/${supply.id}/post`);
  await page.goto('/app/receipts?tab=supplies');
  const open = async () => {
    await page
      .locator('#records tr')
      .filter({ hasText: 'Retry smoke' })
      .getByRole('button', { name: 'Открыть' })
      .click();
    await page.locator('#add-item').click();
  };
  await open();
  await selectProduct(page, 'SUP-90101');
  await page.locator('#add-quantity').fill('2');
  await page.route(
    '**/supplies/*/items',
    async (route) => {
      await route.fetch();
      await route.abort('failed');
    },
    { times: 1 },
  );
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#add-item-message')).toBeVisible();
  await page.reload();
  await open();
  await expect(page.locator('#confirm-add-item')).toHaveText('Проверить предыдущую операцию');
  await page.locator('#confirm-add-item').click();
  await expect(page.locator('#supply-totals')).toHaveText('Позиций: 1 · Единиц: 3');
  const result = (await (await request.get(`/api/v1/receipts/supplies/${supply.id}`)).json()).data;
  expect(result.total_quantity).toBe(3);
  expect(Object.keys(result.additions)).toHaveLength(1);
});
