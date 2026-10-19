// Small shared session state; adapted from Ether's access store without a store framework.
import { computed, ref, shallowRef } from 'vue'
import { ApiError, invalidateRequests, observeErrors, request, setCsrf } from '../api/client'
import type { Identity } from '../api/client'

export const identity = shallowRef<Identity | null>(null)
export const initialized = ref(false)
export const checking = ref(false)
export const sessionError = ref('')
export const sessionVersion = ref(0)
export const permissions = computed(() => identity.value?.permissions ?? [])
export const ready = computed(
  () => !!identity.value && !identity.value.user.password_change_required,
)
export const can = (permission: string) => ready.value && permissions.value.includes(permission)
let pending: Promise<void> | null = null

export function clearSession() {
  invalidateRequests()
  identity.value = null
  setCsrf(null)
  initialized.value = true
  sessionVersion.value++
}

export function refreshSession(): Promise<void> {
  if (pending) return pending
  checking.value = true
  sessionError.value = ''
  pending = (async () => {
    try {
      const value = await request<Identity>('/auth/me', 'GET', undefined, false)
      const signature = (item: Identity | null) =>
        JSON.stringify(
          item && [
            item.user.id,
            item.user.password_change_required,
            [...item.permissions].sort(),
            item.is_administrator,
          ],
        )
      if (signature(value) !== signature(identity.value)) {
        invalidateRequests()
        sessionVersion.value++
      }
      identity.value = value
      setCsrf(value.csrf_token)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'identity_changed') return
      clearSession()
      if (!(error instanceof ApiError && error.status === 401))
        sessionError.value = '无法确认登录状态，请重试。'
    } finally {
      initialized.value = true
      checking.value = false
      pending = null
    }
  })()
  return pending
}

export const ensureSession = () => (initialized.value ? Promise.resolve() : refreshSession())

observeErrors((error) => {
  if (error.status === 401) clearSession()
  else if (error.status === 403) void refreshSession()
})
