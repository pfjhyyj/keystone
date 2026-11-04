import { computed, ref, shallowRef } from 'vue'
import { authApi } from '../api/auth'
import {
  ApiError,
  currentEpoch,
  invalidateRequests,
  observeErrors,
  setCsrf,
  type Identity,
} from '../api/client'

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
export const canReadAudit = () => !!identity.value?.is_administrator && can('audit:read')
export const SESSION_EVENT = 'keystone:session'
type Pending = { epoch: number; promise: Promise<void> }
let refreshing: Pending | null = null
let changing: Pending | null = null
const signature = (value: Identity | null) =>
  JSON.stringify(
    value && [
      value.user.id,
      value.user.password_change_required,
      [...value.permissions].sort(),
      value.is_administrator,
      value.credential_kind,
      value.csrf_token,
    ],
  )
function apply(value: Identity | null, force = false) {
  if (force || signature(value) !== signature(identity.value)) {
    invalidateRequests()
    sessionVersion.value++
  }
  identity.value = value
  setCsrf(value?.csrf_token ?? null)
}
export function clearSession() {
  apply(null, true)
  initialized.value = true
  checking.value = false
  sessionError.value = ''
}
export function refreshSession(): Promise<void> {
  if (changing?.epoch === currentEpoch()) return changing.promise.catch(() => {})
  return refreshIdentity()
}
function refreshIdentity(strict = false): Promise<void> {
  const epoch = currentEpoch()
  if (refreshing?.epoch === epoch) return refreshing.promise
  checking.value = true
  let promise!: Promise<void>
  promise = (async () => {
    try {
      const value = await authApi.me()
      apply(value)
      sessionError.value = ''
    } catch (error) {
      if (error instanceof ApiError && error.code === 'identity_changed') {
        if (strict) throw error
        return
      }
      clearSession()
      sessionError.value =
        error instanceof ApiError && error.status === 401 ? '' : '无法确认登录状态，请重试。'
    } finally {
      if (refreshing?.promise === promise) {
        initialized.value = true
        checking.value = false
        refreshing = null
      }
    }
  })()
  refreshing = { epoch, promise }
  return promise
}
export const ensureSession = () => (initialized.value ? Promise.resolve() : refreshSession())
function broadcast() {
  try {
    localStorage.setItem(SESSION_EVENT, crypto.randomUUID())
  } catch {
    // Focus still revalidates when local storage is unavailable.
  }
}
function change(action: () => Promise<void>): Promise<void> {
  if (changing?.epoch === currentEpoch())
    return Promise.reject(new ApiError(409, 'session_busy', '正在变更登录状态，请稍后重试。'))
  invalidateRequests()
  const epoch = currentEpoch()
  const promise = action().finally(() => {
    if (changing?.promise === promise) changing = null
  })
  changing = { epoch, promise }
  return promise
}
export function login(username: string, password: string) {
  return change(async () => {
    const result = await authApi.login(username, password)
    setCsrf(result.csrf_token)
    await refreshIdentity(true)
    if (!identity.value)
      throw new ApiError(
        503,
        'session_unavailable',
        sessionError.value || '无法确认登录状态，请重试。',
      )
    broadcast()
  })
}
export function logout() {
  return change(async () => {
    try {
      await authApi.logout()
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) throw error
    }
    clearSession()
    broadcast()
  })
}
export function changePassword(current: string, password: string) {
  return change(async () => {
    await authApi.password(current, password)
    clearSession()
    broadcast()
  })
}
observeErrors((error) => {
  if (error.status === 401) clearSession()
  else if (error.status === 403) void refreshSession()
})
