import { expect, test } from '@playwright/test';


test('products toolbar reuses columns and focus mode', async ({
  page,
}) => {
  await page.goto('/app/products');

  const columns = page.locator('#warehouseColumnSettingsTrigger');
  const panel = page.locator('#warehouseColumnSettingsPanel');
  const focus = page.locator('#warehouseFocusModeToggle');
  await expect(columns).toContainText('Столбцы');
  await expect(focus).toContainText('Развернуть');
  await columns.click();
  await expect(panel).toBeVisible();
  await expect(panel).toContainText('Столбцы таблицы');

  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();

  await columns.click();
  await page.locator('h1').click();
  await expect(panel).toBeHidden();

  await focus.click();
  await expect(page.locator('[data-erp-focus-mode]')).toHaveClass(/erp-focus-mode/);
  await expect(focus).toContainText('Свернуть');
  await focus.click();
  await expect(page.locator('[data-erp-focus-mode]')).not.toHaveClass(/erp-focus-mode/);

  for (const viewport of [
    { width: 1366, height: 768 },
    { width: 1440, height: 900 },
    { width: 1920, height: 1080 },
  ]) {
    await page.setViewportSize(viewport);
    await columns.click();
    const box = await panel.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(viewport.width);
    await page.keyboard.press('Escape');
    await expect(panel).toBeHidden();
  }
});
