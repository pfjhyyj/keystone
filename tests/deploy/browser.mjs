// Exercise built assets through Nginx, without Vite or a mock API.
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(new URL('../../frontend/console/package.json', import.meta.url))
const { chromium, expect } = require('@playwright/test')
const [origin, directory] = process.argv.slice(2)
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw new Error('Only local deployment fixtures are accepted')
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/console/models`)
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('deploy-test-password-only')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型接入', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: '模型接入', exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Flow 流程', exact: true }).click()
  await expect(page.getByRole('button', { name: '发起流程', exact: true })).toBeVisible()
  await page.screenshot({ path: resolve(directory, 'deployed-console.png'), fullPage: true })
  expect(errors).toEqual([])
} finally {
  await browser.close()
}
