import { expect, test, type Page } from '@playwright/test'
import { choose, watchWindowErrors } from './controls'
import { testDirectMedia } from './models-media'

test('OpenRouter and configurable NewAPI channels and a real DBOS Flow through the console UI', async ({
  page,
}) => {
  test.setTimeout(180000)
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'warning' && message.text().includes('[Vue warn]'))
      errors.push(message.text())
  })
  await page.goto('models')
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型接入', exact: true })).toBeVisible()
  const channels = page.getByRole('region', { name: '渠道账户', exact: true })
  for (const provider of ['openrouter', 'newapi_prefix', 'newapi_other']) {
    await page.getByRole('tab', { name: '渠道配置', exact: true }).click()
    await page.getByRole('button', { name: '新建渠道', exact: true }).click()
    let dialog = page.getByRole('dialog')
    await dialog.getByLabel('渠道名称', { exact: true }).fill(`${provider} 模拟`)
    await choose(dialog, '服务商', provider === 'openrouter' ? 'OpenRouter' : 'NewAPI')
    await expect(dialog.getByRole('option', { name: 'Openlux', exact: true })).toHaveCount(0)

    await dialog
      .getByLabel('服务地址', { exact: true })
      .fill(
        provider === 'openrouter'
          ? 'https://openrouter.ai/api/v1'
          : provider === 'newapi_prefix'
            ? 'https://relay.example/gateway/v1'
            : 'https://relay-other.example/v1',
      )
    await dialog.getByLabel('推理 API Key', { exact: true }).fill('fake-inference')
    if (provider === 'openrouter')
      await dialog.getByLabel('账务 API Key', { exact: true }).fill('fake-billing')
    await dialog.getByRole('button', { name: '保存渠道', exact: true }).click()
    await expect(dialog).toBeHidden()
    const row = channels.getByRole('row').filter({ hasText: `${provider} 模拟` })
    if (provider === 'openrouter') {
      await row.getByRole('button', { name: '刷新余额', exact: true }).click()
      await expect(row).toContainText('查询成功')
    } else {
      await row.getByRole('button', { name: '刷新余额', exact: true }).click()
      await expect(row).toContainText('查询成功')
      await expect(row).toContainText('当前 Token 不限额')
      await expect(row).toContainText('账户余额')
      await expect(row).toContainText('192.851716 USD')
      expect((await row.boundingBox())!.height).toBeLessThan(170)
      await row.getByRole('button', { name: '额度详情', exact: true }).click({ timeout: 10000 })
      const balance = page.getByRole('dialog')
      await expect(balance).toContainText('当前 Key 的 Token 额度')
      const close = await balance
        .getByRole('button', { name: '关闭对话框', exact: true })
        .boundingBox()
      expect(close!.y).toBeGreaterThanOrEqual(0)
      await page.screenshot({
        path: '../../.artifacts/console-channel-balance.png',
        fullPage: true,
        animations: 'disabled',
      })
      await balance.getByRole('button', { name: '刷新余额', exact: true }).click()
      await expect(balance).toBeVisible()
      await expect(balance).toContainText('查询成功')
      await balance
        .getByRole('button', { name: '关闭对话框', exact: true })
        .click({ timeout: 10000 })
    }
    await page.getByRole('tab', { name: '模型配置', exact: true }).click()
    await page.getByRole('button', { name: '新建模型', exact: true }).click()
    dialog = page.getByRole('dialog')
    await dialog.getByLabel('模型 key', { exact: true }).fill(`${provider}_chat`)
    await choose(dialog, '模型渠道', `${provider} 模拟`)
    if (provider === 'openrouter') {
      await expect(dialog).toContainText('已获取 1 个模型')
      await choose(dialog, '上游模型', 'mock-chat')
    } else {
      await dialog.getByRole('combobox', { name: '上游模型', exact: true }).fill('mock-chat')
      await dialog.getByRole('option', { name: 'mock-chat', exact: true }).click()
    }
    await dialog.getByRole('button', { name: '保存模型', exact: true }).click()
    await expect(dialog).toBeHidden()
    await page.getByRole('tab', { name: '调用测试', exact: true }).click()
    const probe = page.getByRole('region', { name: '真实调用', exact: true })
    await probe.getByRole('button', { name: '刷新可用模型' }).click()
    await choose(probe, '模型', `${provider}_chat`)
    await probe.getByLabel('测试消息', { exact: true }).fill('验证渠道，不调用真实模型。')
    await expect(probe.getByLabel('流式响应', { exact: true })).toHaveCount(0)
    await probe.getByRole('button', { name: '发送真实请求', exact: true }).click()
    await expect(probe.locator('pre')).toContainText('模拟渠道响应')
    await expect(page.getByRole('region', { name: '调用记录与费用', exact: true })).toHaveCount(0)
  }
  await page.getByRole('tab', { name: '渠道配置', exact: true }).click()
  for (const provider of ['openrouter', 'newapi_prefix', 'newapi_other']) {
    const row = channels.getByRole('row').filter({ hasText: `${provider} 模拟` })
    await expect(row.getByText('启用', { exact: true })).toBeVisible()
    await expect(row.getByRole('button', { name: '编辑渠道', exact: true })).toBeVisible()
    await expect(row.getByRole('button', { name: '额度详情', exact: true })).toBeVisible()
  }
  await page.screenshot({
    path: '../../.artifacts/console-model-channels.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: '../../.artifacts/console-model-channels-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('tab', { name: '模型配置', exact: true }).click()
  await page.screenshot({
    path: '../../.artifacts/console-model-entries.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('link', { name: '用量与费用', exact: true }).click()
  await expect(page.getByRole('heading', { name: '用量与费用', exact: true })).toBeVisible()
  await expect(page.getByRole('combobox', { name: '统计时区', exact: true })).toHaveCount(0)
  const overview = page.getByRole('region', { name: '用量费用总览', exact: true })
  await expect(overview).toContainText('费用未知：2 次')
  await expect(overview).toContainText('0.0012')
  for (const label of ['趋势指标', '分析维度', '分组排序']) {
    const control = page.getByRole('combobox', { name: label, exact: true })
    expect(
      (await control
        .locator('xpath=ancestor::div[contains(@class, "app-select")][1]')
        .boundingBox())!.width,
    ).toBeGreaterThan(180)
  }
  await page.screenshot({
    path: '../../.artifacts/console-model-usage-overview.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page
    .getByRole('group', { name: '可点击的时间趋势', exact: true })
    .getByRole('button')
    .first()
    .click()
  await expect(
    page.getByRole('region', { name: '调用明细', exact: true }).locator('tbody tr'),
  ).toHaveCount(3)
  await expect(page).toHaveURL(/range=custom/)
  await page.goBack()
  await expect(overview).toBeVisible()
  await page
    .getByRole('region', { name: '维度分析', exact: true })
    .getByRole('button', { name: 'newapi_prefix 模拟', exact: true })
    .click()
  const history = page.getByRole('region', { name: '调用明细', exact: true })
  await expect(history.locator('tbody tr')).toHaveCount(1)
  await expect(page).toHaveURL(/channel_id=/)
  const channelPicker = page
    .getByRole('combobox', { name: '渠道筛选', exact: true })
    .locator('xpath=ancestor::div[contains(@class, "app-select")][1]')
  const selectedChannel = channelPicker.locator('.el-select__placeholder')
  await expect(selectedChannel).toHaveText('newapi_prefix 模拟')
  await page.getByRole('tab', { name: '维度分析', exact: true }).click()
  await expect(selectedChannel).toHaveText('newapi_prefix 模拟')
  await page.getByRole('tab', { name: '调用明细', exact: true }).click()
  const options = page.getByRole('listbox', { name: '渠道筛选', exact: true })
  const input = channelPicker.getByRole('combobox')
  await input.fill('newapi_prefix')
  await expect(
    options.getByRole('option', { name: 'newapi_prefix 模拟', exact: true }),
  ).toBeVisible()
  await expect(options.getByRole('option', { name: 'newapi_prefix', exact: true })).toHaveCount(0)
  await input.press('Escape')
  await expect(selectedChannel).toHaveText('newapi_prefix 模拟')
  // Tab changes are intentionally part of the history; restore the unfiltered overview.
  await page.getByRole('button', { name: '重置筛选', exact: true }).click()
  await page.getByRole('tab', { name: '总览', exact: true }).click()
  await expect(overview).toBeVisible()
  await choose(page, '分析维度', '业务模型')
  await choose(page, '分组排序', '已知费用从高到低 · credits')
  await expect(page).toHaveURL(/sort_unit=credits/)
  await page.getByRole('tab', { name: '调用明细', exact: true }).click()
  await expect(history.locator('tbody tr')).toHaveCount(3)
  await history.locator('tbody tr').last().getByRole('button', { name: '查看调用' }).click()
  let dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('mock-')
  await expect(dialog).toContainText('total_tokens')
  await dialog.getByRole('button', { name: '补查费用' }).click()
  await expect(dialog).toContainText('来源 query')
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  await page.screenshot({
    path: '../../.artifacts/console-model-usage.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: '../../.artifacts/console-model-usage-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.setViewportSize({ width: 1440, height: 1000 })

  // Exercise extreme display values without changing the real test ledger.
  await page.route('**/api/v1/model-invocations/statistics?*', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    const group = body.groups.find((row: { costs: unknown[] }) => row.costs.length)
    group.label = '较长的渠道显示名称与上游模型配置说明'.repeat(5)
    group.costs[0].amount = '0.002306'
    group.costs[0].avg_cost = '0.000768666666666667'
    await route.fulfill({ response, json: body })
  })
  await page.getByRole('tab', { name: '总览', exact: true }).click()
  await page.getByRole('button', { name: '刷新统计', exact: true }).click()
  const groups = page.getByRole('region', { name: '维度分析', exact: true })
  const average = groups.locator('.average-cost .money-amount').first()
  await expect(average).toHaveText('0.00076866… credits')
  await expect(average).toHaveAttribute('title', '0.000768666666666667 credits')
  const longLabel = groups.locator('.group-link .truncate').first()
  await expect
    .poll(() => longLabel.evaluate((node) => node.scrollWidth > node.clientWidth))
    .toBe(true)
  expect(await longLabel.evaluate((node) => getComputedStyle(node).textOverflow)).toBe('ellipsis')
  await page.screenshot({
    path: '../../.artifacts/console-usage-truncation.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true)
  await page.screenshot({
    path: '../../.artifacts/console-usage-truncation-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.unroute('**/api/v1/model-invocations/statistics?*')
  await page.setViewportSize({ width: 1440, height: 1000 })

  await page.getByRole('link', { name: 'Flow 流程', exact: true }).click()
  const catalog = page.getByRole('region', { name: '流程目录', exact: true })
  await expect(catalog.getByRole('row').filter({ hasText: 'document.review' })).toBeVisible()
  await page.getByLabel('搜索流程', { exact: true }).fill('不存在的流程')
  await expect(catalog).toContainText('没有匹配的流程')
  await page.getByLabel('搜索流程', { exact: true }).fill('document.review')
  await page.screenshot({
    path: '../../.artifacts/console-flow-catalog.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '发起流程', exact: true }).click()
  dialog = page.getByRole('dialog')
  const files = await (await page.request.get('/api/v1/files?limit=50')).json()
  const source = files.find((file: { name: string }) => file.name === 'referenced.txt')
  await dialog.getByLabel('流程输入 JSON').fill(JSON.stringify({ source_file_id: source.id }))
  await choose(dialog, '审查模型', 'openrouter_chat')
  await dialog.getByRole('button', { name: '提交流程', exact: true }).click()
  dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '运行详情', exact: true })).toBeVisible()
  await expect(dialog.locator('dd').filter({ hasText: /^已完成$/ })).toBeVisible({ timeout: 35000 })
  await expect(dialog).toContainText('模拟审查已完成')
  await expect(dialog).toContainText('0.0024 credits')
  await expect(dialog.locator('.el-timeline-item').first()).toBeVisible()
  const budgetPanel = dialog.getByRole('region', { name: '运行预算', exact: true })
  await expect(budgetPanel).toContainText('模型请求 · 已预留')
  await expect(budgetPanel).toContainText('未结清文本请求：0')
  const diagnosticModels = dialog.getByRole('region', { name: '关联模型调用', exact: true })
  await expect(diagnosticModels).toContainText('Agent')
  await expect(diagnosticModels).toContainText('review_agent')
  await expect(diagnosticModels).not.toContainText('历史 Step 位置未记录')
  await expect(dialog.getByRole('region', { name: '关联文件', exact: true })).toContainText(
    'artifact.report',
  )

  await page.screenshot({
    path: '../../.artifacts/console-flow-detail.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: '../../.artifacts/console-flow-detail-mobile.png',
    fullPage: true,
    animations: 'disabled',
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  const runRows = page.getByRole('region', { name: '运行记录' }).locator('tbody tr')
  await expect(runRows).toHaveCount(1)
  const original = await runRows.first().locator('small').textContent()
  await page.getByRole('button', { name: '发起流程', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '提交流程', exact: true }).click()
  await expect(
    page.getByRole('dialog').getByRole('heading', { name: '运行详情', exact: true }),
  ).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '关闭对话框' }).click()
  await expect(runRows).toHaveCount(1)
  expect(await runRows.first().locator('small').textContent()).toBe(original)
  await choose(page, '运行状态', '失败')
  await page.getByRole('button', { name: '查询流程', exact: true }).click()
  await expect(page.getByRole('region', { name: '运行记录' })).toContainText('暂无符合条件的运行')
  await page.getByRole('button', { name: '重置筛选', exact: true }).click()
  await expect(runRows).toHaveCount(1)
  await expect(runRows.first()).toContainText(original!)
  await page.getByRole('link', { name: '用量与费用', exact: true }).click()
  await choose(page, '分析维度', 'Flow 运行')
  await page
    .getByRole('region', { name: '维度分析', exact: true })
    .getByRole('button', { name: original!.trim(), exact: true })
    .click()
  await expect(
    page.getByRole('region', { name: '调用明细', exact: true }).locator('tbody tr'),
  ).toHaveCount(2)
  await page.getByRole('button', { name: '查看流程运行', exact: true }).first().click()
  await expect(page.getByRole('dialog')).toContainText('模拟审查已完成')
  await page.getByRole('dialog').getByRole('button', { name: '关闭对话框' }).click()
  await renameAndDeleteUnusedConfiguration(page)
  await testDirectMedia(page)
  expect(errors).toEqual([])
})

async function renameAndDeleteUnusedConfiguration(page: Page) {
  await page.goto('models')
  await page.getByRole('button', { name: '新建渠道', exact: true }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('渠道名称', { exact: true }).fill('待清理渠道')
  await choose(dialog, '服务商', 'Yunwu')
  let releaseVerification!: () => void
  let verifying = false
  const verificationGate = new Promise<void>((resolve) => {
    releaseVerification = resolve
  })
  await page.route(
    '**/api/v1/auth/me',
    async (route) => {
      verifying = true
      await verificationGate
      await route.continue()
    },
    { times: 1 },
  )
  const verified = page.waitForResponse((response) => response.url().endsWith('/api/v1/auth/me'))
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  try {
    await expect.poll(() => verifying).toBe(true)
    await expect(dialog).toBeVisible()
    await expect(dialog.getByLabel('渠道名称', { exact: true })).toHaveValue('待清理渠道')
  } finally {
    releaseVerification()
  }
  await verified
  await expect(dialog.getByLabel('渠道名称', { exact: true })).toHaveValue('待清理渠道')
  await dialog.getByLabel('推理 API Key', { exact: true }).fill('fake-inference')
  await dialog.getByRole('button', { name: '保存渠道', exact: true }).click()
  await expect(dialog).toBeHidden()
  const channelRow = page
    .getByRole('region', { name: '渠道账户', exact: true })
    .getByRole('row')
    .filter({ hasText: '待清理渠道' })
  await expect(channelRow).toContainText('暂不支持查询')
  await expect(channelRow.getByRole('button', { name: '刷新余额', exact: true })).toBeDisabled()
  expect((await channelRow.boundingBox())!.height).toBeLessThan(170)
  await channelRow.getByRole('button', { name: '修改渠道名称', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('显示名称', { exact: true }).fill('可删除渠道')
  await dialog.getByRole('button', { name: '保存名称', exact: true }).click()
  await expect(dialog).toBeHidden()
  await page.getByRole('tab', { name: '模型配置', exact: true }).click()
  await page.getByRole('button', { name: '新建模型', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('模型 key', { exact: true }).fill('unused_cleanup')
  await choose(dialog, '模型渠道', '可删除渠道')
  await choose(dialog, '上游模型', 'mock-chat')
  await dialog.getByRole('button', { name: '保存模型', exact: true }).click()
  await expect(dialog).toBeHidden()
  const modelRow = page.getByRole('row').filter({ hasText: 'unused_cleanup' })
  await modelRow.getByRole('button', { name: '修改模型名称', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('显示名称', { exact: true }).fill('临时模型')
  await dialog.getByRole('button', { name: '保存名称', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(modelRow).toContainText('临时模型')
  await expect(modelRow).toContainText('unused_cleanup')
  await modelRow.getByRole('button', { name: '编辑模型', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('模型显示名称', { exact: true }).fill('')
  await dialog.getByRole('button', { name: '保存模型', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(modelRow).not.toContainText('临时模型')
  await expect(modelRow).toContainText('unused_cleanup')
  await modelRow.getByRole('button', { name: '删除模型', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(modelRow).toHaveCount(0)
  await page.getByRole('tab', { name: '渠道配置', exact: true }).click()
  const renamedChannel = page
    .getByRole('region', { name: '渠道账户', exact: true })
    .getByRole('row')
    .filter({ hasText: '可删除渠道' })
  await renamedChannel.getByRole('button', { name: '删除渠道', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(renamedChannel).toHaveCount(0)
}
