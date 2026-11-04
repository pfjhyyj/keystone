import { expect, test } from '@playwright/test'
import { check, watchWindowErrors } from './controls'

test('dictionary configuration and option states against the real API', async ({ page }) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  await page.goto('dictionaries')
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('link', { name: '业务字典', exact: true }).click()
  await page.getByRole('button', { name: '新建字典', exact: true }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('字典标识', { exact: true }).fill('browser_source')
  await dialog.getByLabel('字典名称', { exact: true }).fill('浏览器来源')
  await dialog.getByLabel('字典说明', { exact: true }).fill('保留这条说明')
  await dialog.getByRole('button', { name: '保存字典', exact: true }).click()
  await expect(dialog).toBeHidden()
  for (const [value, label, sort] of [
    ['later', '较后', '20'],
    ['first', '优先', '-1'],
  ]) {
    await page.getByRole('button', { name: '添加选项', exact: true }).click()
    dialog = page.getByRole('dialog')
    await dialog.getByLabel('稳定值', { exact: true }).fill(value!)
    await dialog.getByLabel('显示名称', { exact: true }).fill(label!)
    await dialog.getByLabel('排序', { exact: true }).fill(sort!)
    await dialog.getByRole('button', { name: '保存选项', exact: true }).click()
    await expect(dialog).toBeHidden()
  }
  const items = page.getByRole('region', { name: '字典选项', exact: true })
  await expect(items.locator('tbody tr').first()).toContainText('优先')
  await expect(page.getByText('标识与时间', { exact: true })).toHaveCount(0)
  const dictionaryRow = page.getByRole('row').filter({ hasText: 'browser_source' })
  await dictionaryRow.getByRole('button', { name: '编辑字典', exact: true }).click()
  dialog = page.getByRole('dialog')
  await expect(dialog.getByLabel('字典标识', { exact: true })).toBeDisabled()
  await expect(dialog.getByLabel('字典说明', { exact: true })).toHaveValue('保留这条说明')
  await check(dialog.getByLabel('启用字典', { exact: true }), false)
  await dialog.getByRole('button', { name: '保存字典', exact: true }).click()
  await expect(items.locator('tbody tr').first()).toContainText('已启用，字典停用中')
  const priority = items.getByRole('row').filter({ hasText: '优先' })
  await priority.getByRole('button', { name: '编辑选项', exact: true }).click()
  dialog = page.getByRole('dialog')
  await expect(dialog.getByLabel('稳定值', { exact: true })).toBeDisabled()
  await expect(dialog.getByLabel('排序', { exact: true })).toHaveValue('-1')
  await check(dialog.getByLabel('启用选项', { exact: true }), false)
  await dialog.getByRole('button', { name: '保存选项', exact: true }).click()
  await expect(priority).toContainText('已停用')
  await dictionaryRow.getByRole('button', { name: '编辑字典', exact: true }).click()
  await check(page.getByRole('dialog').getByLabel('启用字典', { exact: true }), true)
  await page.getByRole('dialog').getByRole('button', { name: '保存字典', exact: true }).click()
  await expect(priority).toContainText('已停用')
  await expect(items.getByRole('row').filter({ hasText: '较后' })).toContainText('可选')
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )

  expect(errors).toEqual([])
})
