import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { request } from '../api/client'
import {
  changePassword,
  checking,
  clearSession,
  identity,
  login,
  logout,
  refreshSession,
  sessionError,
  sessionVersion,
  SESSION_EVENT,
} from '../stores/session'
import { authorize, deferred, fakeApi } from './support'

function account(id = 'new') {
  authorize(['user:read'])
  identity.value = { ...identity.value!, user: { ...identity.value!.user, id, username: id } }
  return identity.value
}
it('does not wait for a pre-login refresh or accept its late expired response', async () => {
  const user = account(),
    old = deferred<unknown>()
  clearSession()
  let reads = 0
  fakeApi((path) =>
    path === '/auth/login' ? { csrf_token: 'new-csrf' } : ++reads === 1 ? old.promise : user,
  )
  const refreshing = refreshSession()
  await login('new', 'password')
  expect(identity.value?.user.id).toBe('new')
  old.resolve(new Response('{}', { status: 401 }))
  await refreshing
  expect(identity.value?.user.id).toBe('new')
})
it('an old refresh cannot clear the newer pending refresh or its checking state', async () => {
  const user = account(),
    old = deferred<unknown>(),
    fresh = deferred<unknown>()
  let reads = 0
  fakeApi(() => (++reads === 1 ? old.promise : fresh.promise))
  const previous = refreshSession()
  clearSession()
  const current = refreshSession()
  old.resolve(user)
  await previous
  expect(checking.value).toBe(true)
  expect(refreshSession()).toBe(current)
  expect(reads).toBe(2)
  fresh.resolve(user)
  await current
  expect(checking.value).toBe(false)
})
it('waits for login before servicing a window-focus refresh', async () => {
  const user = account(),
    gate = deferred<unknown>()
  clearSession()
  let reads = 0
  fakeApi((path) => {
    if (path === '/auth/login') return gate.promise
    reads++
    return user
  })
  const signingIn = login('new', 'password'),
    focused = refreshSession()
  await flushPromises()
  expect(reads).toBe(0)
  gate.resolve({ csrf_token: 'new-csrf' })
  await Promise.all([signingIn, focused])
  expect(reads).toBe(1)
  expect(identity.value?.user.id).toBe('new')
})
it('keeps invalid credential errors and reports verification failure after login', async () => {
  fakeApi(
    () =>
      new Response('{"code":"invalid_credentials","detail":"用户名或密码错误"}', { status: 401 }),
  )
  await expect(login('wrong', 'wrong')).rejects.toMatchObject({
    status: 401,
    code: 'invalid_credentials',
  })
  fakeApi((path) =>
    path === '/auth/login' ? { csrf_token: 'new' } : new Response('{}', { status: 503 }),
  )
  await expect(login('new', 'password')).rejects.toMatchObject({ code: 'session_unavailable' })
  expect(identity.value).toBeNull()
  expect(sessionError.value).not.toBe('')
})
it('cannot report an obsolete login as successful after another account is loaded', async () => {
  const oldUser = account(),
    newUser = account('other'),
    gate = deferred<unknown>()
  clearSession()
  let reads = 0
  fakeApi((path) =>
    path === '/auth/login' ? { csrf_token: 'old' } : ++reads === 1 ? gate.promise : newUser,
  )
  const pending = login('new', 'password').catch((error) => error.code)
  await flushPromises()
  clearSession()
  await refreshSession()
  gate.resolve(oldUser)
  expect(await pending).toBe('identity_changed')
  expect(identity.value?.user.id).toBe('other')
})
it('rejects overlapping local session changes and preserves the running login', async () => {
  const user = account(),
    gate = deferred<unknown>()
  clearSession()
  fakeApi((path) => (path === '/auth/login' ? gate.promise : user))
  const signingIn = login('new', 'password')
  await expect(logout()).rejects.toMatchObject({ code: 'session_busy' })
  gate.resolve({ csrf_token: 'new' })
  await signingIn
  expect(identity.value?.user.id).toBe('new')
})
it('clears an already expired logout and broadcasts no credentials', async () => {
  account()
  const writes = vi.spyOn(Storage.prototype, 'setItem')
  fakeApi(() => new Response('{}', { status: 401 }))
  await logout()
  expect(identity.value).toBeNull()
  expect(writes).toHaveBeenCalledWith(SESSION_EVENT, expect.any(String))
  expect(writes.mock.calls.every(([, value]) => !value.includes('csrf'))).toBe(true)
})
it('invalidates uploads and other work before changing password, then clears identity', async () => {
  account()
  const pending = deferred<unknown>()
  let signal: AbortSignal | null = null
  fakeApi((path, init) => {
    if (path === '/auth/password') return {}
    signal = init.signal as AbortSignal
    return pending.promise
  })
  const old = request('/users').catch((error) => error.code)
  await changePassword('old', 'new-password')
  expect(signal!.aborted).toBe(true)
  pending.resolve([])
  expect(await old).toBe('identity_changed')
  expect(identity.value).toBeNull()
})
it('keeps stable identity refreshes mounted and invalidates work when CSRF changes', async () => {
  const user = account(),
    pending = deferred<unknown>()
  fakeApi((path) => (path === '/auth/me' ? user : pending.promise))
  const version = sessionVersion.value
  await refreshSession()
  expect(sessionVersion.value).toBe(version)
  const old = request('/users').catch((error) => error.code)
  fakeApi(() => ({ ...user, csrf_token: 'rotated' }))
  await refreshSession()
  pending.resolve([])
  expect(await old).toBe('identity_changed')
  expect(sessionVersion.value).toBeGreaterThan(version)
})

it('clears an expired session on password change while preserving its original 401', async () => {
  account()
  fakeApi(() => new Response('{"code":"authentication_required"}', { status: 401 }))
  await expect(changePassword('old', 'new')).rejects.toMatchObject({ status: 401 })
  expect(identity.value).toBeNull()
})
