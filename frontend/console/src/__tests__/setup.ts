import { afterEach, beforeEach, vi } from 'vitest'
import { config, enableAutoUnmount } from '@vue/test-utils'
import { ui } from '../plugins/element-plus'
config.global.plugins = [ui]
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
window.matchMedia =
  window.matchMedia ||
  (() =>
    ({
      matches: false,
      addEventListener() {},
      removeEventListener() {},
    }) as unknown as MediaQueryList)
import { clearSession, initialized, checking, sessionError } from '../stores/session'
beforeEach(() => {
  clearSession()
  initialized.value = true
  checking.value = false
  sessionError.value = ''
})
enableAutoUnmount(afterEach)
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})
