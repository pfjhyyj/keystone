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
  await control.locator('..').locator('..').locator('..').click()
  const listbox = scope.getByRole('listbox', { name: label, exact: true })
  await listbox.getByRole('option', { name: option, exact: true }).click()
  await expect(control).toHaveAttribute('aria-expanded', 'false')
}

export async function check(control: Locator, checked = true) {
  // Element Plus hides the native input and renders its clickable box beside it.
  if ((await control.isChecked()) !== checked) await control.locator('..').click()
  await expect(control).toBeChecked({ checked })
}
