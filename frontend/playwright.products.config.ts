import { defineConfig } from '@playwright/test';

const baseURL = 'http://127.0.0.1:4174';
const pythonCommand = process.env.ERP_E2E_PYTHON
  ? `"${process.env.ERP_E2E_PYTHON}"`
  : process.platform === 'win32'
    ? 'py -3'
    : 'python3';

export default defineConfig({
  testDir: './e2e-products',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { viewport: { width: 1366, height: 768 } } }],
  webServer: {
    command: `${pythonCommand} ../tests/stage2_preview_server.py`,
    env: { ...process.env, PREVIEW_PORT: '4174' },
    url: `${baseURL}/app/products`,
    reuseExistingServer: Boolean(process.env.ERP_E2E_REUSE_SERVER),
  },
});
