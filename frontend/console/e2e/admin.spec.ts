import { expect, test } from '@playwright/test'
import { check, watchWindowErrors } from './controls'

test('user, role, audit and session lifecycle against the real API', async ({ page, browser }) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' && message.text().includes('[Vue warn]'))
      errors.push(message.text())
  })
  await page.goto('users')
  await expect(page).toHaveURL(/login/)
  await page.screenshot({
    path: '../../.artifacts/console-login.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '用户管理', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: '用户管理', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '折叠侧栏' }).click()
  await expect(page.locator('.console-shell')).toHaveClass(/is-collapsed/)
  await page.getByRole('button', { name: '展开侧栏' }).click()

  await page.getByRole('button', { name: '新建用户', exact: true }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('登录名', { exact: true }).fill('alice')
  await dialog.getByLabel('展示名', { exact: true }).fill('Alice')
  await dialog.getByLabel('初始密码', { exact: true }).fill('test-password-123')
  await dialog.getByLabel('确认密码', { exact: true }).fill('test-password-123')
  await dialog.getByRole('button', { name: '确认保存' }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('row').filter({ hasText: 'alice' })).toBeVisible()

  const created = page.getByRole('row').filter({ hasText: 'alice' })
  await created.getByRole('button', { name: '修改', exact: true }).click()
  await page.getByRole('dialog').getByLabel('展示名', { exact: true }).fill('Alice 编辑')
  await page.getByRole('dialog').getByRole('button', { name: '确认保存' }).click()
  await expect(created).toContainText('Alice 编辑')
  await created.getByRole('button', { name: '停用', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认保存' }).click()
  await expect(created.getByRole('button', { name: '启用', exact: true })).toBeVisible()
  await created.getByRole('button', { name: '启用', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认保存' }).click()
  await expect(created.getByRole('button', { name: '停用', exact: true })).toBeVisible()
  await created.getByRole('button', { name: '重置密码', exact: true }).click()
  await page.getByRole('dialog').getByLabel('初始密码', { exact: true }).fill('test-password-123')
  await page.getByRole('dialog').getByLabel('确认密码', { exact: true }).fill('test-password-123')
  await page.getByRole('dialog').getByRole('button', { name: '确认保存' }).click()
  await expect(page.getByRole('dialog')).toBeHidden()

  await page.getByRole('link', { name: '角色与权限', exact: true }).click()
  await page.getByRole('button', { name: '新建角色', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('角色标识', { exact: true }).fill('reader')
  await dialog.getByLabel('角色名称', { exact: true }).fill('用户只读')
  await check(dialog.getByRole('checkbox', { name: /user:read/ }), true)
  await dialog.getByRole('button', { name: '保存角色' }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('row').filter({ hasText: 'reader' })).toBeVisible()

  await page.getByRole('link', { name: '用户管理', exact: true }).click()
  let alice = page.getByRole('row').filter({ hasText: 'alice' })
  await alice.getByRole('button', { name: '分配角色' }).click()
  dialog = page.getByRole('dialog')
  await dialog
    .getByRole('listitem')
    .filter({ hasText: '用户只读' })
    .getByRole('button', { name: '分配角色' })
    .click()
  await expect(
    dialog.getByRole('listitem').filter({ hasText: '用户只读' }).getByRole('button'),
  ).toHaveText('解除角色')
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  await page.screenshot({
    path: '../../.artifacts/console-users.png',
    fullPage: true,
    animations: 'disabled',
  })

  const context = await browser.newContext()
  const reader = await context.newPage()
  await reader.goto('http://127.0.0.1:5175/console/users')
  await reader.getByLabel('登录名', { exact: true }).fill('alice')
  await reader.getByLabel('密码', { exact: true }).fill('test-password-123')
  await reader.getByRole('button', { name: '登录', exact: true }).click()
  await expect(reader.getByRole('heading', { name: '首次使用，请先修改密码' })).toBeVisible()
  await reader.getByLabel('当前密码', { exact: true }).fill('test-password-123')
  await reader.getByLabel('新密码（8–128 个字符）', { exact: true }).fill('changed-password-123')
  await reader.getByLabel('确认新密码', { exact: true }).fill('changed-password-123')
  await reader.getByRole('button', { name: '修改并退出' }).click()
  await reader.getByLabel('登录名', { exact: true }).fill('alice')
  await reader.getByLabel('密码', { exact: true }).fill('changed-password-123')
  await reader.getByRole('button', { name: '登录', exact: true }).click()
  await reader.getByRole('link', { name: '用户管理', exact: true }).click()
  await expect(reader.getByRole('heading', { name: '用户管理', exact: true })).toBeVisible()
  await expect(reader.getByRole('button', { name: '新建用户', exact: true })).toHaveCount(0)
  await reader.goto('http://127.0.0.1:5175/console/audit')
  await expect(reader.getByRole('heading', { name: '没有此页面的访问权限' })).toBeVisible()

  // Referenced roles cannot be removed; the dialog retains the server's reason.
  await page.getByRole('link', { name: '角色与权限', exact: true }).click()
  await page
    .getByRole('row')
    .filter({ hasText: 'reader' })
    .getByRole('button', { name: '删除角色' })
    .click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByRole('dialog')).toContainText('请先解除角色分配')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toBeHidden()

  await page.getByRole('link', { name: '用户管理', exact: true }).click()
  alice = page.getByRole('row').filter({ hasText: 'alice' })
  await alice.getByRole('button', { name: '撤销凭据' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认保存' }).click()
  await expect(page.getByRole('dialog')).toBeHidden()
  await reader.goto('http://127.0.0.1:5175/console/users')
  await expect(reader.getByRole('heading', { name: '登录控制台' })).toBeVisible()
  await context.close()

  await page.getByRole('link', { name: '操作审计', exact: true }).click()
  await page.getByLabel('操作代码', { exact: true }).fill('identity.credentials.revoke')
  await page.getByRole('button', { name: '查询审计' }).click()
  await expect(page.locator('tbody tr')).toHaveCount(1)
  await expect(page.locator('tbody')).toContainText('成功')
  await page.getByRole('button', { name: '查看详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('tokens_revoked')
  await page.getByRole('dialog').getByRole('button', { name: '关闭对话框' }).click()
  await page.screenshot({
    path: '../../.artifacts/console-batch-one.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: '操作审计', exact: true })).toBeVisible()
  await page.screenshot({
    path: '../../.artifacts/console-batch-one-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.getByRole('button', { name: '展开导航' }).click()
  await expect(page.getByRole('navigation', { name: '平台模块' })).toBeInViewport()
  await page.getByRole('link', { name: '用户管理', exact: true }).click()
  await expect(page.locator('.console-shell')).not.toHaveClass(/mobile-open/)
  await expect(page.getByRole('heading', { name: '用户管理', exact: true })).toBeVisible()
  await page.screenshot({
    path: '../../.artifacts/console-users-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: '账户菜单' }).click()
  await page.getByRole('menuitem', { name: '个人令牌', exact: true }).click()
  await page.getByLabel('用途名称', { exact: true }).fill('UI 回归验证')
  await check(page.getByRole('checkbox', { name: 'user:read', exact: true }))
  await page.getByRole('button', { name: '签发令牌', exact: true }).click()
  await expect(page.getByLabel('新令牌明文', { exact: true })).not.toHaveValue('')
  await page.getByRole('tab', { name: '账户信息', exact: true }).click()
  await expect(page.getByLabel('新令牌明文', { exact: true })).toHaveCount(0)
  await page.getByRole('tab', { name: '个人令牌', exact: true }).click()
  const token = page.getByRole('row').filter({ hasText: 'UI 回归验证' })
  await token.getByRole('button', { name: '撤销', exact: true }).click()
  await expect(token).toContainText('已撤销')
  await page.screenshot({
    path: '../../.artifacts/console-account.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('link', { name: '平台状态', exact: true }).click()
  await expect(page.getByText('数据库版本', { exact: true })).toBeVisible()
  await page.screenshot({
    path: '../../.artifacts/console-overview.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '账户菜单' }).click()
  await page.getByRole('menuitem', { name: '退出登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '登录控制台' })).toBeVisible()
  expect(errors).toEqual([])
})
