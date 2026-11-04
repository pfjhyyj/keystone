import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ElOption } from 'element-plus'
import PricingDialog from '../views/models/components/PricingDialog.vue'
import { authorize, chooseSelect, fakeApi } from './support'
import type { PricingView, PricingConfig, PricingTarget } from '../api/pricing'

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
const chat = (): PricingTarget['profile'] => JSON.parse(JSON.stringify(profile))
const wrappers: VueWrapper[] = []
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks()
})
const button = (wrapper: VueWrapper, text: string) =>
  wrapper.findAll('button').find((item) => item.text() === text)!
const operationOptions = (wrapper: VueWrapper) => {
  const control = wrapper
    .findAll('[aria-label="适用操作"]')
    .find((item) => item.findAllComponents(ElOption).length)!
  return control.findAllComponents(ElOption)
}
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
  await wrapper.get('input[aria-label="全部输入第一档单价"]').setValue('2')
  await wrapper.get('input[aria-label="全部输出（含推理）第一档单价"]').setValue('8')
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

it('builds a three-tier chain with exclusive lower bounds and a readable preview', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '上下文阶梯').trigger('click')
  await button(wrapper, '添加档位').trigger('click')
  await wrapper.get('input[aria-label="第一档上限"]').setValue('128000')
  await wrapper.get('input[aria-label="第一档上限"]').trigger('blur')
  await wrapper.get('input[aria-label="第二档上限"]').setValue('256000')
  await wrapper.get('input[aria-label="第二档上限"]').trigger('blur')
  for (const [tier, input, output] of [
    ['一', '0.8', '4.8'],
    ['二', '2', '12'],
    ['三', '4', '24'],
  ])
    for (const [label, rate] of [
      ['全部输入', input],
      ['全部输出（含推理）', output],
    ])
      await wrapper.get(`input[aria-label="${label}第${tier}档单价"]`).setValue(rate)
  expect(wrapper.text()).toContain('128000 < 完整输入 ≤ 256000')
  expect(wrapper.text()).toContain('完整输入 > 256000')
  expect(wrapper.get('pre[aria-label="规则预览"]').text()).toContain(
    'tier("tier_3", 全部输入 × 4 + 全部输出（含推理） × 24)',
  )
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const config = requests.find((item) => item.method === 'PUT')!.body?.config as PricingConfig
  expect(config.rules[0]?.rule.root).toMatchObject({
    kind: 'branch',
    when: { field: 'usage.input_total_tokens', op: 'lte', value: 128000 },
    then: { id: 'tier_1', items: [{ rate: '0.8' }, { rate: '4.8' }] },
    else: {
      kind: 'branch',
      when: { value: 256000 },
      then: { id: 'tier_2', items: [{ rate: '2' }, { rate: '12' }] },
      else: { kind: 'price', id: 'tier_3', items: [{ rate: '4' }, { rate: '24' }] },
    },
  })
})

it('rejects non-increasing tier bounds before saving', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '上下文阶梯').trigger('click')
  await button(wrapper, '添加档位').trigger('click')
  await wrapper.get('input[aria-label="第二档上限"]').setValue('1000')
  await wrapper.get('input[aria-label="第二档上限"]').trigger('blur')
  for (const tier of ['一', '二', '三'])
    for (const label of ['全部输入', '全部输出（含推理）'])
      await wrapper.get(`input[aria-label="${label}第${tier}档单价"]`).setValue('1')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('逐档递增')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
})

it('reopens a saved multi-tier cache rule in the tier form', async () => {
  const value = JSON.parse(JSON.stringify(loaded)) as PricingView
  const meter = value.operations[0]!.profile
  meter.quantities['usage.input_uncached_tokens'] = 'token'
  meter.quantities['usage.input_cache_read_tokens'] = 'token'
  meter.input_partitions.cache_read = [
    'usage.input_uncached_tokens',
    'usage.input_cache_read_tokens',
  ]
  const tier = (id: string, input: string, cached: string, output: string) => ({
    kind: 'price' as const,
    id,
    input_partition: 'cache_read',
    output_partition: 'aggregate',
    factor: '1',
    items: [
      { id: 'input', quantity: 'usage.input_uncached_tokens', rate: input, per: 1000000 },
      { id: 'output', quantity: 'usage.output_total_tokens', rate: output, per: 1000000 },
      { id: 'cache_read', quantity: 'usage.input_cache_read_tokens', rate: cached, per: 1000000 },
    ],
  })
  value.config = {
    mode: 'override',
    rules: [
      {
        operation: 'text.generate',
        delivery: 'sync',
        source: { label: '管理员配置', version: 'custom.1' },
        rule: {
          format: 1,
          currency: 'CNY',
          root: {
            kind: 'branch',
            when: { field: 'usage.input_total_tokens', op: 'lte', value: 32768 },
            then: tier('tier_0', '0.6', '0.12', '3.6'),
            else: {
              kind: 'branch',
              when: { field: 'usage.input_total_tokens', op: 'lte', value: 131072 },
              then: tier('tier_1', '0.9', '0.18', '5.4'),
              else: tier('tier_2', '1.8', '0.36', '10.8'),
            },
          },
        },
      },
    ],
  }
  const { wrapper } = await open(value)
  expect(wrapper.find('textarea[aria-label="结构化价格规则"]').exists()).toBe(false)
  const rate = (label: string) =>
    (wrapper.get(`input[aria-label="${label}"]`).element as HTMLInputElement).value
  expect(rate('缓存读取第一档单价')).toBe('0.12')
  expect(rate('未命中缓存的输入（含写入）第三档单价')).toBe('1.8')
  expect(rate('第二档上限')).toBe('131072')
})

it('prices cache writes explicitly when the protocol reports them', async () => {
  const value = JSON.parse(JSON.stringify(loaded)) as PricingView
  const meter = value.operations[0]!.profile
  meter.id = 'relay_chat.v2'
  for (const key of ['regular', 'cache_read', 'cache_write'])
    meter.quantities[`usage.input_${key}_tokens`] = 'token'
  meter.input_partitions.cache = [
    'usage.input_regular_tokens',
    'usage.input_cache_read_tokens',
    'usage.input_cache_write_tokens',
  ]
  const { wrapper, requests } = await open(value)
  await custom(wrapper)
  await chooseSelect(wrapper, 'cache', '输入计价分区')
  expect(wrapper.text()).toContain('缓存读取和缓存写入各自定价')
  await wrapper.get('input[aria-label="普通输入（排除缓存）单价"]').setValue('3')
  await wrapper.get('input[aria-label="缓存读取单价"]').setValue('0.3')
  await wrapper.get('input[aria-label="缓存写入单价"]').setValue('3.75')
  await wrapper.get('input[aria-label="全部输出（含推理）单价"]').setValue('15')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const config = requests.find((item) => item.method === 'PUT')!.body?.config as PricingConfig
  expect(config.rules[0]?.rule.root).toMatchObject({
    input_partition: 'cache',
    items: [
      { quantity: 'usage.input_regular_tokens', rate: '3' },
      { quantity: 'usage.input_cache_read_tokens', rate: '0.3' },
      { quantity: 'usage.input_cache_write_tokens', rate: '3.75' },
      { quantity: 'usage.output_total_tokens', rate: '15' },
    ],
  })
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

const cacheRead = () => {
  const value = JSON.parse(JSON.stringify(loaded)) as PricingView
  const meter = value.operations[0]!.profile
  meter.id = 'relay_chat.v2'
  meter.quantities['usage.input_uncached_tokens'] = 'token'
  meter.quantities['usage.input_cache_read_tokens'] = 'token'
  meter.input_partitions.cache_read = [
    'usage.input_uncached_tokens',
    'usage.input_cache_read_tokens',
  ]
  return value
}
const priceLeaf = (id: string, input: string, cached: string, output: string) => ({
  kind: 'price' as const,
  id,
  input_partition: 'cache_read',
  output_partition: 'aggregate',
  factor: '1',
  items: [
    { id: 'input', quantity: 'usage.input_uncached_tokens', rate: input, per: 1000000 },
    { id: 'cache_read', quantity: 'usage.input_cache_read_tokens', rate: cached, per: 1000000 },
    { id: 'output', quantity: 'usage.output_total_tokens', rate: output, per: 1000000 },
  ],
})

it('prices overnight idle hours separately and previews them at the observed hour', async () => {
  const { wrapper, requests } = await open(cacheRead(), () => ({
    status: 'estimated',
    amount: '0.0017',
    currency: 'CNY',
    reason: null,
    detail: { leaf: 'idle', items: [] },
  }))
  await custom(wrapper)
  await button(wrapper, '闲忙时').trigger('click')
  await chooseSelect(wrapper, 'cache_read', '输入计价分区')
  await wrapper.get('input[aria-label="未命中缓存的输入（含写入）闲时单价"]').setValue('1')
  await wrapper.get('input[aria-label="缓存读取闲时单价"]').setValue('0.1')
  await wrapper.get('input[aria-label="全部输出（含推理）闲时单价"]').setValue('4')
  await wrapper.get('input[aria-label="未命中缓存的输入（含写入）忙时单价"]').setValue('2')
  await wrapper.get('input[aria-label="缓存读取忙时单价"]').setValue('0.2')
  await wrapper.get('input[aria-label="全部输出（含推理）忙时单价"]').setValue('8')
  expect(wrapper.get('pre[aria-label="规则预览"]').text()).toContain(
    '(pricing_hour >= 22 || pricing_hour < 8)',
  )
  await button(wrapper, '填入示例计量').trigger('click')
  expect(wrapper.get('textarea[aria-label="试算条件与计量 JSON"]').element).toHaveProperty(
    'value',
    expect.stringContaining('"pricing_hour": 23'),
  )
  await button(wrapper, '试算当前草稿').trigger('click')
  await flushPromises()
  const preview = requests.find((item) => item.path.endsWith('/preview'))!
  expect(preview.body?.context).toEqual({ pricing_hour: 23 })
  expect(preview.body?.config).toMatchObject({
    rules: [
      {
        rule: {
          time_basis: {
            source: 'observed_response',
            timezone: 'Asia/Shanghai',
            calendar: 'all_days',
          },
          root: {
            when: {
              any: [
                { field: 'context.pricing_hour', op: 'gte', value: 22 },
                { field: 'context.pricing_hour', op: 'lt', value: 8 },
              ],
            },
            then: { id: 'idle', input_partition: 'cache_read' },
            else: { id: 'peak' },
          },
        },
      },
    ],
  })
  expect(wrapper.text()).toContain('0.0017 CNY')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
})

it('keeps an idle window inside one day as a conjunction', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '闲忙时').trigger('click')
  await wrapper.get('input[aria-label="闲时开始小时"]').setValue('9')
  await wrapper.get('input[aria-label="闲时开始小时"]').trigger('blur')
  await wrapper.get('input[aria-label="闲时结束小时"]').setValue('18')
  await wrapper.get('input[aria-label="闲时结束小时"]').trigger('blur')
  for (const side of ['闲时', '忙时']) {
    await wrapper.get(`input[aria-label="全部输入${side}单价"]`).setValue('1')
    await wrapper.get(`input[aria-label="全部输出（含推理）${side}单价"]`).setValue('4')
  }
  expect(wrapper.text()).toContain('闲时为 9:00 至 18:00')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const config = requests.find((item) => item.method === 'PUT')!.body?.config as PricingConfig
  expect(config.rules[0]?.rule.root).toMatchObject({
    when: {
      all: [
        { field: 'context.pricing_hour', op: 'gte', value: 9 },
        { field: 'context.pricing_hour', op: 'lt', value: 18 },
      ],
    },
    then: { id: 'idle' },
    else: { id: 'peak' },
  })
})

it('stacks the same context tiers under both idle and peak prices', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '闲忙时').trigger('click')
  const box = wrapper
    .findAll('.el-checkbox')
    .find((item) => item.text().includes('叠加上下文阶梯'))!
  await box.get('input').setValue(true)
  await flushPromises()
  await button(wrapper, '添加档位').trigger('click')
  await wrapper.get('input[aria-label="第一档上限"]').setValue('128000')
  await wrapper.get('input[aria-label="第一档上限"]').trigger('blur')
  await wrapper.get('input[aria-label="第二档上限"]').setValue('256000')
  await wrapper.get('input[aria-label="第二档上限"]').trigger('blur')
  for (const [tier, idle, peak] of [
    ['一', '1', '2'],
    ['二', '1.5', '3'],
    ['三', '2', '4'],
  ]) {
    await wrapper.get(`input[aria-label="全部输入第${tier}档闲时单价"]`).setValue(idle)
    await wrapper.get(`input[aria-label="全部输入第${tier}档忙时单价"]`).setValue(peak)
    await wrapper.get(`input[aria-label="全部输出（含推理）第${tier}档闲时单价"]`).setValue('4')
    await wrapper.get(`input[aria-label="全部输出（含推理）第${tier}档忙时单价"]`).setValue('8')
  }
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const config = requests.find((item) => item.method === 'PUT')!.body?.config as PricingConfig
  expect(config.rules[0]?.rule.root).toMatchObject({
    when: {
      any: [
        { op: 'gte', value: 22 },
        { op: 'lt', value: 8 },
      ],
    },
    then: {
      kind: 'branch',
      when: { field: 'usage.input_total_tokens', op: 'lte', value: 128000 },
      then: { id: 'idle_tier_1', items: [{ rate: '1' }, { rate: '4' }] },
      else: {
        when: { value: 256000 },
        then: { id: 'idle_tier_2', items: [{ rate: '1.5' }, { rate: '4' }] },
        else: { id: 'idle_tier_3', items: [{ rate: '2' }, { rate: '4' }] },
      },
    },
    else: {
      then: { id: 'peak_tier_1', items: [{ rate: '2' }, { rate: '8' }] },
      else: {
        then: { id: 'peak_tier_2' },
        else: { id: 'peak_tier_3', items: [{ rate: '4' }, { rate: '8' }] },
      },
    },
  })
})

it('reopens a saved idle and peak rule in the time form', async () => {
  const value = cacheRead()
  value.config = {
    mode: 'override',
    rules: [
      {
        operation: 'text.generate',
        delivery: 'sync',
        source: { label: '管理员配置', version: 'custom.1' },
        rule: {
          format: 1,
          currency: 'CNY',
          time_basis: {
            source: 'observed_response',
            timezone: 'Asia/Shanghai',
            calendar: 'all_days',
          },
          root: {
            kind: 'branch',
            when: {
              any: [
                { field: 'context.pricing_hour', op: 'gte', value: 22 },
                { field: 'context.pricing_hour', op: 'lt', value: 8 },
              ],
            },
            then: priceLeaf('idle', '1', '0.1', '4'),
            else: priceLeaf('peak', '2', '0.2', '8'),
          },
        },
      },
    ],
  }
  const { wrapper } = await open(value)
  expect(wrapper.find('textarea[aria-label="结构化价格规则"]').exists()).toBe(false)
  const rate = (label: string) =>
    (wrapper.get(`input[aria-label="${label}"]`).element as HTMLInputElement).value
  expect(rate('缓存读取闲时单价')).toBe('0.1')
  expect(rate('未命中缓存的输入（含写入）忙时单价')).toBe('2')
  expect(rate('全部输出（含推理）闲时单价')).toBe('4')
  expect(rate('闲时开始小时')).toBe('22')
  expect(rate('闲时结束小时')).toBe('8')
  expect(wrapper.text()).toContain('北京时间 UTC+8')
})

it('rejects an idle window that starts and ends on the same hour', async () => {
  const { wrapper, requests } = await open()
  await custom(wrapper)
  await button(wrapper, '闲忙时').trigger('click')
  await wrapper.get('input[aria-label="闲时结束小时"]').setValue('22')
  await wrapper.get('input[aria-label="闲时结束小时"]').trigger('blur')
  for (const side of ['闲时', '忙时']) {
    await wrapper.get(`input[aria-label="全部输入${side}单价"]`).setValue('1')
    await wrapper.get(`input[aria-label="全部输出（含推理）${side}单价"]`).setValue('4')
  }
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('不能相同')
  expect(requests.some((item) => item.method === 'PUT')).toBe(false)
})

it('merges identically metered operations and writes one rule per member', async () => {
  const view: PricingView = {
    ...loaded,
    operations: [
      { operation: 'text.generate', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'text.structured', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'image.understand', delivery: 'sync', reference: null, profile: chat() },
    ],
  }
  const { wrapper, requests } = await open(view)
  await custom(wrapper)
  expect(operationOptions(wrapper)[0]?.props('label')).toContain('文本生成 / 结构化文本')
  expect(wrapper.text()).toContain('这些操作计量方式相同，价格只需填写一次')
  await rates(wrapper, '3', '9')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const saved = requests.find((item) => item.method === 'PUT')!
  const config = saved.body?.config as PricingConfig
  expect(config.rules).toHaveLength(3)
  expect(config.rules.map((r) => r.operation)).toEqual([
    'text.generate',
    'text.structured',
    'image.understand',
  ])
  for (const rule of config.rules) {
    expect(rule.rule.root).toMatchObject({
      items: [
        { rate: '3', per: 1000000 },
        { rate: '9', per: 1000000 },
      ],
    })
  }
})

it('splits groups with inconsistent saved rules and shows a warning', async () => {
  const view: PricingView = {
    ...loaded,
    config: {
      mode: 'override',
      rules: [
        {
          operation: 'text.generate',
          delivery: 'sync',
          rule: {
            format: 1,
            currency: 'CNY',
            root: {
              kind: 'price',
              id: 'base',
              input_partition: 'aggregate',
              output_partition: 'aggregate',
              factor: '1',
              items: [
                { id: 'input', quantity: 'usage.input_total_tokens', rate: '2', per: 1000000 },
                { id: 'output', quantity: 'usage.output_total_tokens', rate: '8', per: 1000000 },
              ],
            },
          },
          source: { label: '测试', version: 'v1' },
        },
        {
          operation: 'text.structured',
          delivery: 'sync',
          rule: {
            format: 1,
            currency: 'CNY',
            root: {
              kind: 'price',
              id: 'base',
              input_partition: 'aggregate',
              output_partition: 'aggregate',
              factor: '1',
              items: [
                { id: 'input', quantity: 'usage.input_total_tokens', rate: '5', per: 1000000 },
                { id: 'output', quantity: 'usage.output_total_tokens', rate: '10', per: 1000000 },
              ],
            },
          },
          source: { label: '测试', version: 'v1' },
        },
      ],
    },
    operations: [
      { operation: 'text.generate', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'text.structured', delivery: 'sync', reference: null, profile: chat() },
    ],
  }
  const { wrapper } = await open(view)
  const options = operationOptions(wrapper)
  expect(options).toHaveLength(2)
  expect(options[0]?.props('label')).toContain('文本生成')
  expect(options[1]?.props('label')).toContain('结构化文本')
  expect(wrapper.text()).toContain('组内价格不一致，已分开显示')
})

it('keeps operations with different profiles separate', async () => {
  const imageProfile: PricingTarget['profile'] = {
    id: 'image.v1',
    quantities: { 'usage.generated_images': 'image' },
    context_fields: {},
    input_partitions: { none: [] },
    output_partitions: { none: [] },
  }
  const view: PricingView = {
    ...loaded,
    operations: [
      { operation: 'text.generate', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'image.generate', delivery: 'sync', reference: null, profile: imageProfile },
    ],
  }
  const { wrapper } = await open(view)
  const options = operationOptions(wrapper)
  expect(options).toHaveLength(2)
  expect(options[0]?.props('label')).toContain('文本生成')
  expect(options[1]?.props('label')).toContain('图片生成')
  expect(wrapper.text()).not.toContain('计量方式相同')
})

it('splits a merged group and copies the shared editor to each member', async () => {
  const view: PricingView = {
    ...loaded,
    operations: [
      { operation: 'text.generate', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'text.structured', delivery: 'sync', reference: null, profile: chat() },
    ],
  }
  const { wrapper } = await open(view)
  await custom(wrapper)
  await rates(wrapper, '3', '9')
  await button(wrapper, '分别设置').trigger('click')
  await flushPromises()
  const options = operationOptions(wrapper)
  expect(options).toHaveLength(2)
  expect(options[0]?.props('label')).toContain('文本生成')
  expect(options[1]?.props('label')).toContain('结构化文本')
  expect((wrapper.get('input[aria-label="全部输入单价"]').element as HTMLInputElement).value).toBe(
    '3',
  )
  await chooseSelect(wrapper, 'text.structured:sync', '适用操作')
  await flushPromises()
  expect((wrapper.get('input[aria-label="全部输入单价"]').element as HTMLInputElement).value).toBe(
    '3',
  )
})

it('re-merges split groups and keeps the selected editor prices', async () => {
  const view: PricingView = {
    ...loaded,
    config: {
      mode: 'override',
      rules: [
        {
          operation: 'text.generate',
          delivery: 'sync',
          rule: {
            format: 1,
            currency: 'CNY',
            root: {
              kind: 'price',
              id: 'base',
              input_partition: 'aggregate',
              output_partition: 'aggregate',
              factor: '1',
              items: [
                { id: 'input', quantity: 'usage.input_total_tokens', rate: '2', per: 1000000 },
                { id: 'output', quantity: 'usage.output_total_tokens', rate: '8', per: 1000000 },
              ],
            },
          },
          source: { label: '测试', version: 'v1' },
        },
        {
          operation: 'text.structured',
          delivery: 'sync',
          rule: {
            format: 1,
            currency: 'CNY',
            root: {
              kind: 'price',
              id: 'base',
              input_partition: 'aggregate',
              output_partition: 'aggregate',
              factor: '1',
              items: [
                { id: 'input', quantity: 'usage.input_total_tokens', rate: '5', per: 1000000 },
                { id: 'output', quantity: 'usage.output_total_tokens', rate: '10', per: 1000000 },
              ],
            },
          },
          source: { label: '测试', version: 'v1' },
        },
      ],
    },
    operations: [
      { operation: 'text.generate', delivery: 'sync', reference: null, profile: chat() },
      { operation: 'text.structured', delivery: 'sync', reference: null, profile: chat() },
    ],
  }
  const { wrapper, requests } = await open(view)
  expect(wrapper.text()).toContain('组内价格不一致')
  await chooseSelect(wrapper, 'text.structured:sync', '适用操作')
  await flushPromises()
  await button(wrapper, '合并设置（以当前操作的价格为准）').trigger('click')
  await flushPromises()
  const options = operationOptions(wrapper)
  expect(options).toHaveLength(1)
  expect(wrapper.text()).toContain('这些操作计量方式相同')
  expect(wrapper.text()).not.toContain('组内价格不一致')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const saved = requests.find((item) => item.method === 'PUT')!
  const config = saved.body?.config as PricingConfig
  expect(config.rules).toHaveLength(2)
  for (const rule of config.rules) {
    expect(rule.rule.root).toMatchObject({
      items: [
        { rate: '5', per: 1000000 },
        { rate: '10', per: 1000000 },
      ],
    })
  }
})
