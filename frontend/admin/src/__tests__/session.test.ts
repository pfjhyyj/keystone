import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { expect, it, vi } from 'vitest'
import { clearSession, identity, initialized, refreshSession } from '../access'
import { request } from '../auth'
import { loginDestination, makeRouter } from '../router'
import App from '../App.vue'
import { authorize, deferred, fakeApi } from './support'

it('rejects late responses after logout', async () => {
  authorize(['user:read'])
  const late = deferred<unknown>()
  fakeApi(() => late.promise)
  const response = request('/users')
  clearSession()
  late.resolve([{ username: 'private-user' }])
  await expect(response).rejects.toMatchObject({ code: 'identity_changed' })
})
it('clears session on 401 and refreshes permissions on 403', async () => {
  authorize(['user:read'])
  fakeApi((path) =>
    path === '/auth/me'
      ? { ...identity.value, permissions: [] }
      : new Response(JSON.stringify({ code: 'permission_denied', detail: '没有权限' }), {
          status: 403,
        }),
  )
  await expect(request('/users')).rejects.toMatchObject({ status: 403 })
  await flushPromises()
  expect(identity.value?.permissions).toEqual([])
  fakeApi(() => new Response('{}', { status: 401 }))
  await expect(request('/users')).rejects.toMatchObject({ status: 401 })
  expect(identity.value).toBeNull()
})
it('protects direct routes and safe login destinations', async () => {
  expect(loginDestination('//evil.example')).toBe('/')
  expect(loginDestination('https://evil.example')).toBe('/')
  initialized.value = false
  fakeApi(() => new Response('{}', { status: 401 }))
  const router = makeRouter(createMemoryHistory('/admin/'))
  await router.push('/users')
  expect(router.currentRoute.value.path).toBe('/login')
  expect(router.currentRoute.value.query.next).toBe('/users')
  authorize([])
  identity.value = {
    ...identity.value!,
    user: { ...identity.value!.user, password_change_required: true },
  }
  fakeApi(() => identity.value)
  await router.push('/audit')
  expect(router.currentRoute.value.path).toBe('/account')
})
it('does not mount a forbidden page or fetch its data', async () => {
  authorize([])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return identity.value
  })
  const router = makeRouter(createMemoryHistory('/admin/'))
  await router.push('/users')
  const wrapper = mount(App, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.text()).toContain('没有此页面的访问权限')
  expect(paths.every((path) => path === '/auth/me')).toBe(true)
})
it('cannot restore an old identity from an interrupted refresh', async () => {
  authorize(['user:read'])
  const old = identity.value,
    late = deferred<unknown>()
  fakeApi(() => late.promise)
  const refreshing = refreshSession()
  clearSession()
  late.resolve(old)
  await refreshing
  expect(identity.value).toBeNull()
})

it('finishes the post-login navigation without restarting it on each identity refresh', async () => {
  authorize(['user:read'])
  const signedIn = identity.value!
  clearSession()
  let loggedIn = false,
    checks = 0
  fakeApi((path) => {
    if (path === '/auth/login') {
      loggedIn = true
      return { csrf_token: 'test-csrf' }
    }
    if (path === '/auth/me') {
      checks++
      if (checks > 4) throw new Error('Repeated session refresh')
      return loggedIn ? { ...signedIn } : new Response('{}', { status: 401 })
    }
    return []
  })
  const router = makeRouter(createMemoryHistory('/admin/'))
  await router.push('/users')
  const wrapper = mount(App, { global: { plugins: [router] } })
  await wrapper.get('input[name=username]').setValue('admin')
  await wrapper.get('input[name=password]').setValue('test-password-123')
  await wrapper.get('form').trigger('submit')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/users'))
  await flushPromises()
  expect(wrapper.text()).toContain('用户管理')
  expect(checks).toBeLessThanOrEqual(3)
})
