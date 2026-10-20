import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import ModelManager from '../views/models/ModelsView.vue'
import ModelProbe from '../views/models/components/ModelProbe.vue'
import ModelPicker from '../components/models/ModelPicker.vue'
import InvocationHistory from '../views/models/components/InvocationHistory.vue'
import FlowManager from '../views/flows/FlowsView.vue'
import FlowRunDialog from '../views/flows/components/FlowRunDialog.vue'
import FlowStartDialog from '../views/flows/components/FlowStartDialog.vue'
import * as transport from '../api/client'
import { identity } from '../stores/session'
import { authorize, deferred, fakeApi, chooseSelect, selectControl } from './support'
vi.mock('../views/flows/forms', async () => ({
  flowForms: { 'document.review': (await import('./fixtures/ReviewInput.vue')).default },
}))

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
const definition = {
  key: 'custom.flow',
  name: '自定义流程',
  description: '',
  input_schema: { type: 'object' },
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('applies session invalidation to errors carried inside a successful HTTP stream', async () => {
  authorize(['model:invoke'])
  fakeApi(
    () =>
      new Response(
        'data: {"type":"error","http_status":401,"code":"authentication_required","detail":"请重新登录"}\n\n',
        { headers: { 'Content-Type': 'text/event-stream' } },
      ),
  )
  const event = vi.fn()
  await expect(
    transport.invokeModel({}, event, new AbortController().signal),
  ).rejects.toMatchObject({ status: 401 })
  expect(identity.value).toBeNull()
  expect(event).not.toHaveBeenCalled()
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
  expect(wrapper.text()).toContain('费用未知：1')
  expect(wrapper.text()).toContain('0.0012 credits')
  expect(paths.every((path) => path.startsWith('/model-invocations'))).toBe(true)
})
it('preserves channel fields and labels stale balance; provider and account are immutable', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    if (path === '/model-access/options') return { providers: ['openrouter'] }
    return path.startsWith('/model-channels') ? [channel] : [entry]
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).toContain('这是上次成功结果，不代表当前余额')
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
    if (path === '/model-access/options') return { providers: ['openrouter'] }
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
    if (path === '/model-access/options') return { providers: ['openrouter'] }
    return path.startsWith('/model-entries') ? [entry] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '编辑模型').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=dialog] input').attributes('disabled')).toBeDefined()
  expect(selectControl(wrapper, '模型渠道').text()).toContain(channelId)
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toEqual([{ channel_id: channelId, provider_model: 'mock-chat', is_active: true }])
})
it('uses identical bounded filters for history and summary and rejects invalid IDs', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/summary') ? costs : []
  })
  const wrapper = mount(InvocationHistory, { props: { version: 0 } })
  await flushPromises()
  const initial = paths.map((path) => new URLSearchParams(path.split('?')[1]))
  expect(initial[0]!.get('since')).toBeTruthy()
  expect(initial[0]!.get('until')).toBeTruthy()
  expect(initial[0]!.get('since')).toBe(initial[1]!.get('since'))
  expect(initial[0]!.get('until')).toBe(initial[1]!.get('until'))
  paths.length = 0
  await wrapper.findAll('input')[0]!.setValue(channelId)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const params = paths.map((path) => new URLSearchParams(path.split('?')[1]))
  for (const key of ['channel_id', 'since', 'until'])
    expect(params[0]!.get(key)).toBe(params[1]!.get(key))
  paths.length = 0
  await wrapper.findAll('input')[0]!.setValue('invalid')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(paths).toEqual([])
  expect(wrapper.text()).toContain('完整 UUID')
})
it('keeps the model history window on paging and updates it for a completed invocation without applying drafts', async () => {
  authorize(['model_invocation:read'])
  let now = Date.parse('2026-10-20T10:00:00Z')
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/summary')
      ? costs
      : Array.from({ length: 50 }, (_, i) => ({
          id: `call-${i}`,
          channel_id: channelId,
          provider_model: 'chat',
          status: 'succeeded',
          created_at: stamp,
          cost_amount: null,
          cost_source: 'unknown',
        }))
  })
  const wrapper = mount(InvocationHistory, { props: { version: 0 } })
  await flushPromises()
  const original = new URLSearchParams(paths[0]!.split('?')[1])
  now += 60000
  await button(wrapper, '下一页').trigger('click')
  await flushPromises()
  expect(new URLSearchParams(paths.at(-1)!.split('?')[1]).get('until')).toBe(original.get('until'))
  await wrapper.findAll('input')[0]!.setValue('unapplied-draft')
  paths.length = 0
  await wrapper.setProps({ version: 1 })
  await flushPromises()
  expect(paths).toHaveLength(2)
  for (const path of paths) {
    const params = new URLSearchParams(path.split('?')[1])
    expect(params.get('until')).toBe(new Date(now).toISOString())
    expect(params.get('since')).toBe(new Date(now - 30 * 86400000).toISOString())
    expect(params.has('channel_id')).toBe(false)
  }
  expect(paths.some((path) => path.includes('offset=50'))).toBe(false)
  await wrapper.findAll('input')[0]!.setValue(channelId)
  const dates = wrapper.findAll('input[type=datetime-local]')
  await dates[0]!.setValue('2026-10-01T00:00')
  await dates[1]!.setValue('2026-10-19T00:00')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const bounded = new URLSearchParams(paths.at(-1)!.split('?')[1])
  paths.length = 0
  now += 60000
  await wrapper.setProps({ version: 2 })
  await flushPromises()
  for (const path of paths) {
    const params = new URLSearchParams(path.split('?')[1])
    expect(params.get('channel_id')).toBe(channelId)
    expect(params.get('since')).toBe(bounded.get('since'))
    expect(params.get('until')).toBe(bounded.get('until'))
  }
})
it('paginates the model catalog while retaining selection', async () => {
  authorize(['model:invoke'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return { items: Array.from({ length: 50 }, (_, i) => ({ key: `model${i}` })) }
  })
  const wrapper = mount(ModelPicker, { props: { modelValue: 'chosen-off-page' } })
  await flushPromises()
  await button(wrapper, '下一页').trigger('click')
  await flushPromises()
  expect(paths[1]).toContain('offset=50')
  expect(selectControl(wrapper).text()).toContain('chosen-off-page')
})
it('stops receiving without resending and aborts on disposal', async () => {
  authorize(['model:invoke'])
  fakeApi(() => ({ items: [{ key: 'chat' }] }))
  let signal: AbortSignal | undefined
  const spy = vi
    .spyOn(transport, 'invokeModel')
    .mockImplementation(async (_body, _event, incoming) => {
      signal = incoming
      await new Promise((_done, reject) =>
        incoming.addEventListener('abort', () => reject(new Error('aborted'))),
      )
    })
  const completed = vi.fn()
  const wrapper = mount(ModelProbe, { props: { onCompleted: completed } })
  await flushPromises()
  await chooseSelect(wrapper, 'chat')
  await wrapper.get('textarea').setValue('hello')
  await wrapper.get('form').trigger('submit')
  await wrapper.get('form').trigger('submit')
  expect(spy).toHaveBeenCalledTimes(1)
  await button(wrapper, '停止接收').trigger('click')
  await flushPromises()
  expect(signal?.aborted).toBe(true)
  expect(wrapper.text()).toContain('上游可能已经执行')
  await wrapper.get('form').trigger('submit')
  wrapper.unmount()
  await flushPromises()
  expect(signal?.aborted).toBe(true)
  expect(completed).toHaveBeenCalledTimes(1)
})
it('requires a model and message before submitting a real request', async () => {
  authorize(['model:invoke'])
  fakeApi(() => ({ items: [] }))
  const spy = vi.spyOn(transport, 'invokeModel')
  const wrapper = mount(ModelProbe)
  await flushPromises()
  await wrapper.get('form').trigger('submit')
  expect(spy).not.toHaveBeenCalled()
  expect(button(wrapper, '发送真实请求').attributes('disabled')).toBeDefined()
})
it('labels live calls and sends only after an explicit submission', async () => {
  authorize(['model:invoke'])
  fakeApi(() => ({ items: [{ key: 'chat' }] }))
  const spy = vi.spyOn(transport, 'invokeModel').mockResolvedValue(undefined)
  const wrapper = mount(ModelProbe)
  await flushPromises()
  expect(wrapper.text()).toContain('可能产生真实费用')
  expect(wrapper.find('section').attributes('aria-label')).toBe('真实调用')
  expect(spy).not.toHaveBeenCalled()
  await chooseSelect(wrapper, 'chat')
  await wrapper.get('textarea').setValue('hello')
  expect(button(wrapper, '发送真实请求').attributes('disabled')).toBeUndefined()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(spy).toHaveBeenCalledTimes(1)
  expect(spy.mock.calls[0]![0]).toMatchObject({ model_key: 'chat', stream: true })
})
it('uses the official address and no fake upstream model when configuring live mode', async () => {
  authorize(['model:manage'])
  fakeApi((path) => (path === '/model-access/options' ? { providers: ['openrouter'] } : []))
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).toContain('真实发送（仅 OpenRouter）')
  await button(wrapper, '新建渠道').trigger('click')
  await flushPromises()
  expect(selectControl(wrapper, '服务商').text()).not.toContain('DeepSeek')
  expect((wrapper.get('input[type="url"]').element as HTMLInputElement).value).toBe(
    'https://openrouter.ai/api/v1',
  )
  expect(wrapper.text()).toContain('Management Key')
  await wrapper.get('button[aria-label="关闭对话框"]').trigger('click')
  await flushPromises()
  await button(wrapper, '新建模型').trigger('click')
  await flushPromises()
  expect(
    wrapper
      .findAll('input')
      .some((input) => (input.element as HTMLInputElement).value === 'mock-chat'),
  ).toBe(false)
})
it('keeps a Flow request key after failure and dialog reopen; a new request changes it', async () => {
  authorize(['flow:start'])
  const bodies: { request_key: string }[] = []
  fakeApi((_path, init) => {
    if (init.method === 'POST') {
      bodies.push(JSON.parse(String(init.body)))
      return new Response(JSON.stringify({ detail: '暂不可用' }), { status: 503 })
    }
    return []
  })
  const wrapper = mount(FlowStartDialog, {
    props: { open: true, definitions: [definition], enabled: true, idempotencyDays: 7 },
  })
  await flushPromises()
  await wrapper.get('textarea').setValue('{"count":2}')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await wrapper.setProps({ open: false })
  await wrapper.setProps({ open: true })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(bodies[0]!.request_key).toBe(bodies[1]!.request_key)
  await button(wrapper, '准备新的运行').trigger('click')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(bodies[2]!.request_key).not.toBe(bodies[1]!.request_key)
})
it('rejects non-object Flow JSON without sending', async () => {
  authorize(['flow:start'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return []
  })
  const wrapper = mount(FlowStartDialog, {
    props: { open: true, definitions: [definition], enabled: true, idempotencyDays: 7 },
  })
  await flushPromises()
  await wrapper.get('textarea').setValue('[]')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(paths).toEqual([])
  expect(wrapper.text()).toContain('JSON 对象')
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
it('requires explicit cancel confirmation and waits for execution-side cancellation', async () => {
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
  await wrapper.get('.confirmation').trigger('submit')
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
  expect(paths).toEqual(['/flow-runs?offset=0&limit=50'])
  expect(wrapper.text()).not.toContain('发起流程')
})
it('distinguishes catalog loading and failure, retries and opens the selected Flow definition', async () => {
  authorize(['flow:start', 'flow:read'])
  const pending = deferred<unknown>()
  let attempts = 0
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    if (path === '/flow-definitions') {
      attempts++
      return attempts === 1
        ? pending.promise
        : {
            enabled: true,
            idempotency_days: 7,
            items: [
              definition,
              { ...definition, key: 'custom.other', name: '第二个流程', description: '批量处理' },
            ],
          }
    }
    return []
  })
  const wrapper = mount(FlowManager, { global: { stubs: { RouterLink: RouterLinkStub } } })
  await flushPromises()
  expect(wrapper.text()).toContain('正在加载流程目录')
  expect(wrapper.text()).not.toContain('尚未启用')
  pending.resolve(new Response(JSON.stringify({ detail: '流程目录暂不可用' }), { status: 503 }))
  await flushPromises()
  expect(wrapper.text()).toContain('流程目录暂不可用')
  await button(wrapper, '刷新流程目录').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/')
  await wrapper.get('input[placeholder="搜索名称、标识或说明"]').setValue(' 批量处理 ')
  expect(wrapper.text()).toContain('显示 1 / 2 个流程')
  await button(wrapper, '发起流程').trigger('click')
  await flushPromises()
  expect(selectControl(wrapper, '流程').props('modelValue')).toBe('custom.other')
  await wrapper.get('button[aria-label="关闭对话框"]').trigger('click')
  await chooseSelect(wrapper, 'failed', '运行状态')
  const runs = wrapper.get('section[aria-label="运行记录"]')
  await runs.get('form').trigger('submit')
  await flushPromises()
  expect(paths.at(-1)).toContain('status=failed')
  await button(wrapper, '重置筛选').trigger('click')
  await flushPromises()
  expect(paths.at(-1)).not.toContain('status=')
})
it('retains a composed Flow input across reopen and clears it when switching definitions', async () => {
  authorize(['flow:start', 'file:inspect', 'model:invoke'])
  const bodies: { definition_key: string; request_key: string; input: Record<string, unknown> }[] =
    []
  fakeApi((path, init) => {
    if (init.method === 'POST') {
      bodies.push(JSON.parse(String(init.body)))
      return new Response(JSON.stringify({ detail: '暂不可用' }), { status: 503 })
    }
    return path.startsWith('/models') ? { items: [{ key: 'chat' }] } : []
  })
  const wrapper = mount(FlowStartDialog, {
    props: {
      open: true,
      definitions: [{ ...definition, key: 'document.review' }, definition],
      enabled: true,
      idempotencyDays: 7,
    },
  })
  await flushPromises()
  await wrapper.get('input[placeholder="已知文件 UUID"]').setValue(id)
  await chooseSelect(wrapper, 'chat', '模型')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await wrapper.setProps({ open: false })
  await wrapper.setProps({ open: true })
  await flushPromises()
  expect(
    (wrapper.get('input[placeholder="已知文件 UUID"]').element as HTMLInputElement).value,
  ).toBe(id)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(bodies[1]).toEqual(bodies[0])
  expect(bodies[0]!.input).toEqual({ source_file_id: id, model_key: 'chat' })
  await chooseSelect(wrapper, definition.key, '流程')
  await wrapper.get('textarea').setValue('{"count":2}')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(bodies[2]!.input).toEqual({ count: 2 })
  expect(bodies[2]!.request_key).not.toBe(bodies[1]!.request_key)
})
