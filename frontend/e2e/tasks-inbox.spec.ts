import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('read-only acquaintance, normal acceptance, persistent micro timer and split badges', async ({
  page,
}, testInfo) => {
  await page.clock.install();
  await page.goto('/app/tasks-module?view=inbox');
  await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'false');
  const title = `Incoming ${testInfo.project.name} ${Date.now()}`;
  const createIncoming = (route: string, title: string) =>
    page.evaluate(
      async ({ route, title }) => {
        const csrf = (window as unknown as { TASKS_MODULE_BOOTSTRAP: { csrf: string } })
          .TASKS_MODULE_BOOTSTRAP.csrf;
        const response = await fetch('/api/v1/tasks-module/' + route, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrf,
            'X-Preview-Actor': '2',
          },
          body: JSON.stringify({ title, assigned_to: 1 }),
        });
        return { status: response.status, body: await response.json() };
      },
      { route, title },
    );
  const normalResponse = await createIncoming('tasks', title);
  expect(normalResponse.status, JSON.stringify(normalResponse.body)).toBe(201);
  const normal = normalResponse.body.data;
  expect(normal.status).toBe('waiting');
  const microResponse = await createIncoming('microtasks', `⚡ ${title}`);
  expect(microResponse.status).toBe(201);
  const micro = microResponse.body.data;
  await page.reload();
  const normalRow = page.locator(`[data-inbox-task="${normal.id}"]`);
  const microRow = page.locator(`[data-inbox-task="${micro.id}"]`);
  await expect(normalRow).toBeVisible();
  await expect(microRow).toContainText('МИКРОЗАДАЧА · 24 ЧАСА');
  await expect(page.locator('#tm-inbox-nav-count')).toBeVisible();
  await expect(page.locator('.tm-nav [data-tasks-module-badge="micro"]')).toContainText('⚡');
  const readRequests: string[] = [];
  page.on('request', (request) => {
    if (/\/inbox\/\d+\/read/.test(request.url())) readRequests.push(request.url());
  });
  await normalRow.getByRole('button', { name: 'Ознакомиться' }).click();
  const drawer = page.locator('#tm-dialog');
  await expect(drawer).toContainText('Ожидает принятия');
  await expect(drawer.locator('input,select,textarea')).toHaveCount(0);
  await expect(drawer.getByRole('button', { name: 'Сохранить', exact: true })).toHaveCount(0);
  await page.locator('#tm-dialog-close').click();
  await expect(normalRow).toBeVisible();
  expect(readRequests).toEqual([]);
  await normalRow.getByRole('button', { name: 'Взять в работу' }).click();
  await expect(normalRow).toHaveCount(0);
  const accepted = await page.evaluate(
    async (id) => (await (await fetch(`/api/v1/tasks-module/tasks/${id}`)).json()).data,
    normal.id,
  );
  expect(accepted.status).toBe('in_progress');
  expect(accepted.version).toBe(2);
  await microRow.getByRole('button', { name: 'Ознакомиться' }).click();
  await expect(drawer).toContainText('24 часа отсчитываются от создания');
  await expect(drawer.locator('input,select,textarea')).toHaveCount(0);
  await expect(drawer.getByRole('button', { name: 'Принять', exact: true })).toHaveCount(0);
  await page.locator('#tm-dialog-close').click();
  await expect(microRow).toBeVisible();
  const timer = microRow.locator('[data-micro-deadline]');
  const initial = await timer.innerText();
  await page.clock.install();
  await page.clock.fastForward(61000);
  await expect(timer).not.toHaveText(initial);
  await expect(page.getByRole('button', { name: 'Прочитано', exact: true })).toHaveCount(0);
  await microRow.getByRole('button', { name: 'Готово', exact: true }).click();
  await expect(microRow).toHaveCount(0);
  const done = await page.evaluate(
    async (id) => (await (await fetch(`/api/v1/tasks-module/tasks/${id}`)).json()).data,
    micro.id,
  );
  expect(done.status).toBe('done');
  expect(done.micro_deadline_at).toBe(micro.micro_deadline_at);
  expect(readRequests).toEqual([]);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1),
  ).toBe(false);
  const a11y = await new AxeBuilder({ page }).include('.tm-main').analyze();
  expect(a11y.violations).toEqual([]);
});

test('overdue micro is explicit and stays in Inbox', async ({ page }) => {
  await page.clock.install();
  await page.goto('/app/tasks-module?view=inbox');
  const overdue = page
    .locator('.tm-inbox-micro')
    .filter({ hasText: 'Просроченная микрозадача остаётся во входящих' });
  await expect(overdue).toBeVisible();
  await expect(overdue.locator('[data-micro-deadline]')).toContainText('Просрочено');
  await expect(overdue.locator('[data-micro-deadline]')).toHaveClass(/tm-overdue/);
  await overdue.getByRole('button', { name: 'Ознакомиться' }).click();
  await page.locator('#tm-dialog-close').click();
  await expect(overdue).toBeVisible();
});
