import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory } from 'vue-router'
import { makeRouter, navigation, canNavigate } from '../router'
import ModelDefaultsView from '../views/flows/ModelDefaultsView.vue'
import { identity } from '../stores/session'
import { authorize, chooseSelect, fakeApi } from './support'

const row = (model_key: string | null) => ({
  definition_key: 'document.review',
  definition_name: '文档审阅',
  role: 'review',
  role_label: '审阅',
  operation: 'text.generate',
  delivery: 'sync',
  model_key,
})
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)

describe('flow model defaults', () => {
  it('is reachable only with its own permission', () => {
    const item = navigation.find((entry) => entry.path === '/flow-model-defaults')!
    authorize(['flow:read'])
    expect(canNavigate(item)).toBe(false)
    authorize(['flow:configure'])
    expect(canNavigate(item)).toBe(true)
  })

  it('chooses each role default from the usable models and keeps a stale one visible', async () => {
    authorize(['flow:configure', 'model:invoke'])
    const saved: unknown[] = []
    let current: string | null = 'retired'
    fakeApi((path, init) => {
      if (path === '/auth/me') return identity.value
      if (init.method === 'PUT') {
        saved.push([path, JSON.parse(String(init.body))])
        current = 'chat'
        return row(current)
      }
      if (path.endsWith('/models'))
        return [
          { key: 'chat', name: '对话模型' },
          { key: 'fast', name: '快速模型' },
        ]
      return [row(current)]
    })
    const router = makeRouter(createMemoryHistory('/console/'))
    await router.push('/flow-model-defaults')
    const wrapper = mount(ModelDefaultsView, {
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
    expect(wrapper.text()).toContain('文档审阅')
    expect(button(wrapper, '保存')!.attributes('disabled')).toBeDefined()
    await chooseSelect(wrapper, 'chat', '文档审阅的审阅模型')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(saved).toEqual([['/flow-model-defaults/document.review/review', { model_key: 'chat' }]])
    expect(wrapper.text()).toContain('默认模型已保存')
    wrapper.unmount()
  })
})
