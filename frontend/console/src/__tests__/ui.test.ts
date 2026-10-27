import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { expect, it, vi } from 'vitest'
import { ElMessageBox, ElOption } from 'element-plus'
import { defineComponent, h, ref } from 'vue'
import { useUnsavedChanges } from '../composables/useUnsavedChanges'
import AppDialog from '../components/AppDialog.vue'
import AppSelect from '../components/AppSelect.vue'
import AccountView from '../views/account/AccountView.vue'
import { identity } from '../stores/session'
import { makeRouter } from '../router'
import { authorize, chooseSelect, deferred, fakeApi } from './support'

it('protects unsaved work on navigation and allows leaving after an explicit discard', async () => {
  const dirty = ref(true)
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
  const editor = defineComponent({
    setup() {
      useUnsavedChanges(dirty)
      return () => h('p', '编辑内容')
    },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/edit', component: editor },
      { path: '/done', component: { render: () => h('p', '完成') } },
    ],
  })
  await router.push('/edit')
  mount(RouterView, { global: { plugins: [router] } })
  await flushPromises()
  const unload = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(unload)
  expect(unload.defaultPrevented).toBe(true)
  await router.push('/done')
  expect(router.currentRoute.value.path).toBe('/edit')
  confirm.mockResolvedValue('confirm' as never)
  await router.push('/done')
  expect(router.currentRoute.value.path).toBe('/done')
  confirm.mockRestore()
})

it('does not revoke a personal token until the user confirms', async () => {
  authorize([])
  const confirm = vi
    .spyOn(ElMessageBox, 'confirm')
    .mockRejectedValueOnce('cancel')
    .mockResolvedValueOnce('confirm' as never)
  const deletes: string[] = []
  fakeApi((path, init) => {
    if (path === '/auth/me') return identity.value
    if (init.method === 'DELETE') {
      deletes.push(path)
      return {}
    }
    return [
      {
        id: 'token-one',
        name: '本地脚本',
        permissions: [],
        expires_at: '2030-01-01T00:00:00Z',
        revoked_at: null,
      },
    ]
  })
  const router = makeRouter(createMemoryHistory('/console/'))
  await router.push('/account?tab=tokens')
  const wrapper = mount(AccountView, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper
    .findAll('button')
    .find((node) => node.text() === '撤销')!
    .trigger('click')
  await flushPromises()
  expect(deletes).toEqual([])
  expect(confirm).toHaveBeenCalledOnce()
  await wrapper
    .findAll('button')
    .find((node) => node.text() === '撤销')!
    .trigger('click')
  await flushPromises()
  expect(deletes).toEqual(['/auth/tokens/token-one'])
  confirm.mockRestore()
})

it('validates a required Element selection through the native form', async () => {
  const wrapper = mount(
    {
      components: { AppSelect, ElOption },
      data: () => ({ value: '' }),
      template:
        '<form><AppSelect v-model="value" aria-label="模型" required><ElOption value="chat" label="测试模型" /></AppSelect></form>',
    },
    { attachTo: document.body },
  )
  expect((wrapper.get('form').element as HTMLFormElement).checkValidity()).toBe(false)
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('请选择模型')
  await chooseSelect(wrapper, 'chat', '模型')
  expect((wrapper.get('form').element as HTMLFormElement).checkValidity()).toBe(true)
  expect(wrapper.find('[role=alert]').exists()).toBe(false)
})

it('prevents closing a pending dialog and enables closing after completion', async () => {
  const wrapper = mount(AppDialog, {
    props: { title: '保存用户', busy: true },
    slots: { default: '<p>内容</p>' },
  })
  await flushPromises()
  const close = wrapper.get('button[aria-label="关闭对话框"]')
  expect(close.attributes('disabled')).toBeDefined()
  await close.trigger('click')
  expect(wrapper.emitted('close')).toBeUndefined()
  await wrapper.setProps({ busy: false })
  await close.trigger('click')
  expect(wrapper.emitted('close')).toHaveLength(1)
})

it('uses explicit dirty state so filter inputs do not masquerade as unsaved form changes', async () => {
  const wrapper = mount(AppDialog, {
    props: { title: '权限', protectChanges: true, dirty: false },
    slots: { default: '<input aria-label="筛选权限" />' },
  })
  await flushPromises()
  await wrapper.get('input').setValue('read')
  await wrapper.get('button[aria-label="关闭对话框"]').trigger('click')
  expect(wrapper.emitted('close')).toHaveLength(1)
  await wrapper.setProps({ dirty: true })
  await wrapper.get('button[aria-label="关闭对话框"]').trigger('click')
  expect(wrapper.emitted('close')).toHaveLength(1)
  expect(wrapper.get('.discard-confirmation').text()).toContain('尚有未保存的修改')
})

it('sends only selected PAT permissions and clears the revealed secret when leaving its tab', async () => {
  authorize(['user:read', 'audit:read'])
  const bodies: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (path === '/auth/me') return identity.value
    if (init.method === 'POST') {
      bodies.push(JSON.parse(String(init.body)))
      return { token: 'secret-shown-once' }
    }
    return []
  })
  const router = makeRouter(createMemoryHistory('/console/'))
  await router.push('/account?tab=tokens')
  const wrapper = mount(AccountView, { global: { plugins: [router] } })
  await flushPromises()
  expect(wrapper.find('.token-form').exists()).toBe(false)
  await wrapper
    .findAll('button')
    .find((node) => node.text() === '新建令牌')!
    .trigger('click')
  await wrapper.get('input[placeholder="例如：本地脚本"]').setValue('local')
  await wrapper.get('.permission-table input[type=checkbox]').setValue(true)
  await wrapper.get('.token-form').trigger('submit')
  await flushPromises()
  expect(bodies[0]).toMatchObject({ name: 'local', permissions: ['user:read'] })
  expect(wrapper.get('textarea[aria-label="新令牌明文"]').element).toHaveProperty(
    'value',
    'secret-shown-once',
  )
  await router.push('/account?tab=profile')
  await flushPromises()
  expect(wrapper.find('textarea[aria-label="新令牌明文"]').exists()).toBe(false)
})

it('clears all password inputs after a rejected password change', async () => {
  authorize([])
  const result = deferred<Response>()
  fakeApi((path) => (path === '/auth/me' ? identity.value : result.promise))
  const router = makeRouter(createMemoryHistory('/console/'))
  await router.push('/account?tab=password')
  const wrapper = mount(AccountView, { global: { plugins: [router] } })
  await flushPromises()
  for (const input of wrapper.findAll('.password-form input'))
    await input.setValue('new-password-123')
  await wrapper.get('.password-form').trigger('submit')
  result.resolve(new Response(JSON.stringify({ detail: '当前密码错误' }), { status: 400 }))
  await flushPromises()
  expect(wrapper.text()).toContain('当前密码错误')
  expect(
    wrapper
      .findAll('.password-form input')
      .every((input) => (input.element as HTMLInputElement).value === ''),
  ).toBe(true)
})
