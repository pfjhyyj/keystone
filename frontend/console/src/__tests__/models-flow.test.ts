import * as confirmation from '../utils/confirm'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  config,
  DOMWrapper,
  flushPromises,
  mount,
  RouterLinkStub,
  type VueWrapper,
} from '@vue/test-utils'
import ModelManager from '../views/models/ModelsView.vue'
import { modelOptionsFixture } from './model-options.fixture'
import FlowManager from '../views/flows/FlowsView.vue'
import FlowRunDialog from '../views/flows/components/FlowRunDialog.vue'
import ListPager from '../components/ListPager.vue'
import { authorize, deferred, fakeApi, chooseSelect, selectControl } from './support'

const id = '11111111-1111-4111-8111-111111111111',
  channelId = '22222222-2222-4222-8222-222222222222'
const stamp = '2026-01-01T00:00:00Z'
const channel = {
  id: channelId,
  name: '模拟渠道',
  provider: 'openrouter',
  api_base: 'https://mock.invalid/v1',
  has_api_key: true,
  has_billing_api_key: true,
  upstream_account_ref: 'account',
  is_active: true,
  balance_snapshot: { items: [{ kind: 'remaining', amount: '2.00', unit: 'credits' }] },
  balance_check_status: 'error',
  balance_checked_at: stamp,
  balance_last_attempt_at: stamp,
}
const entry = {
  id,
  key: 'chat',
  channel_id: channelId,
  provider_model: 'mock-chat',
  is_active: true,
}
const costs = {
  attempts_by_status: { succeeded: 1 },
  unknown_cost_attempts: 1,
  known_costs: [{ channel_id: channelId, unit: 'credits', amount: '0.001200', attempts: 1 }],
}
const flow = {
  id,
  definition_key: 'document.review',
  actor_id: 'admin',
  status: 'running',
  input: {},
  result: null,
  created_at: stamp,
  finished_at: null,
  observed_at: stamp,
  cancel_requested_at: null,
  error_code: null,
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!
beforeEach(() => {
  config.global.stubs = { ...config.global.stubs, RouterLink: RouterLinkStub }
})
afterEach(() => {
  delete (config.global.stubs as Record<string, unknown>).RouterLink
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('keeps history readers out of configuration and invocation APIs', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/summary') ? costs : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).not.toContain('新建渠道')
  expect(wrapper.text()).not.toContain('发送真实请求')
  expect(wrapper.text()).not.toContain('调用记录与费用')
  expect(paths).toEqual([])
})
it('preserves channel fields and labels stale balance; provider and account are immutable', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    if (path === '/model-access/options') return modelOptionsFixture({ creatable: ['openrouter'] })
    return path.startsWith('/model-channels') ? [channel] : [entry]
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).toContain('上次成功结果 · 查询失败')
  await button(wrapper, '编辑渠道').trigger('click')
  expect(selectControl(wrapper, '服务商').props('disabled')).toBe(true)
  expect(wrapper.findAll('[role=dialog] input').at(-2)!.attributes('disabled')).toBeDefined()
  await wrapper.findAll('[role=dialog] input[type=checkbox]').at(-1)!.setValue(false)
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toEqual([
    {
      name: '模拟渠道',
      api_base: 'https://mock.invalid/v1',
      clear_billing_api_key: false,
      upstream_account_ref: 'account',
      is_active: false,
    },
  ])
})
it('updates write-only API keys and clears optional billing keys without retaining the input', async () => {
  authorize(['model:manage'])
  const writes: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    if (path === '/model-access/options') return modelOptionsFixture({ creatable: ['openrouter'] })
    return path.startsWith('/model-channels') ? [channel] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '编辑渠道').trigger('click')
  expect(
    wrapper
      .findAll('input[type=password]')
      .every((input) => (input.element as HTMLInputElement).value === ''),
  ).toBe(true)
  await wrapper.get('input[type=password]').setValue('replacement-api-key')
  await wrapper.get('[role=dialog] input[type=checkbox]').setValue(true)
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes[0]).toMatchObject({ api_key: 'replacement-api-key', clear_billing_api_key: true })
  expect(writes[0]).not.toHaveProperty('billing_api_key')
  await button(wrapper, '编辑渠道').trigger('click')
  expect((wrapper.get('input[type=password]').element as HTMLInputElement).value).toBe('')
})
it('preserves an off-page channel and never sends model key in PATCH', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return entry
    }
    if (path === '/model-access/options') return modelOptionsFixture({ creatable: ['openrouter'] })
    if (path.endsWith('/models')) return { items: [{ id: 'mock-chat' }] }
    return path.startsWith('/model-entries') ? [entry] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '编辑模型').trigger('click')
  await flushPromises()
  expect(wrapper.get('.page-editor input[pattern]').attributes('disabled')).toBeDefined()
  expect(selectControl(wrapper, '模型渠道').text()).toContain(channelId)
  await wrapper.get('.page-editor form').trigger('submit')
  await flushPromises()
  expect(writes).toEqual([
    {
      channel_id: channelId,
      name: 'chat',
      provider_model: 'mock-chat',
      is_active: true,
      operations: ['text.generate'],
    },
  ])
})
it('loads channel models, selects an upstream ID and suggests a model key', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  let discoveryCalls = 0
  fakeApi((path, init) => {
    if (init.method === 'POST') {
      writes.push(JSON.parse(String(init.body)))
      return entry
    }
    if (path === '/model-access/options')
      return modelOptionsFixture({
        creatable: ['openrouter', 'newapi', 'yunwu'],
        balance: [],
      })
    if (path.endsWith('/models')) {
      discoveryCalls++
      return { items: [{ id: 'vendor/chat-v1' }] }
    }
    return path.startsWith('/model-channels') ? [{ ...channel, provider: 'openlux' }] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(button(wrapper, '刷新余额').attributes('disabled')).toBeDefined()
  await button(wrapper, '额度详情').trigger('click')
  await flushPromises()
  expect(new DOMWrapper(document.body).get('[role=dialog]').text()).toContain(
    '当前渠道尚未接入账户余额查询',
  )
  await new DOMWrapper(document.body).get('[aria-label="关闭对话框"]').trigger('click')
  await flushPromises()
  await button(wrapper, '新建模型').trigger('click')
  await flushPromises()
  await chooseSelect(wrapper, channelId, '模型渠道')
  expect(wrapper.text()).toContain('已获取 1 个模型')
  expect(discoveryCalls).toBe(1)
  await button(wrapper, '刷新上游模型').trigger('click')
  await flushPromises()
  expect(discoveryCalls).toBe(2)
  await chooseSelect(wrapper, 'vendor/chat-v1', '上游模型')
  expect((wrapper.get('.page-editor input[pattern]').element as HTMLInputElement).value).toBe(
    'vendor-chat-v1',
  )
  await wrapper.get('.page-editor form').trigger('submit')
  await flushPromises()
  expect(writes).toEqual([
    {
      key: 'vendor-chat-v1',
      name: 'vendor-chat-v1',
      channel_id: channelId,
      provider_model: 'vendor/chat-v1',
      operations: ['text.generate'],
      is_active: true,
    },
  ])
})
it('resets a blank model display name to its fixed key', async () => {
  authorize(['model:manage'])
  const writes: { path: string; body: Record<string, unknown> }[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      return entry
    }
    if (path === '/model-access/options') return modelOptionsFixture({ creatable: ['openrouter'] })
    if (path.endsWith('/models')) return { items: [{ id: 'mock-chat' }] }
    return path.startsWith('/model-entries') ? [{ ...entry, name: '旧显示名称' }] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '编辑模型').trigger('click')
  const name = wrapper.get('.page-editor input[placeholder="留空使用模型 key"]')
  expect((name.element as HTMLInputElement).value).toBe('旧显示名称')
  await name.setValue('   ')
  await wrapper.get('.page-editor form').trigger('submit')
  await flushPromises()
  expect(writes).toHaveLength(1)
  expect(writes[0]!.path).toBe(`/model-entries/${id}`)
  expect(writes[0]!.body.name).toBe(entry.key)
  expect(writes[0]!.body).not.toHaveProperty('key')
})
it.each(['https://newapiai.com/v1', 'https://relay.example/gateway/api/v1'])(
  'uses one NewAPI option and saves its configured API base (%s)',
  async (base) => {
    authorize(['model:manage'])
    const writes: unknown[] = []
    fakeApi((path, init) => {
      if (path === '/model-access/options')
        return modelOptionsFixture({
          creatable: ['openrouter', 'newapi', 'yunwu'],
          balance: ['openrouter'],
          api_bases: { newapi: 'https://newapiai.com/v1' },
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
    await chooseSelect(wrapper, 'newapi', '服务商')
    const address = wrapper.get('input[type=url]')
    expect((address.element as HTMLInputElement).value).toBe('https://newapiai.com/v1')
    expect(selectControl(wrapper, '服务商').text()).not.toContain('Openlux')
    expect(selectControl(wrapper, '服务商').text()).toContain('Yunwu')
    await address.setValue(base)
    await wrapper.get('[role=dialog] input').setValue('My NewAPI')
    await wrapper.get('input[type=password]').setValue('private-key')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toMatchObject({ provider: 'newapi', api_base: base, api_key: 'private-key' })
  },
)
it('creates a Yunwu protocol channel for Openlux and keeps it separate from NewAPI', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return modelOptionsFixture({
        creatable: ['openrouter', 'newapi', 'yunwu'],
        balance: ['openrouter', 'newapi'],
        api_bases: { yunwu: 'https://yunwu.ai/v1' },
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
  await chooseSelect(wrapper, 'yunwu', '服务商')
  const address = wrapper.get('input[type=url]')
  expect((address.element as HTMLInputElement).value).toBe('https://yunwu.ai/v1')
  await address.setValue('https://api.openlux.ai/v1')
  await wrapper.get('[role=dialog] input').setValue('Openlux 渠道')
  await wrapper.get('input[type=password]').setValue('private-key')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes[0]).toMatchObject({ provider: 'yunwu', api_base: 'https://api.openlux.ai/v1' })
})
it('shows unlimited NewAPI Token quota without suggesting a zero account balance', async () => {
  authorize(['model:manage'])
  fakeApi((path) =>
    path === '/model-access/options'
      ? modelOptionsFixture({
          creatable: ['openrouter', 'newapi', 'yunwu'],
          balance: ['openrouter', 'newapi'],
        })
      : path.startsWith('/model-channels')
        ? [
            {
              ...channel,
              provider: 'newapi',
              api_base: 'https://relay.example/v1',
              balance_snapshot: {
                scope: 'token_quota',
                unlimited: true,
                items: [{ kind: 'used', amount: '0.1', unit: 'USD' }],
              },
            },
          ]
        : [],
  )
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).toContain('当前 Token 不限额')
  expect(button(wrapper, '刷新余额').attributes('disabled')).toBeUndefined()
  await button(wrapper, '额度详情').trigger('click')
  await flushPromises()
  expect(new DOMWrapper(document.body).get('[role=dialog]').text()).toContain('不是账户总余额')
})
it.each([true, false])(
  'shows account remaining separately from Token quota (unlimited=%s)',
  async (unlimited) => {
    authorize(['model:manage'])
    fakeApi((path) =>
      path === '/model-access/options'
        ? modelOptionsFixture({ creatable: ['newapi'], balance: ['newapi'] })
        : path.startsWith('/model-channels')
          ? [
              {
                ...channel,
                provider: 'newapi',
                balance_snapshot: {
                  scope: 'provider_account',
                  unlimited,
                  items: [{ kind: 'remaining', amount: '192.851716', unit: 'USD' }],
                  token_items: unlimited
                    ? [{ kind: 'used', amount: '0.1', unit: 'USD' }]
                    : [{ kind: 'remaining', amount: '0', unit: 'USD' }],
                },
              },
            ]
          : [],
    )
    const wrapper = mount(ModelManager)
    await flushPromises()
    expect(wrapper.text()).toContain('账户余额')
    expect(wrapper.text()).toContain('192.851716 USD')
    expect(wrapper.text()).not.toContain('当前 Key 的 Token 额度')
    await button(wrapper, '额度详情').trigger('click')
    await flushPromises()
    expect(new DOMWrapper(document.body).get('[role=dialog]').text()).toContain(
      '当前 Key 的 Token 额度',
    )
    expect(
      new DOMWrapper(document.body).get('[role=dialog]').text().includes('当前 Token 不限额'),
    ).toBe(unlimited)
    expect(wrapper.text()).not.toContain('上游未返回账户余额')
  },
)
it.each(['渠道', '模型'])('renames %s from the list using only the display name', async (kind) => {
  authorize(['model:manage'])
  const writes: { path: string; body: unknown }[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      return {}
    }
    if (path === '/model-access/options')
      return modelOptionsFixture({ creatable: ['openrouter'], balance: ['openrouter'] })
    return path.startsWith('/model-channels') ? [channel] : [entry]
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await wrapper
    .get(`#configuration-${kind === '渠道' ? 'channels' : 'models'} .row-more-trigger`)
    .trigger('click')
  await flushPromises()
  await button(new DOMWrapper(document.body) as unknown as VueWrapper, `修改${kind}名称`).trigger(
    'click',
  )
  await wrapper.get('[role=dialog] input').setValue('新的显示名称')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toEqual([
    {
      path: kind === '渠道' ? `/model-channels/${channelId}/name` : `/model-entries/${id}/name`,
      body: { name: '新的显示名称' },
    },
  ])
  expect(wrapper.find('[role=dialog]').exists()).toBe(false)
})
it.each(['渠道', '模型'])(
  'confirms deleting %s and keeps referenced rows when the API refuses',
  async (kind) => {
    const approval = deferred<boolean>()
    const confirm = vi
      .spyOn(confirmation, 'confirmAction')
      .mockReturnValueOnce(approval.promise)
      .mockResolvedValue(true)
    authorize(['model:manage'])
    const deletes: string[] = []
    let referenced = true
    fakeApi((path, init) => {
      if (init.method === 'DELETE') {
        deletes.push(path)
        return referenced
          ? new Response(
              JSON.stringify({
                code: 'model_entry_referenced',
                detail: '已有流程引用，只能停用，不能删除',
              }),
              { status: 409, headers: { 'Content-Type': 'application/json' } },
            )
          : new Response(null, { status: 204 })
      }
      if (path === '/model-access/options')
        return modelOptionsFixture({ creatable: ['openrouter'], balance: ['openrouter'] })
      return path.startsWith('/model-channels') ? [channel] : [entry]
    })
    const wrapper = mount(ModelManager)
    await flushPromises()
    await wrapper
      .get(`#configuration-${kind === '渠道' ? 'channels' : 'models'} .row-more-trigger`)
      .trigger('click')
    await flushPromises()
    await button(new DOMWrapper(document.body) as unknown as VueWrapper, `删除${kind}`).trigger(
      'click',
    )
    expect(deletes).toEqual([])
    approval.resolve(true)
    await flushPromises()
    expect(wrapper.text()).toContain('只能停用，不能删除')
    referenced = false
    await wrapper
      .get(`#configuration-${kind === '渠道' ? 'channels' : 'models'} .row-more-trigger`)
      .trigger('click')
    await flushPromises()
    await button(new DOMWrapper(document.body) as unknown as VueWrapper, `删除${kind}`).trigger(
      'click',
    )
    await flushPromises()
    expect(wrapper.find('[role=dialog]').exists()).toBe(false)
    expect(deletes).toHaveLength(2)
    expect(confirm).toHaveBeenCalledTimes(2)
  },
)
it('does not prefill a fake upstream model when configuring live mode', async () => {
  authorize(['model:manage'])
  fakeApi((path) =>
    path === '/model-access/options' ? modelOptionsFixture({ creatable: ['openrouter'] }) : [],
  )
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '新建模型').trigger('click')
  await flushPromises()
  expect(
    wrapper
      .findAll('input')
      .some((input) => (input.element as HTMLInputElement).value === 'mock-chat'),
  ).toBe(false)
})
it('keeps run state when steps fail and never fetches unauthorized costs', async () => {
  authorize(['flow:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.endsWith('/steps')
      ? new Response(JSON.stringify({ detail: '执行历史暂不可用' }), { status: 503 })
      : flow
  })
  const wrapper = mount(FlowRunDialog, { props: { id } })
  await flushPromises()
  expect(wrapper.text()).toContain('执行中')
  expect(wrapper.text()).toContain('不代表运行失败')
  expect(wrapper.text()).toContain('无查看费用权限')
  expect(paths.some((path) => path.endsWith('/costs'))).toBe(false)
  expect(wrapper.text()).not.toContain('取消流程')
})
it('puts readable execution facts first and leaves result identifiers in raw fields', async () => {
  authorize(['flow:read'])
  fakeApi((path) => {
    if (path.includes('/diagnostics?')) return { files: { items: [], total: 0 } }
    if (path.endsWith('/steps')) return { items: [], available: false, truncated: false }
    return {
      ...flow,
      definition_name: '文本文件审查',
      status: 'succeeded',
      finished_at: new Date(Date.parse(stamp) + 7200).toISOString(),
      result: { summary: '已生成审查报告', result_file_id: 'private-file-id' },
    }
  })
  const wrapper = mount(FlowRunDialog, { props: { id } })
  await flushPromises()
  const overview = wrapper.get('.detail-summary')
  expect(overview.text()).toContain('文本文件审查')
  expect(overview.text()).toContain('耗时 7 秒')
  expect(overview.text()).not.toContain(id)
  expect(wrapper.get('.result-facts').text()).toContain('已生成审查报告')
  expect(wrapper.get('.result-facts').text()).not.toContain('private-file-id')
  expect(wrapper.get('[aria-label="关联模型调用"]').isVisible()).toBe(true)
  expect(wrapper.get('[aria-label="关联文件"]').isVisible()).toBe(false)
})
it('renders step durations, inline step model I/O and auto-loaded images on expand', async () => {
  authorize(['flow:read', 'flow:diagnose', 'model_invocation:read'])
  const originalCreate = URL.createObjectURL
  const originalRevoke = URL.revokeObjectURL
  const createdUrls: string[] = []
  const revokedUrls: string[] = []
  URL.createObjectURL = vi.fn(() => {
    const value = `blob:test-${createdUrls.length + 1}`
    createdUrls.push(value)
    return value
  })
  URL.revokeObjectURL = vi.fn((value: string) => {
    revokedUrls.push(value)
  })
  const startMs = Date.parse('2026-11-27T01:29:35Z')
  const endMs = startMs + 46_000
  const resultFileId = '356da119-e500-4158-9786-24686669c09f'
  const callId = '6dd6726f-fc7a-5073-be6b-efb79cfec5e4'
  fakeApi((path) => {
    if (path.startsWith(`/flow-runs/${id}/output-file`)) {
      return new Response(new Blob(['img'], { type: 'image/png' }), {
        status: 200,
        headers: { 'Content-Type': 'image/png' },
      })
    }
    if (path.startsWith(`/flow-runs/${id}/step-attachment`)) {
      return new Response(new Blob(['img'], { type: 'image/png' }), {
        status: 200,
        headers: { 'Content-Type': 'image/png' },
      })
    }
    if (path.startsWith(`/flow-runs/${id}/step-attempts`)) {
      return {
        truncated: false,
        items: [
          {
            attempt: 1,
            label: '编辑生图',
            kind: 'original',
            duration_ms: 46_000,
            input: null,
            output: null,
            error: null,
            model_calls: [
              {
                operation: 'image.edit',
                model_role: 'image',
                node_key: 'edit',
                invocation_id: callId,
                duration_ms: 45_500,
                input: { prompt: '第一行要求\n第二行要求', aspect_ratio: '3:4' },
                output: { images: 1 },
              },
            ],
            attachments: [
              {
                label: '输入图片 1',
                file_id: '11111111-1111-4111-8111-111111111112',
                role: 'input',
              },
              { label: '生成图片', file_id: resultFileId, role: 'output' },
            ],
          },
        ],
      }
    }
    if (path.includes('/diagnostics?')) {
      return path.includes('section=models')
        ? {
            run_id: id,
            models: {
              available: true,
              total: 1,
              truncated: false,
              next_offset: null,
              items: [
                {
                  invocation_id: callId,
                  operation: 'image.edit',
                  status: 'succeeded',
                  provider_model: 'gemini-image',
                  model_name: '生图模型',
                  channel_name: '测试渠道',
                  duration_ms: 45_500,
                  model_role: 'image',
                  node_key: 'edit',
                  agent_key: null,
                  agent_call_id: null,
                  workflow_id: 'wf-1',
                  step_id: 2,
                  error_code: null,
                },
              ],
            },
          }
        : { run_id: id, files: { total: 0, truncated: false, next_offset: null, items: [] } }
    }
    if (path.endsWith('/steps')) {
      return {
        available: true,
        truncated: false,
        items: [
          {
            workflow_id: 'wf-1',
            step_id: 2,
            name: 'example.edit',
            started_at_epoch_ms: startMs,
            completed_at_epoch_ms: endMs,
          },
        ],
      }
    }
    if (path.includes('/costs')) return costs
    return {
      ...flow,
      definition_name: '图片编辑流程',
      definition_snapshot: { output_file_fields: ['result_file_id'] },
      status: 'succeeded',
      finished_at: new Date(Date.parse(stamp) + 47_000).toISOString(),
      result: { invocation_id: callId, result_file_id: resultFileId },
    }
  })
  try {
    const wrapper = mount(FlowRunDialog, { props: { id } })
    await flushPromises()
    expect(wrapper.text()).toContain('耗时 46 秒')
    expect(wrapper.find('.output-card img').exists()).toBe(true)
    expect(wrapper.find('.result-call-chip').text()).toContain('生图模型')

    const stepDetails = wrapper.get('details.step-detail')
    ;(stepDetails.element as HTMLDetailsElement).open = true
    await stepDetails.trigger('toggle')
    await flushPromises()

    expect(stepDetails.text()).toContain('编辑生图')
    expect(stepDetails.text()).toContain('第一行要求')
    expect(stepDetails.text()).toContain('输入图片 / 文件')
    expect(stepDetails.text()).toContain('输出图片 / 产物')
    expect(stepDetails.findAll('.attachment-card img')).toHaveLength(2)

    wrapper.unmount()
    expect(revokedUrls.length).toBeGreaterThanOrEqual(3)
  } finally {
    URL.createObjectURL = originalCreate
    URL.revokeObjectURL = originalRevoke
  }
})
it('requires explicit cancel confirmation and waits for execution-side cancellation', async () => {
  const approval = deferred<boolean>()
  vi.spyOn(confirmation, 'confirmAction').mockReturnValueOnce(approval.promise)
  authorize(['flow:read', 'flow:cancel'])
  const writes: string[] = []
  fakeApi((path, init) => {
    if (init.method === 'POST') {
      writes.push(path)
      return { ...flow, cancel_requested_at: stamp }
    }
    return path.endsWith('/steps') ? { items: [], available: false, truncated: false } : flow
  })
  const wrapper = mount(FlowRunDialog, { props: { id } })
  await flushPromises()
  await button(wrapper, '取消流程').trigger('click')
  expect(writes).toEqual([])
  approval.resolve(true)
  await flushPromises()
  expect(writes).toEqual([`/flow-runs/${id}/cancel`])
  expect(wrapper.text()).toContain('取消处理中')
  expect(wrapper.text()).not.toContain('已取消')
})
it('discards old run details and stops polling after unmount', async () => {
  vi.useFakeTimers()
  authorize(['flow:read'])
  const pending = deferred<unknown>(),
    paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return pending.promise
  })
  const wrapper = mount(FlowRunDialog, { props: { id } })
  await flushPromises()
  wrapper.unmount()
  pending.resolve(flow)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(9000)
  expect(paths).toEqual([`/flow-runs/${id}`])
})
it('clears run data after object access is lost', async () => {
  authorize(['flow:read'])
  let lost = false
  fakeApi((path) =>
    lost
      ? new Response(JSON.stringify({ detail: '流程不存在或不可访问' }), { status: 404 })
      : path.endsWith('/steps')
        ? { items: [], available: false, truncated: false }
        : flow,
  )
  const wrapper = mount(FlowRunDialog, { props: { id } })
  await flushPromises()
  lost = true
  await button(wrapper, '刷新运行详情').trigger('click')
  await flushPromises()
  expect(wrapper.text()).not.toContain(id)
  expect(wrapper.text()).toContain('流程不存在或不可访问')
})
it('does not fetch start catalogs, files or models for a Flow reader', async () => {
  authorize(['flow:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return []
  })
  const wrapper = mount(FlowManager)
  await flushPromises()
  const size = wrapper.getComponent(ListPager).props('size')
  expect(paths).toEqual([`/flow-runs?offset=0&limit=${size}`])
  expect(wrapper.text()).not.toContain('发起流程')
})

it('keeps balance details open and replaces only the refreshed channel snapshot', async () => {
  authorize(['model:manage'])
  let writes = 0
  const updated = {
    ...channel,
    balance_check_status: 'ok',
    balance_snapshot: {
      scope: 'provider_account',
      items: [{ kind: 'remaining', amount: '3.50', unit: 'credits' }],
    },
  }
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return modelOptionsFixture({ creatable: ['openrouter'], balance: ['openrouter'] })
    if (init.method === 'POST') {
      writes++
      return updated
    }
    if (path.startsWith('/model-channels')) return [channel]
    return []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '额度详情').trigger('click')
  await flushPromises()
  const dialog = new DOMWrapper(document.body).get('[role=dialog]')
  expect(dialog.text()).toContain('这是上次成功结果，不代表当前余额')
  await dialog
    .findAll('button')
    .find((node) => node.text() === '刷新余额')!
    .trigger('click')
  await flushPromises()
  expect(new DOMWrapper(document.body).find('[role=dialog]').exists()).toBe(true)
  expect(new DOMWrapper(document.body).get('[role=dialog]').text()).toContain('3.5 credits')
  expect(new DOMWrapper(document.body).get('[role=dialog]').text()).toContain('查询成功')
  expect(writes).toBe(1)
})
