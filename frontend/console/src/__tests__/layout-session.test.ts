import { flushPromises, mount } from '@vue/test-utils'
import { h, onMounted, onUnmounted, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { expect, it } from 'vitest'
import ConsoleLayout from '../layouts/ConsoleLayout.vue'
import { identity } from '../stores/session'
import { authorize, deferred, fakeApi } from './support'

async function layout() {
  const lifecycle = { mounts: 0, unmounts: 0 }
  const editor = {
    setup() {
      const draft = ref('')
      onMounted(() => lifecycle.mounts++)
      onUnmounted(() => lifecycle.unmounts++)
      return () =>
        h('input', {
          'aria-label': '未保存的模型名称',
          value: draft.value,
          onInput: (event: Event) => (draft.value = (event.target as HTMLInputElement).value),
        })
    },
  }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/models', component: editor },
      { path: '/login', component: { render: () => h('h1', '登录') } },
      { path: '/:pathMatch(.*)*', component: editor },
    ],
  })
  await router.push('/models')
  await router.isReady()
  const wrapper = mount(ConsoleLayout, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, lifecycle }
}

it('keeps the current form mounted during and after an unchanged focus verification', async () => {
  authorize(['model:manage'])
  const user = identity.value
  const pending = deferred<unknown>()
  fakeApi(() => pending.promise)
  const { wrapper, lifecycle } = await layout()
  const input = wrapper.get('input[aria-label="未保存的模型名称"]')
  await input.setValue('尚未保存')
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(wrapper.get('input').element).toBe(input.element)
  expect(lifecycle).toEqual({ mounts: 1, unmounts: 0 })
  pending.resolve(user)
  await flushPromises()
  expect(wrapper.get('input').element).toBe(input.element)
  expect((input.element as HTMLInputElement).value).toBe('尚未保存')
  expect(lifecycle).toEqual({ mounts: 1, unmounts: 0 })
})

it.each(['revoked', 'expired'])(
  'still clears the mounted page when identity is %s',
  async (change) => {
    authorize(['model:manage'])
    const user = identity.value!
    const pending = deferred<unknown>()
    fakeApi(() => pending.promise)
    const { wrapper, lifecycle } = await layout()
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    pending.resolve(
      change === 'revoked' ? { ...user, permissions: [] } : new Response('{}', { status: 401 }),
    )
    await flushPromises()
    expect(wrapper.find('input[aria-label="未保存的模型名称"]').exists()).toBe(false)
    expect(lifecycle.unmounts).toBeGreaterThanOrEqual(1)
    expect(wrapper.text()).toContain(change === 'revoked' ? '没有此页面的访问权限' : '登录')
  },
)
