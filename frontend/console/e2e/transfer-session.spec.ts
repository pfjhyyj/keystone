import { expect, test } from '@playwright/test'
import { watchWindowErrors } from './controls'

test('another tab logout clears a mounted configuration draft', async ({ page, context }) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  await page.goto('models')
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建渠道', exact: true }).click()
  await page.getByRole('dialog').getByLabel('渠道名称', { exact: true }).fill('未保存的配置')
  const other = await context.newPage()
  await other.goto('http://127.0.0.1:5175/console/models')
  await expect(other.getByRole('heading', { name: '模型接入', exact: true })).toBeVisible()
  await other.getByRole('button', { name: '账户菜单', exact: true }).click()
  await other.getByRole('menuitem', { name: '退出登录', exact: true }).click()
  await expect(other).toHaveURL(/login/)
  await expect(page).toHaveURL(/login/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await other.close()
  expect(errors).toEqual([])
})
