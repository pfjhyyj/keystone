import { expect, test } from '@playwright/test'
import { choose, check, watchWindowErrors } from './controls'

test('file grants, reference protection and dictionaries against the real API', async ({
  page,
  browser,
}) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  let optionsAttempts = 0
  await page.route('**/api/v1/files/options', async (route) => {
    optionsAttempts++
    if (optionsAttempts === 1)
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '上传配置暂不可用' }),
      })
    else await route.continue()
  })
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' && message.text().includes('[Vue warn]'))
      errors.push(message.text())
  })
  await page.goto('files')
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '文件库', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('上传配置加载失败')
  await page.getByRole('button', { name: '重试上传配置', exact: true }).click()
  await expect(
    page
      .locator('.app-select')
      .filter({ has: page.getByRole('combobox', { name: '存储源', exact: true }) }),
  ).toContainText('local')
  expect(optionsAttempts).toBe(2)
  const identity = await (await page.request.get('/api/v1/auth/me')).json()
  const headers = {
    Origin: 'http://127.0.0.1:5175',
    'X-Keystone-Request': '1',
    'X-CSRF-Token': identity.csrf_token,
  }
  // Identity fixtures use existing APIs; all file and dictionary mutations below use the UI.
  const userResponse = await page.request.post('/api/v1/users', {
    headers,
    data: { username: 'file_viewer', display_name: '文件测试用户', password: 'test-password-123' },
  })
  expect(userResponse.ok()).toBe(true)
  const viewerUser = await userResponse.json()
  const roleResponse = await page.request.post('/api/v1/roles', {
    headers,
    data: {
      key: 'file_viewer',
      name: '文件测试角色',
      description: '',
      permissions: ['file:inspect', 'file:content_read', 'file:manage', 'dictionary:read'],
    },
  })
  expect(roleResponse.ok()).toBe(true)
  const viewerRole = await roleResponse.json()
  expect(
    (
      await page.request.put(`/api/v1/users/${viewerUser.id}/roles/${viewerRole.id}`, {
        headers,
        data: { assigned: true },
      })
    ).ok(),
  ).toBe(true)

  await page.getByLabel('选择文件', { exact: true }).setInputFiles({
    name: 'sharing.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('shared content'),
  })
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  const fileRow = page.getByRole('row').filter({ hasText: 'sharing.txt' })
  await expect(fileRow).toBeVisible()
  await fileRow.getByRole('button', { name: '文件授权', exact: true }).click()
  let dialog = page.getByRole('dialog')
  await choose(dialog, '授权主体', '角色')
  await choose(dialog, '授权对象', '文件测试角色（file_viewer）')
  await dialog.getByRole('button', { name: '添加授权', exact: true }).click()
  await expect(dialog.locator('.grant-list')).toContainText(viewerRole.id)
  await dialog.getByRole('button', { name: '关闭对话框' }).click()

  const context = await browser.newContext()
  const viewer = await context.newPage()
  viewer.on('pageerror', (error) => errors.push(error.message))
  const loginHeaders = { Origin: 'http://127.0.0.1:5175', 'X-Keystone-Request': '1' }
  let login = await (
    await context.request.post('http://127.0.0.1:5175/api/v1/auth/login', {
      headers: loginHeaders,
      data: { username: 'file_viewer', password: 'test-password-123' },
    })
  ).json()
  expect(
    (
      await context.request.post('http://127.0.0.1:5175/api/v1/auth/password', {
        headers: { ...loginHeaders, 'X-CSRF-Token': login.csrf_token },
        data: { current_password: 'test-password-123', password: 'changed-password-123' },
      })
    ).ok(),
  ).toBe(true)
  login = await context.request.post('http://127.0.0.1:5175/api/v1/auth/login', {
    headers: loginHeaders,
    data: { username: 'file_viewer', password: 'changed-password-123' },
  })
  expect(login.ok()).toBe(true)
  await viewer.goto('http://127.0.0.1:5175/console/files')
  const visibleFile = viewer.getByRole('row').filter({ hasText: 'sharing.txt' })
  await visibleFile.getByRole('button', { name: '下载', exact: true }).click()
  await expect(viewer.getByRole('alert')).toContainText('文件不存在或不可访问')
  await visibleFile.getByRole('button', { name: '文件授权', exact: true }).click()
  await expect(viewer.getByRole('dialog').getByRole('alert')).toContainText('文件不存在或不可访问')
  await viewer.getByRole('button', { name: '关闭对话框' }).click()

  await fileRow.getByRole('button', { name: '文件授权', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('搜索授权用户', { exact: true }).fill('file_viewer')
  await dialog.getByRole('button', { name: '查找用户' }).click()
  await choose(dialog, '授权对象', '文件测试用户（file_viewer）')
  await choose(dialog, '授权动作', '读取内容')
  await dialog.getByRole('button', { name: '添加授权', exact: true }).click()
  await expect(dialog.locator('.grant-list li').filter({ hasText: viewerUser.id })).toContainText(
    '读取内容',
  )
  const downloaded = viewer.waitForEvent('download')
  await visibleFile.getByRole('button', { name: '下载', exact: true }).click()
  expect((await downloaded).suggestedFilename()).toBe('sharing.txt')

  await choose(dialog, '授权对象', '文件测试用户（file_viewer）')
  await choose(dialog, '授权动作', '管理与授权')
  await dialog.getByRole('button', { name: '添加授权', exact: true }).click()
  await expect(
    dialog
      .locator('.grant-list li')
      .filter({ hasText: viewerUser.id })
      .filter({ hasText: '管理与授权' }),
  ).toBeVisible()
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  await visibleFile.getByRole('button', { name: '文件授权', exact: true }).click()
  const viewerDialog = viewer.getByRole('dialog')
  await expect(viewerDialog.getByLabel('授权对象 ID', { exact: true })).toBeVisible()
  await viewerDialog
    .locator('.grant-list li')
    .filter({ hasText: viewerUser.id })
    .filter({ hasText: '管理与授权' })
    .getByRole('button', { name: '撤销授权' })
    .click()
  await viewerDialog.getByRole('button', { name: '确认撤销', exact: true }).click()
  await expect(viewerDialog.getByRole('alert')).toContainText('文件不存在或不可访问')
  await expect(viewerDialog.locator('.grant-list li')).toHaveCount(0)
  await viewerDialog.getByRole('button', { name: '关闭对话框' }).click()

  await fileRow.getByRole('button', { name: '文件授权', exact: true }).click()
  dialog = page.getByRole('dialog')
  await choose(dialog, '授权主体', '所有登录用户')
  await expect(dialog.getByRole('option', { name: '管理与授权', exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: '添加授权', exact: true }).click()
  await expect(dialog.locator('.grant-list li').filter({ hasText: '所有登录用户' })).toHaveCount(1)
  await dialog.getByRole('button', { name: '添加授权', exact: true }).click()
  await expect(dialog.locator('.grant-list li').filter({ hasText: '所有登录用户' })).toHaveCount(1)
  await page.screenshot({
    path: '../../.artifacts/console-file-grants.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: '../../.artifacts/console-file-grants-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.setViewportSize({ width: 1440, height: 1000 })
  for (const subject of [viewerRole.id, '所有登录用户']) {
    await dialog
      .locator('.grant-list li')
      .filter({ hasText: subject })
      .getByRole('button', { name: '撤销授权' })
      .click()
    await dialog.getByRole('button', { name: '确认撤销', exact: true }).click()
    await expect(dialog.locator('.grant-list li').filter({ hasText: subject })).toHaveCount(0)
  }
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  await viewer.getByRole('button', { name: '刷新文件', exact: true }).click()
  await expect(visibleFile).toHaveCount(0)

  const referenced = page.getByRole('row').filter({ hasText: 'referenced.txt' })
  await referenced.getByRole('button', { name: '查看引用', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('e2e:record:1')
  await expect(page.getByRole('dialog')).toContainText('input')
  await page.getByRole('button', { name: '关闭对话框' }).click()
  await referenced.getByRole('button', { name: '删除文件', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(
    '文件仍被业务引用，不能删除',
  )
  await page.getByRole('button', { name: '关闭对话框' }).click()

  await page.getByRole('link', { name: '业务字典', exact: true }).click()
  await page.getByRole('button', { name: '新建字典', exact: true }).click()
  dialog = page.getByRole('dialog')
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
  await page.screenshot({
    path: '../../.artifacts/console-dictionaries.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: '../../.artifacts/console-dictionaries-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )

  await viewer.getByRole('link', { name: '业务字典', exact: true }).click()
  await viewer.getByRole('button', { name: '查看选项', exact: true }).click()
  await expect(viewer.getByRole('button', { name: '新建字典', exact: true })).toHaveCount(0)
  await expect(viewer.getByRole('button', { name: '编辑选项', exact: true })).toHaveCount(0)
  await expect(viewer.getByRole('region', { name: '字典选项', exact: true })).toContainText(
    '已停用',
  )
  await context.close()
  expect(errors).toEqual([])
})
