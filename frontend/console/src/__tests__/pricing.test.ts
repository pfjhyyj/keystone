import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PricingDialog from '../views/models/components/PricingDialog.vue'
import { authorize, chooseSelect, fakeApi } from './support'
import type { PricingView, PricingConfig } from '../api/pricing'

const entry = {
  id: '11111111-1111-4111-8111-111111111111',
  key: 'chat',
  name: '采购模型',
  channel_id: '22222222-2222-4222-8222-222222222222',
  provider_model: 'upstream',
  operations: ['text.generate'] as const,
  is_active: true,
}
const profile = {
  id: 'chat.v2',
  quantities: {
    'usage.input_total_tokens': 'token',
    'usage.output_total_tokens': 'token',
    'events.upstream_success_count': 'request',
  },
  context_fields: {},
  input_partitions: { none: [], aggregate: ['usage.input_total_tokens'] },
  output_partitions: { none: [], aggregate: ['usage.output_total_tokens'] },
} as const
const loaded: PricingView = {
  revision: 7,
  binding_valid: true,
  config: { mode: 'inherit', rules: [] },
  operations: [
    {
      operation: 'text.generate',
      delivery: 'sync',
      reference: null,
      profile: JSON.parse(JSON.stringify(profile)),
    },
  ],
}
const wrappers: VueWrapper[] = []
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks()
})
const button = (wrapper: VueWrapper, text: string) =>
  wrapper.findAll('button').find((item) => item.text() === text)!
async function open(value = loaded, handler?: (path: string, init: RequestInit) => unknown) {
  authorize(['model:manage'])
  const requests: { path: string; method: string; body: Record<string, unknown> | null }[] = []
  fakeApi((path, init) => {
    const method = init.method ?? 'GET'
    requests.push({ path, method, body: init.body ? JSON.parse(String(init.body)) : null })
    if (method === 'GET') return value
    return handler?.(path, init) ?? value
  })
  const wrapper = mount(PricingDialog, {
    props: { entry: { ...entry, operations: [...entry.operations] } },
    global: { stubs: { AppDialog: { template: '<section><slot /></section>' } } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, requests }
}
async function custom(wrapper: VueWrapper) {
  await wrapper.get('input[type=radio][value=override]').setValue()
  await flushPromises()
}
async function rates(wrapper: VueWrapper, input = '2', output = '8') {
  await wrapper.get('input[aria-label="全部输入单价"]').setValue(input)
  await wrapper.get('input[aria-label="全部输出（含推理）单价"]').setValue(output)
}

it('explains an unlisted reference without inventing a zero price or invoking a model', async () => {
  const { wrapper, requests } = await open()
  expect(wrapper.text()).toContain('没有匹配此渠道和上游模型的参考价')
  expect(requests).toHaveLength(1)
  expect(requests[0]?.method).toBe('GET')
})

it('saves bounded decimal token prices with the revision read from the server', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await rates(wrapper, '2.000000000000000001', '8')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const saved = requests.find((item) => item.method === 'PUT')!
  expect(saved.body?.expected_revision).toBe(7)
  const config = saved.body?.config as PricingConfig
  expect(config.rules[0]?.rule.root).toMatchObject({
    kind: 'price',
    input_partition: 'aggregate',
    items: [
      { rate: '2.000000000000000001', per: 1000000 },
      { rate: '8', per: 1000000 },
    ],
  })
  expect(requests.some((item) => item.path.includes('/models/'))).toBe(false)
  expect(wrapper.emitted('saved')).toHaveLength(1)
})

it('requires an explicit free price and keeps blank prices unsaved', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('免费项请明确填 0')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
  await rates(wrapper, '0', '0')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(requests.some((item) => item.method === 'PUT')).toBe(true)
})

it('previews a full-context tier draft with manual facts and never saves it', async () => {
  const { wrapper, requests } = await open(loaded, () => ({
    status: 'estimated',
    amount: '0.0036',
    currency: 'CNY',
    reason: null,
    detail: { leaf: 'tier_a', items: [] },
  }))
  await custom(wrapper)
  await button(wrapper, '上下文阶梯').trigger('click')
  await rates(wrapper)
  await wrapper.get('input[aria-label="全部输入第二档单价"]').setValue('4')
  await wrapper.get('input[aria-label="全部输出（含推理）第二档单价"]').setValue('16')
  await button(wrapper, '填入示例计量').trigger('click')
  await button(wrapper, '试算当前草稿').trigger('click')
  await flushPromises()
  const preview = requests.find((item) => item.path.endsWith('/preview'))!
  expect(preview.body?.config).toMatchObject({
    rules: [
      {
        rule: {
          root: {
            kind: 'branch',
            when: { field: 'usage.input_total_tokens', op: 'lte', value: 32000 },
          },
        },
      },
    ],
  })
  expect(preview.body?.facts).toMatchObject({
    profile: 'chat.v2',
    values: {
      'usage.input_total_tokens': { value: 1000, source: 'manual' },
    },
  })
  expect(wrapper.text()).toContain('0.0036 CNY')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
})

it('builds cache-hit pricing and a consistent manual sample without requiring cache-write counts', async () => {
  const value = JSON.parse(JSON.stringify(loaded)) as PricingView
  const meter = value.operations[0]!.profile
  meter.id = 'relay_chat.v2'
  meter.quantities['usage.input_uncached_tokens'] = 'token'
  meter.quantities['usage.input_cache_read_tokens'] = 'token'
  meter.input_partitions.cache_read = [
    'usage.input_uncached_tokens',
    'usage.input_cache_read_tokens',
  ]
  const { wrapper, requests } = await open(value, () => ({
    status: 'estimated',
    amount: '0.00342',
    currency: 'CNY',
    reason: null,
    detail: { items: [] },
  }))
  await custom(wrapper)
  await chooseSelect(wrapper, 'cache_read', '输入计价分区')
  await wrapper.get('input[aria-label="未命中缓存的输入（含写入）单价"]').setValue('2')
  await wrapper.get('input[aria-label="缓存读取单价"]').setValue('0.2')
  await wrapper.get('input[aria-label="全部输出（含推理）单价"]').setValue('8')
  await button(wrapper, '填入示例计量').trigger('click')
  await button(wrapper, '试算当前草稿').trigger('click')
  await flushPromises()
  const preview = requests.find((item) => item.path.endsWith('/preview'))!
  expect(preview.body?.config).toMatchObject({
    rules: [{ rule: { root: { input_partition: 'cache_read' } } }],
  })
  expect(preview.body?.facts).toMatchObject({
    profile: 'relay_chat.v2',
    values: {
      'usage.input_total_tokens': { value: 1000 },
      'usage.input_uncached_tokens': { value: 900 },
      'usage.input_cache_read_tokens': { value: 100 },
    },
  })
  expect(wrapper.text()).toContain('0.00342 CNY')
})

it('preserves an unsaved tariff after a revision conflict and offers reload', async () => {
  const { wrapper, requests } = await open(
    loaded,
    () =>
      new Response(
        JSON.stringify({
          code: 'pricing_revision_conflict',
          detail: '价格已被修改，请重新加载后编辑',
        }),
        { status: 409, headers: { 'content-type': 'application/json' } },
      ),
  )
  await custom(wrapper)
  await rates(wrapper, '2', '8')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('价格已被修改')
  expect((wrapper.get('input[aria-label="全部输入单价"]').element as HTMLInputElement).value).toBe(
    '2',
  )
  expect(button(wrapper, '重新加载价格').exists()).toBe(true)
  expect(wrapper.emitted('close')).toBeUndefined()
  expect(requests.filter((item) => item.method === 'PUT')).toHaveLength(1)
})

it('rejects invalid advanced JSON locally without discarding the draft', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '高级 JSON').trigger('click')
  await wrapper.get('textarea[aria-label="结构化价格规则"]').setValue('{ invalid')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('价格规则 JSON 格式有误')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
})
