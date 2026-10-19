import { afterEach, beforeEach, vi } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'
import { clearSession, initialized, checking, sessionError } from '../access'
beforeEach(() => { clearSession(); initialized.value = true; checking.value = false; sessionError.value = '' })
enableAutoUnmount(afterEach)
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = '' })
// Native focus and Escape behavior are exercised in Playwright.
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
