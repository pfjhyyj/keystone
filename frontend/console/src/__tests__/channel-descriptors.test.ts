import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { resolveChannel, type ChannelDescriptor } from '../api/models'
import ModelsView from '../views/models/ModelsView.vue'
import { modelOptionsFixture } from './model-options.fixture'
import { authorize, fakeApi, selectControl } from './support'

const fiction: ChannelDescriptor = {
  ...modelOptionsFixture().channels[0]!,
  id: 'nebula_test',
  label: '星云试验渠道',
  display_as: 'nebula_test',
  default_api_base: 'https://nebula.invalid/v2',
  billing_credentials: 'cloud',
  bills: true,
  fields: [{ name: 'region_id', label: '星云区域', hint: '区域用于发现模型。' }],
  hint: '这个虚构渠道完全由 API descriptor 定义。',
}
const channel = {
  id: '22222222-2222-4222-8222-222222222222',
  name: '历史渠道',
  provider: 'openlux',
  api_base: 'https://api.openlux.ai/v1',
  has_api_key: true,
  has_billing_api_key: true,
  upstream_account_ref: null,
  is_active: true,
  balance_snapshot: null,
  balance_check_status: 'never',
  balance_checked_at: null,
  balance_last_attempt_at: null,
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((item) => item.text() === label)!

it('creates a fictional channel from its descriptor, dynamic fields and cloud credentials', async () => {
  authorize(['model:manage'])
  const writes: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options') return modelOptionsFixture({ channels: [fiction] })
    if (init.method === 'POST') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    return []
  })
  const wrapper = mount(ModelsView)
  await flushPromises()
  await button(wrapper, '新建渠道').trigger('click')
  expect(selectControl(wrapper, '服务商').text()).toContain(fiction.label)
  expect(wrapper.get('input[type=url]').element).toHaveProperty('value', fiction.default_api_base)
  expect(wrapper.text()).toContain(fiction.hint)
  expect(wrapper.text()).toContain(fiction.fields[0]!.hint)
  await wrapper.get('[role=dialog] input').setValue('新星云')
  await wrapper.get('input[aria-label="星云区域"]').setValue('moon')
  const passwords = wrapper.findAll('input[type=password]')
  await passwords[0]!.setValue('inference-key')
  await passwords[1]!.setValue('cloud-ak')
  await passwords[2]!.setValue('cloud-sk')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toHaveLength(1)
  expect(writes[0]).toMatchObject({
    provider: fiction.id,
    api_base: fiction.default_api_base,
    region_id: 'moon',
    api_key: 'inference-key',
    billing_credentials: { access_key_id: 'cloud-ak', access_key_secret: 'cloud-sk' },
  })
})

it('resolves a generic legacy site while preserving its provider and false capabilities', () => {
  const row = { provider: 'old_protocol', api_base: 'https://legacy.invalid/v1' }
  const options = modelOptionsFixture({
    channels: [fiction, { ...fiction, id: row.provider }],
    legacy_sites: [
      {
        providers: [row.provider],
        api_base: row.api_base,
        display_as: fiction.id,
        balance: false,
        bills: false,
        billing_credentials: 'none',
      },
    ],
  })
  expect(resolveChannel(row, options)).toMatchObject({
    id: row.provider,
    label: fiction.label,
    balance: false,
    bills: false,
    billing_credentials: 'none',
  })
  expect(resolveChannel({ ...row, provider: fiction.id }, options)).toMatchObject({
    id: fiction.id,
    balance: true,
    bills: true,
    billing_credentials: 'cloud',
  })
})

it('edits legacy Openlux as Yunwu without changing its provider or saved address', async () => {
  authorize(['model:manage'])
  const writes: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options') return modelOptionsFixture()
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    return path.startsWith('/model-channels') ? [channel] : []
  })
  const wrapper = mount(ModelsView)
  await flushPromises()
  await button(wrapper, '编辑渠道').trigger('click')
  const control = selectControl(wrapper, '服务商')
  expect(control.props()).toMatchObject({ modelValue: channel.provider, disabled: true })
  expect(control.text()).toContain('Yunwu')
  expect(wrapper.get('input[type=url]').element).toMatchObject({
    value: channel.api_base,
    readOnly: true,
  })
  expect(wrapper.findAll('input[type=password]')).toHaveLength(1)
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toHaveLength(1)
  expect(writes[0]).toHaveProperty('api_base', channel.api_base)
  expect(writes[0]).not.toHaveProperty('provider')
})
