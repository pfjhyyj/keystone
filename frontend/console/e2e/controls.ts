import { expect, type Locator, type Page } from '@playwright/test'

export async function watchWindowErrors(page: Page, errors: string[]) {
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().startsWith('Window error:'))
      errors.push(message.text())
  })
  await page.addInitScript(() => {
    window.addEventListener('error', (event) => console.error('Window error:', event.message))
  })
}

export async function choose(scope: Page | Locator, label: string, option: string) {
  const control = scope.getByRole('combobox', { name: label, exact: true })
  await expect(control).toBeEnabled()
  await control.locator('..').locator('..').locator('..').click()
  await expect(control).toHaveAttribute('aria-expanded', 'true')
  const listbox = scope.getByRole('listbox', { name: label, exact: true })
  await listbox.getByRole('option', { name: option, exact: true }).click()
  await expect(control).toHaveAttribute('aria-expanded', 'false')
}

export async function check(control: Locator, checked = true) {
  // Element Plus hides the native input and renders its clickable box beside it.
  if ((await control.isChecked()) !== checked) await control.locator('..').click()
  await expect(control).toBeChecked({ checked })
}

export async function more(page: Page, row: Locator, label: string) {
  await row.getByRole('button', { name: /的更多操作$/ }).click()
  await page
    .locator('.row-more-actions:visible')
    .getByRole('button', { name: label, exact: true })
    .click()
}

export async function openUpload(page: Page) {
  await page.getByRole('button', { name: '上传文件', exact: true }).click()
  const panel = page.getByRole('region', { name: '上传文件', exact: true })
  await expect(panel).toBeVisible()
  return panel
}
