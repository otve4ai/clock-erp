import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('loading list preserves readable contrast', async ({ page }) => {
  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(/\/api\/v1\/tasks-module\/tasks\?/, async (route) => {
    await pending;
    await route.continue();
  });
  try {
    await page.goto('/app/tasks-module');
    await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('#tm-list .tm-loading')).toBeVisible();
    const result = await new AxeBuilder({ page }).include('#tm-list').analyze();
    expect(result.violations).toEqual([]);
  } finally {
    release();
  }
  await expect(page.locator('#tm-list')).toHaveAttribute('aria-busy', 'false');
});

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
  await expect(microRow).toContainText('Микрозадача · 24 ч');
  await expect(microRow.locator('.tm-inbox-content')).not.toContainText('Срок:');
  await expect(microRow.locator('.tm-inbox-content [data-micro-deadline]')).toHaveCount(0);
  await expect(microRow.locator('.tm-inbox-actions [data-micro-deadline]')).toBeVisible();
  const order = await page
    .locator('[data-inbox-task]')
    .evaluateAll((rows) => rows.map((row) => row.classList.contains('tm-inbox-micro')));
  expect(order).toEqual([...order].sort((a, b) => Number(b) - Number(a)));
  if (testInfo.project.use.viewport!.width > 780) {
    const geometry = await page.evaluate(
      ({ normalId, microId }) => {
        const normal = document.querySelector(`[data-inbox-task="${normalId}"]`)!;
        const micro = document.querySelector(`[data-inbox-task="${microId}"]`)!;
        const box = (element: Element) => element.getBoundingClientRect();
        const center = (element: Element) => {
          const b = box(element);
          return b.x + b.width / 2;
        };
        return {
          heights: [box(normal).height, box(micro).height],
          centers: [
            center(normal.querySelector('.tm-primary')!),
            center(micro.querySelector('.tm-inbox-timer')!),
          ],
        };
      },
      { normalId: normal.id, microId: micro.id },
    );
    expect(Math.abs(geometry.heights[0] - geometry.heights[1])).toBeLessThan(1);
    expect(Math.abs(geometry.centers[0] - geometry.centers[1])).toBeLessThan(1);
  }
  await expect(page.locator('#tm-inbox-nav-count')).toBeVisible();
  await expect(page.locator('.tm-nav [data-tasks-module-badge="micro"]')).toHaveText(/^⚡\d+$/);
  const polish = await page.evaluate(
    ({ normalId, microId }) => {
      const accept = document.querySelector(`[data-inbox-task="${normalId}"] .tm-primary`)!;
      const timer = document.querySelector(`[data-inbox-task="${microId}"] .tm-inbox-timer`)!;
      const box = (element: Element) => element.getBoundingClientRect();
      const counts = Array.from(
        document.querySelectorAll('.tm-metric-inbox .tm-metric-counts strong'),
      );
      return {
        widths: [box(accept).width, box(timer).width],
        heights: [box(accept).height, box(timer).height],
        radii: [getComputedStyle(accept).borderRadius, getComputedStyle(timer).borderRadius],
        cursor: getComputedStyle(timer).cursor,
        role: timer.getAttribute('role'),
        countsCenters: counts.map((element) => box(element).y + box(element).height / 2),
        metricHeights: Array.from(document.querySelectorAll('.tm-metric')).map(
          (element) => box(element).height,
        ),
        sidebarAlignment: Array.from(document.querySelectorAll('.tasks-sidebar-counts'))
          .filter((element) => box(element).width > 0)
          .map((group) => {
            const badges = Array.from(group.children).filter((element) => box(element).width > 0);
            return (
              badges.length < 2 ||
              Math.abs(
                box(badges[0]).y +
                  box(badges[0]).height / 2 -
                  box(badges[1]).y -
                  box(badges[1]).height / 2,
              ) < 1
            );
          }),
      };
    },
    { normalId: normal.id, microId: micro.id },
  );
  expect(Math.abs(polish.widths[0] - polish.widths[1])).toBeLessThan(1);
  expect(Math.abs(polish.heights[0] - polish.heights[1])).toBeLessThan(1);
  expect(polish.radii[0]).toBe(polish.radii[1]);
  expect(polish.cursor).toBe('default');
  expect(polish.role).not.toBe('button');
  expect(Math.abs(polish.countsCenters[0] - polish.countsCenters[1])).toBeLessThan(1);
  expect(Math.max(...polish.metricHeights) - Math.min(...polish.metricHeights)).toBeLessThan(1);
  if (testInfo.project.use.viewport!.width > 780) {
    expect(polish.sidebarAlignment.length).toBeGreaterThan(0);
  }
  expect(polish.sidebarAlignment.every(Boolean)).toBe(true);
  await expect(microRow.getByRole('button', { name: 'Готово', exact: true })).toHaveCount(0);
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
  await expect(drawer.getByRole('button', { name: 'Готово', exact: true })).toHaveCount(0);
  await page.locator('#tm-dialog-close').click();
  await expect(microRow).toBeVisible();
  const timer = microRow.locator('[data-micro-deadline]');
  const initial = await timer.innerText();
  await page.clock.install();
  await page.clock.fastForward(61000);
  await expect(timer).not.toHaveText(initial);
  await expect(page.getByRole('button', { name: 'Прочитано', exact: true })).toHaveCount(0);
  await page.locator('.tm-nav [data-view="micro"]').click();
  const complete = page.getByRole('checkbox', { name: `Завершить: ⚡ ${title}`, exact: true });
  await complete.click();
  await expect(complete).toHaveCount(0);
  await page.locator('.tm-nav [data-view="inbox"]').click();
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
