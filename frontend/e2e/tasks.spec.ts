import { expect, test } from '@playwright/test';

test('one Tasks entry on desktop and mobile; old links discard legacy IDs', async ({ page }) => {
  await page.goto('/app/tasks?task=1&task_id=2&view=calendar');
  await expect(page).toHaveURL(/\/app\/tasks-module$/);
  await expect(page.locator('#tm-create')).toBeVisible();
  await expect(page.locator('.sidebar-nav [data-navigation-key="tasks"]')).toHaveCount(1);
  await expect(page.locator('#mobileErpMoreSheet [data-navigation-key="tasks"]')).toHaveCount(1);
  for (const anchor of await page.locator('[data-navigation-key="tasks"]').all()) {
    await expect(anchor).toHaveAttribute('href', '/app/tasks-module');
    await expect(anchor).toContainText('Задачи');
  }
  await expect(page.getByRole('link', { name: 'Новые задачи', exact: true })).toHaveCount(0);
  await expect(page.locator('#tm-dialog')).toBeHidden();
  const retired = await page.request.get('/api/v1/tasks/1');
  expect(retired.status()).toBe(410);
  const active = await page.request.get('/api/v1/tasks-module/tasks');
  expect(active.status()).toBe(200);
  const sharedInboxBadge = await page.request.get('/api/v1/inbox/badge');
  expect(sharedInboxBadge.status()).toBe(200);
  expect((await sharedInboxBadge.json()).data.count).toBeGreaterThanOrEqual(0);
  await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'false');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
});

test('new Tasks create, complete and archive still work', async ({ page }, testInfo) => {
  const title = `Retirement CRUD ${testInfo.project.name} ${Date.now()}`;
  await page.goto('/app/tasks-module');
  await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'false');
  await page.locator('#tm-create').click();
  const dialog = page.locator('#tm-dialog');
  await dialog.getByLabel('Название', { exact: true }).fill(title);
  await dialog.getByRole('button', { name: 'Создать', exact: true }).click();
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(title);
  await expect(dialog.getByRole('button', { name: 'Сохранить', exact: true })).toBeVisible();
  await dialog.locator('select[name="status"]').selectOption('done');
  const saved = page.waitForResponse(response => response.request().method() === 'PATCH' && response.url().includes('/api/v1/tasks-module/tasks/'));
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click();
  expect((await saved).status()).toBe(200);
  await page.locator('#tm-dialog-close').click();
  await page.locator('.tm-nav [data-view="archive"]').click();
  await page.locator('#tm-search').fill(title);
  await expect(page.locator('#tm-list')).toContainText(title);
});

test('microtasks use only the new namespace', async ({ page }, testInfo) => {
  const legacyRequests: string[] = [];
  page.on('request', request => {
    if (/\/api\/v1\/tasks(?:[/?]|$)/.test(request.url())) legacyRequests.push(request.url());
  });
  await page.goto('/app/tasks-module');
  await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'false');
  const title = `Micro retirement ${testInfo.project.name} ${Date.now()}`;
  await page.getByLabel('Название микрозадачи', { exact: true }).fill(title);
  await page.getByRole('button', { name: 'Добавить микрозадачу', exact: true }).click();
  await page.locator('.tm-nav [data-view="micro"]').click();
  await page.locator('#tm-search').fill(title);
  await expect(page.locator('#tm-list')).toContainText(title);
  expect(legacyRequests).toEqual([]);
});
