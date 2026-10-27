import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
import { openUpload, watchWindowErrors } from './controls'

async function login(page: Page) {
  await page.goto('files')
  await page.getByLabel('登录名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-password-123')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '文件库', exact: true })).toBeVisible()
  await openUpload(page)
}
test('bounded hash worker, native download and cancel retry use the real file protocol', async ({
  page,
}) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  await login(page)
  const bytes = Buffer.alloc(4 * 1024 * 1024 + 17, 97)
  await page.getByLabel('选择文件', { exact: true }).setInputFiles({
    name: 'bounded-transfer.txt',
    mimeType: 'text/plain',
    buffer: bytes,
  })
  const uploaded = page.waitForResponse(
    (response) =>
      response.url().includes('/api/v1/files?') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  const receipt = await (await uploaded).json()
  expect(receipt.checksum_sha256).toBe(createHash('sha256').update(bytes).digest('hex'))
  const row = page.getByRole('row').filter({ hasText: 'bounded-transfer.txt' })
  await expect(row).toBeVisible()
  const downloading = page.waitForEvent('download')
  await row.getByRole('button', { name: '下载', exact: true }).click()
  const download = await downloading
  expect(download.suggestedFilename()).toBe('bounded-transfer.txt')
  expect(await readFile((await download.path())!)).toEqual(bytes)

  // Hold only the request transport; release it after cancellation and retry the same intent.
  const keys: string[] = []
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/files?**', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    keys.push(new URL(route.request().url()).searchParams.get('request_key')!)
    if (keys.length === 1) {
      await gate
      await route.abort().catch(() => {})
    } else await route.continue()
  })
  await page.getByLabel('选择文件', { exact: true }).setInputFiles({
    name: 'cancel-retry.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('retry bytes'),
  })
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  await expect.poll(() => keys.length).toBe(1)
  await page.getByRole('button', { name: '取消上传', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('同一文件重试')
  release()
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  await expect(page.getByRole('row').filter({ hasText: 'cancel-retry.txt' })).toBeVisible()
  expect(keys).toHaveLength(2)
  expect(keys[1]).toBe(keys[0])
  expect(errors).toEqual([])
})
test('another tab logout clears the mounted console and cancels an in-flight upload', async ({
  page,
  context,
}) => {
  const errors: string[] = []
  await watchWindowErrors(page, errors)
  await login(page)
  const other = await context.newPage()
  await other.goto('http://127.0.0.1:5175/console/files')
  await expect(other.getByRole('heading', { name: '文件库', exact: true })).toBeVisible()
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let pending = false
  await page.route('**/api/v1/files?**', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    pending = true
    await gate
    await route.abort().catch(() => {})
  })
  await page.getByLabel('选择文件', { exact: true }).setInputFiles({
    name: 'session-cancel.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('private bytes'),
  })
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  await expect.poll(() => pending).toBe(true)
  await other.getByRole('button', { name: '账户菜单', exact: true }).click()
  await other.getByRole('menuitem', { name: '退出登录', exact: true }).click()
  await expect(other).toHaveURL(/login/)
  await expect(page).toHaveURL(/login/)
  expect(await page.getByRole('button', { name: '取消上传', exact: true }).count()).toBe(0)
  release()
  await other.close()
  expect(errors).toEqual([])
})
