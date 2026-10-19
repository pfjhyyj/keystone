import { defineConfig } from '@playwright/test'
if (process.env.KEYSTONE_E2E !== '1')
  throw new Error('Run e2e/run.py against a dedicated keystone_test database')
export default defineConfig({
  testDir: 'e2e',
  workers: 1,
  retries: 0,
  timeout: 60000,
  outputDir: '../../.artifacts/console-e2e',
  use: {
    baseURL: 'http://127.0.0.1:5175/console/',
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'off',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5175',
    url: 'http://127.0.0.1:5175/console/',
    reuseExistingServer: false,
    timeout: 30000,
  },
})
