import { flushPromises, mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import LoginView from '../views/login/LoginView.vue'
import { fakeApi } from './support'

it('submits once from Enter in the password field and keeps required validation', async () => {
  const logins: unknown[] = []
  fakeApi((path, init) => {
    if (path === '/auth/login') {
      logins.push(JSON.parse(String(init.body)))
      return new Response('{"code":"invalid_credentials","detail":"用户名或密码错误"}', {
        status: 401,
      })
    }
    return new Response('{}', { status: 401 })
  })
  const wrapper = mount(LoginView, { attachTo: document.body })
  const password = wrapper.get('input[name=password]')
  await password.setValue('secret-value')
  // The username is still empty: native validation stops the submission.
  await password.trigger('keydown', { key: 'Enter' })
  await flushPromises()
  expect(logins).toEqual([])

  await wrapper.get('input[name=username]').setValue('admin')
  await password.trigger('keydown', { key: 'Enter' })
  await flushPromises()
  expect(logins).toEqual([{ username: 'admin', password: 'secret-value' }])
  expect(wrapper.text()).toContain('用户名或密码错误')
  wrapper.unmount()
})
