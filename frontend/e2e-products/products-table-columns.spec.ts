import { expect, test, type Page } from '@playwright/test';

const storageKeys = {
  v1: 'vechasu.warehouse.table-view.v1',
  v2: 'vechasu.warehouse.table-view.v2',
  v3: 'vechasu.warehouse.table-view.v3',
};
const measuredKeys = ['name', 'stock', 'price', 'article'] as const;
const tolerance = 1.25;

type Measurements = {
  columns: Record<(typeof measuredKeys)[number], number>;
  table: number;
};

async function clearTableStorage(page: Page) {
  await page.evaluate((keys) => {
    Object.values(keys).forEach((key) => localStorage.removeItem(key));
  }, storageKeys);
}

async function measure(page: Page): Promise<Measurements> {
  return page.evaluate((keys) => {
    const table = document.querySelector<HTMLTableElement>('#warehouseProductsTable');
    if (!table) throw new Error('Products table was not found');
    const columns = Object.fromEntries(
      keys.map((key) => {
        const header = table.querySelector<HTMLElement>(`thead th[data-column-key="${key}"]`);
        if (!header) throw new Error(`Column ${key} was not found`);
        return [key, header.getBoundingClientRect().width];
      }),
    ) as Measurements['columns'];
    return { columns, table: table.getBoundingClientRect().width };
  }, measuredKeys);
}

async function resizeColumn(page: Page, key: string, delta: number) {
  const handle = page.locator(
    `#warehouseProductsTable th[data-column-key="${key}"] .sales-column-resize-handle`,
  );
  await expect(handle).toHaveCount(1);
  const box = await handle.boundingBox();
  expect(box).not.toBeNull();
  const startX = box!.x + box!.width / 2;
  const startY = box!.y + box!.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX + delta, startY, { steps: 8 });
  await page.mouse.up();
}

function expectClose(actual: number, expected: number, message: string) {
  expect(Math.abs(actual - expected), message).toBeLessThanOrEqual(tolerance);
}

async function setColumnVisibility(page: Page, key: string, visible: boolean) {
  const trigger = page.locator('#warehouseColumnSettingsTrigger');
  const panel = page.locator('#warehouseColumnSettingsPanel');
  if (!(await panel.isVisible())) await trigger.click();
  const checkbox = panel.locator(`input[data-column-key="${key}"]`);
  await expect(checkbox).toBeVisible();
  if ((await checkbox.isChecked()) !== visible) await checkbox.click();
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
}

test('resizing one product column keeps neighbours fixed and persists', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/app/products', { waitUntil: 'load' });
  await clearTableStorage(page);
  await page.reload({ waitUntil: 'load' });
  await expect(page.locator('#warehouseProductsTable tbody tr').first()).toBeVisible();

  const before = await measure(page);
  await resizeColumn(page, 'price', 40);
  const after = await measure(page);

  expectClose(after.columns.price - before.columns.price, 40, 'price delta');
  for (const key of ['name', 'stock', 'article'] as const) {
    expectClose(after.columns[key], before.columns[key], `${key} changed during price resize`);
  }
  expectClose(after.table - before.table, 40, 'table width delta');

  const stored = await page.evaluate(
    (key) => JSON.parse(localStorage.getItem(key) || '{}'),
    storageKeys.v3,
  );
  expect(stored.order).not.toContain('actions');
  expect(stored.customWidths).not.toContain('actions');
  expect(stored.customWidths).toEqual(expect.arrayContaining([...measuredKeys]));

  const actionContract = await page.evaluate(() => {
    const table = document.querySelector<HTMLTableElement>('#warehouseProductsTable')!;
    const actionCol = table.querySelector<HTMLTableColElement>('col[data-system-column="actions"]');
    const actionHeader = table.querySelector<HTMLElement>('th[data-system-column="actions"]');
    return {
      colWidth: actionCol?.style.width,
      headerWidth: actionHeader?.getBoundingClientRect().width,
      hasHandle: Boolean(actionHeader?.querySelector('.sales-column-resize-handle')),
      legacyActions: table.querySelectorAll('[data-column-key="actions"]').length,
      headerIsLast: actionHeader === table.querySelector('thead tr')?.lastElementChild,
    };
  });
  expect(actionContract.colWidth).toBe('82px');
  expectClose(actionContract.headerWidth || 0, 82, 'actions width');
  expect(actionContract.hasHandle).toBe(false);
  expect(actionContract.legacyActions).toBe(0);
  expect(actionContract.headerIsLast).toBe(true);

  await page.reload({ waitUntil: 'load' });
  const persisted = await measure(page);
  for (const key of measuredKeys) {
    expectClose(persisted.columns[key], after.columns[key], `${key} did not persist`);
  }
  expectClose(persisted.table, after.table, 'table width did not persist');
});

test('hidden columns do not redistribute custom widths', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/app/products', { waitUntil: 'load' });
  await clearTableStorage(page);
  await page.reload({ waitUntil: 'load' });

  await resizeColumn(page, 'price', 40);
  const fixed = await measure(page);
  await setColumnVisibility(page, 'article', false);
  const hidden = await measure(page);
  for (const key of ['name', 'stock', 'price'] as const) {
    expectClose(hidden.columns[key], fixed.columns[key], `${key} changed when article was hidden`);
  }

  await resizeColumn(page, 'stock', 30);
  const resizedWhileHidden = await measure(page);
  expectClose(
    resizedWhileHidden.columns.stock - hidden.columns.stock,
    30,
    'stock delta while article is hidden',
  );
  expectClose(
    resizedWhileHidden.columns.name,
    hidden.columns.name,
    'name changed during stock resize',
  );
  expectClose(
    resizedWhileHidden.columns.price,
    hidden.columns.price,
    'price changed during stock resize',
  );

  await setColumnVisibility(page, 'article', true);
  const restored = await measure(page);
  expectClose(restored.columns.article, fixed.columns.article, 'restored article width');
  expectClose(restored.columns.name, fixed.columns.name, 'name changed after article restore');
  expectClose(restored.columns.price, fixed.columns.price, 'price changed after article restore');
  expectClose(
    restored.columns.stock,
    fixed.columns.stock + 30,
    'stock width after article restore',
  );

  await page.reload({ waitUntil: 'load' });
  const persisted = await measure(page);
  for (const key of measuredKeys) {
    expectClose(
      persisted.columns[key],
      restored.columns[key],
      `${key} hidden/custom state did not persist`,
    );
  }
});

test('v1 and v2 settings migrate safely with pinned and system columns normalized', async ({
  page,
}) => {
  const legacyStates = [
    {
      key: storageKeys.v2,
      value: {
        order: ['brand', 'name', 'photo', 'brand', 'actions', 'unknown', 'price'],
        widths: {
          brand: 140,
          article: 128,
          name: 'broken',
          stock: -5,
          price: 'not-a-width',
          actions: 999,
          unknown: 444,
        },
        hidden: ['actions', 'photo', 'brand', 'brand', 'unknown'],
      },
    },
    {
      key: storageKeys.v1,
      value: {
        order: ['brand', 'name', 'photo', 'actions'],
        hidden: ['cell'],
      },
    },
  ];

  for (const legacy of legacyStates) {
    await page.goto('/app/products', { waitUntil: 'load' });
    await page.evaluate(
      ({ keys, state }) => {
        Object.values(keys).forEach((key) => localStorage.removeItem(key));
        localStorage.setItem(state.key, JSON.stringify(state.value));
      },
      { keys: storageKeys, state: legacy },
    );
    await page.reload({ waitUntil: 'load' });

    const migrated = await page.evaluate(
      (key) => JSON.parse(localStorage.getItem(key) || '{}'),
      storageKeys.v3,
    );
    expect(migrated.version).toBe(3);
    expect(migrated.order.slice(0, 3)).toEqual(['photo', 'name', 'brand']);
    expect(new Set(migrated.order).size).toBe(migrated.order.length);
    expect(migrated.order).not.toContain('actions');
    expect(migrated.order).not.toContain('unknown');
    expect(migrated.customWidths).not.toContain('actions');
    expect(migrated.customWidths).not.toContain('unknown');
    expect(migrated.hidden).not.toContain('actions');
    expect(migrated.hidden).not.toContain('photo');
    expect(migrated.widths.actions).toBeUndefined();
    expect(migrated.widths.unknown).toBeUndefined();
    expect(Number.isFinite(migrated.widths.name)).toBe(true);
    expect(Number.isFinite(migrated.widths.stock)).toBe(true);
    expect(Number.isFinite(migrated.widths.price)).toBe(true);

    const domContract = await page.evaluate(() => {
      const table = document.querySelector<HTMLTableElement>('#warehouseProductsTable')!;
      const headers = Array.from(table.querySelectorAll('thead th'));
      const rows = Array.from(table.querySelectorAll('tbody tr'));
      return {
        firstKeys: headers.slice(0, 3).map((header) => (header as HTMLElement).dataset.columnKey),
        actionHeaderLast: headers.at(-1)?.getAttribute('data-system-column') === 'actions',
        actionColLast:
          table.querySelector('colgroup')?.lastElementChild?.getAttribute('data-system-column') ===
          'actions',
        actionCellsLast: rows.every(
          (row) => row.lastElementChild?.getAttribute('data-system-column') === 'actions',
        ),
        actionHandleCount: table.querySelectorAll(
          'th[data-system-column="actions"] .sales-column-resize-handle',
        ).length,
      };
    });
    expect(domContract.firstKeys).toEqual(['photo', 'name', 'brand']);
    expect(domContract.actionHeaderLast).toBe(true);
    expect(domContract.actionColLast).toBe(true);
    expect(domContract.actionCellsLast).toBe(true);
    expect(domContract.actionHandleCount).toBe(0);

    await page.locator('#warehouseColumnSettingsTrigger').click();
    await page.locator('#warehouseTableReset').click();
    const reset = await page.evaluate(
      (keys) => ({
        v1: localStorage.getItem(keys.v1),
        v2: localStorage.getItem(keys.v2),
        v3: localStorage.getItem(keys.v3),
        order: Array.from(
          document.querySelectorAll<HTMLElement>(
            '#warehouseProductsTable thead th[data-column-key]',
          ),
        ).map((header) => header.dataset.columnKey),
      }),
      storageKeys,
    );
    expect(reset.v1).toBeNull();
    expect(reset.v2).toBeNull();
    expect(reset.v3).toBeNull();
    expect(reset.order.slice(0, 3)).toEqual(['photo', 'name', 'stock']);
  }
});
