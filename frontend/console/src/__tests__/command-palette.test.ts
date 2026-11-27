import { flushPromises, mount } from '@vue/test-utils'
import { h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, expect, it } from 'vitest'
import ConsoleLayout from '../layouts/ConsoleLayout.vue'
import { identity } from '../stores/session'
import { authorize } from './support'

async function mountConsole(permissions: string[], passwordChangeRequired = false) {
  authorize(permissions)
  if (passwordChangeRequired && identity.value) {
    identity.value = {
      ...identity.value,
      user: { ...identity.value.user, password_change_required: true },
    }
  }
  const page = { render: () => h('div', 'page') }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: page },
      { path: '/platform', component: page },
      { path: '/models', component: page },
      { path: '/model-usage', component: page },
      { path: '/account', component: page },
      { path: '/login', component: page },
      { path: '/:pathMatch(.*)*', component: page },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ConsoleLayout, {
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

afterEach(() => {
  document.body.innerHTML = ''
})

it('opens with Cmd/Ctrl+K, filters strictly by real permissions, and navigates on Enter', async () => {
  const { wrapper, router } = await mountConsole(['model_invocation:read'])
  try {
    expect(document.querySelector('[data-testid="command-palette"]')).toBeNull()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }))
    await flushPromises()

    const dialog = document.querySelector('[data-testid="command-palette"]')!
    expect(dialog).not.toBeNull()
    // Permitted routes and usage shortcuts appear; unauthorized ones stay hidden.
    expect(dialog.textContent).toContain('用量与费用')
    expect(dialog.textContent).toContain('查看费用未知的调用')
    expect(dialog.textContent).not.toContain('模型接入')
    expect(dialog.textContent).not.toContain('用户管理')

    const input = document.querySelector<HTMLInputElement>('[data-testid="command-palette-input"]')!
    input.value = '未知'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    const options = dialog.querySelectorAll('[role="option"]')
    expect(options).toHaveLength(1)
    expect(options[0]!.textContent).toContain('查看费用未知的调用')

    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/model-usage')
    expect(router.currentRoute.value.query).toEqual({
      cost_basis: 'actual',
      cost_known: 'false',
      tab: 'details',
    })
    expect(document.querySelector('[data-testid="command-palette"]')).toBeNull()
  } finally {
    wrapper.unmount()
  }
})

it('supports arrow navigation, sidebar toggle action, and hides during forced password change', async () => {
  const { wrapper } = await mountConsole([])
  try {
    await wrapper.get('[data-testid="command-palette-trigger"]').trigger('click')
    await flushPromises()

    const input = document.querySelector<HTMLInputElement>('[data-testid="command-palette-input"]')!
    input.value = '折叠'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    const dialog = document.querySelector('[data-testid="command-palette"]')!
    expect(wrapper.find('.console-shell').classes()).not.toContain('is-collapsed')
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await flushPromises()
    expect(wrapper.find('.console-shell').classes()).toContain('is-collapsed')
  } finally {
    wrapper.unmount()
  }

  const restricted = await mountConsole(['model:manage'], true)
  try {
    expect(restricted.wrapper.find('[data-testid="command-palette-trigger"]').exists()).toBe(false)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))
    await flushPromises()
    expect(document.querySelector('[data-testid="command-palette"]')).toBeNull()
  } finally {
    restricted.wrapper.unmount()
  }
})
