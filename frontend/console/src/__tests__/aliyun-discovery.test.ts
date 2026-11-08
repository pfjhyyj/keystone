import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ModelManager from '../views/models/ModelsView.vue'
import { modelOptionsFixture } from './model-options.fixture'
import { authorize, chooseSelect, fakeApi } from './support'

const channel = {
  id: '22222222-2222-4222-8222-222222222222',
  name: 'Aliyun',
  provider: 'aliyun_bailian',
  api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  workspace_id: 'llm-original',
  has_api_key: true,
  has_billing_api_key: false,
  upstream_account_ref: null,
  is_active: true,
  balance_snapshot: null,
  balance_check_status: 'never',
  balance_checked_at: null,
  balance_last_attempt_at: null,
}
function button(wrapper: VueWrapper, label: string) {
  return wrapper.findAll('button').find((b) => b.text() === label)!
}
function workspace(wrapper: VueWrapper) {
  return wrapper
    .findAll('label')
    .find((label) => label.text().startsWith('百炼业务空间 ID'))!
    .get('input')
}

it.each(['llm-next', ''])(
  'loads and saves the Alibaba workspace without changing inference configuration (%s)',
  async (value) => {
    authorize(['model:manage'])
    const writes: Record<string, unknown>[] = []
    fakeApi((path, init) => {
      if (path === '/model-access/options')
        return modelOptionsFixture({ providers: ['aliyun_bailian'], balance_providers: [] })
      if (init.method === 'PATCH') {
        writes.push(JSON.parse(String(init.body)))
        return { ...channel, workspace_id: value || null }
      }
      return path.startsWith('/model-channels') ? [channel] : []
    })
    const wrapper = mount(ModelManager)
    await flushPromises()
    await button(wrapper, '编辑渠道').trigger('click')
    await flushPromises()
    expect((workspace(wrapper).element as HTMLInputElement).value).toBe('llm-original')
    await workspace(wrapper).setValue(value)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toMatchObject({ workspace_id: value || null, api_base: channel.api_base })
    expect(writes[0]).not.toHaveProperty('api_key')
    expect(writes[0]).not.toHaveProperty('billing_credentials')
  },
)

it('uses the default shared endpoint without a workspace and clears unsaved workspace when changing providers', async () => {
  authorize(['model:manage'])
  const writes: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return modelOptionsFixture({
        providers: ['openrouter', 'aliyun_bailian'],
        balance_providers: [],
      })
    if (init.method === 'POST') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    return []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '新建渠道').trigger('click')
  await chooseSelect(wrapper, 'aliyun_bailian', '服务商')
  await workspace(wrapper).setValue('llm-unsaved')
  await chooseSelect(wrapper, 'openrouter', '服务商')
  expect(wrapper.text()).not.toContain('百炼业务空间 ID')
  await chooseSelect(wrapper, 'aliyun_bailian', '服务商')
  expect((workspace(wrapper).element as HTMLInputElement).value).toBe('')
  await wrapper.get('[role=dialog] input').setValue('Aliyun')
  await wrapper.get('input[type=password]').setValue('inference-key')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes[0]).toMatchObject({ provider: 'aliyun_bailian', workspace_id: null })
})
