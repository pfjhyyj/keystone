import { expect, type Page } from '@playwright/test'
import { choose } from './controls'

// Reuse the authenticated model test session; repeated admin logins share a real rate limit.
export async function testDirectMedia(page: Page) {
  await page.goto('models')
  const identity = await (await page.request.get('/api/v1/auth/me')).json()
  const headers = {
    'X-Keystone-Request': '1',
    'X-CSRF-Token': identity.csrf_token,
    Origin: 'http://127.0.0.1:5175',
  }
  const channel = await page.request.post('/api/v1/model-channels', {
    headers,
    data: {
      name: '图片测试渠道',
      provider: 'openrouter',
      api_base: 'https://openrouter.ai/api/v1',
      api_key: 'fake-only-model-media',
    },
  })
  expect(channel.ok()).toBe(true)
  const config = await page.request.post('/api/v1/model-entries', {
    headers,
    data: {
      key: 'media_probe',
      name: '通用图片测试模型',
      channel_id: (await channel.json()).id,
      provider_model: 'mock-chat',
      operations: ['image.generate', 'image.edit', 'image.understand'],
    },
  })
  expect(config.ok()).toBe(true)
  await page.getByRole('tab', { name: '调用测试', exact: true }).click()
  const probe = page.getByRole('region', { name: '真实调用', exact: true })
  await probe.getByRole('tab', { name: '图片生成', exact: true }).click()
  await choose(probe, '模型', '通用图片测试模型（media_probe）')
  await probe
    .getByLabel('测试消息', { exact: true })
    .fill('A blue square. Local protocol stub only.')
  await expect(probe.getByRole('combobox', { name: '画质', exact: true })).toHaveCount(0)
  const generate = page.waitForResponse((response) =>
    response.url().endsWith('/models/images/generate'),
  )
  await probe.getByRole('button', { name: '发送真实请求', exact: true }).click()
  const generated = await (await generate).json()
  expect(generated.files).toHaveLength(1)
  const result = probe.getByLabel('生成图片').locator('img').first()
  await expect(result).toBeVisible()
  await expect
    .poll(() => result.evaluate((node) => (node as HTMLImageElement).naturalWidth))
    .toBeGreaterThan(0)
  await result.click()
  await expect(page.locator('.el-image-viewer__wrapper')).toBeVisible()
  await page.keyboard.press('Escape')
  const download = page.waitForEvent('download')
  await probe.getByRole('button', { name: '下载图片', exact: true }).click()
  expect((await download).suggestedFilename()).toBe(`model.${generated.invocation_id}.0.png`)
  await page.screenshot({
    path: '../../.artifacts/console-model-probe-generation.png',
    fullPage: true,
  })

  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNkYPjPwMDAxAAGAAsfAQMU4wsAAAAAAElFTkSuQmCC',
    'base64',
  )
  for (const mode of ['图片编辑', '图片理解']) {
    await probe.getByRole('tab', { name: mode, exact: true }).click()
    await choose(probe, '模型', '通用图片测试模型（media_probe）')
    await probe
      .getByLabel('测试消息', { exact: true })
      .fill(mode === '图片编辑' ? 'Change to blue.' : 'Describe the image.')
    await probe
      .locator('input[type=file]')
      .setInputFiles({ name: 'reference.png', mimeType: 'image/png', buffer: png })
    const path = mode === '图片编辑' ? '/models/images/edit' : '/models/vision'
    const pending = page.waitForResponse((response) => response.url().endsWith(path))
    await probe.getByRole('button', { name: '发送真实请求', exact: true }).click()
    const response = await pending
    expect(response.ok(), await response.text()).toBe(true)
    if (mode === '图片理解') await expect(probe.locator('pre')).toContainText('模拟渠道响应')
    else await expect(probe.getByLabel('生成图片')).toBeVisible()
    await expect(probe.getByRole('button', { name: '发送真实请求', exact: true })).toBeEnabled()
    await page.screenshot({
      path: `../../.artifacts/console-model-probe-${mode === '图片编辑' ? 'edit' : 'vision'}.png`,
      fullPage: true,
    })
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true)
  await page.screenshot({ path: '../../.artifacts/console-model-probe-mobile.png', fullPage: true })
  const record = await (
    await page.request.get(`/api/v1/model-invocations/${generated.invocation_id}`)
  ).json()
  expect(record.flow_context).toBeNull()
}
