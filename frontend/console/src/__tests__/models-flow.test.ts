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
import FlowManager from '../views/flows/FlowsView.vue'
import FlowRunDialog from '../views/flows/components/FlowRunDialog.vue'
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
    if (path === '/model-access/options') return { providers: ['openrouter'] }
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
      return { providers: ['openrouter', 'newapi', 'yunwu'], balance_providers: [] }
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
it.each(['', '   '])('resets a cleared model display name to its fixed key (%j)', async (value) => {
  authorize(['model:manage'])
  const writes: { path: string; body: Record<string, unknown> }[] = []
  fakeApi((path, init) => {
    if (init.method === 'PATCH') {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      return entry
    }
    if (path === '/model-access/options') return { providers: ['openrouter'] }
    if (path.endsWith('/models')) return { items: [{ id: 'mock-chat' }] }
    return path.startsWith('/model-entries') ? [{ ...entry, name: '旧显示名称' }] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '编辑模型').trigger('click')
  const name = wrapper.get('.page-editor input[placeholder="留空使用模型 key"]')
  expect((name.element as HTMLInputElement).value).toBe('旧显示名称')
  await name.setValue(value)
  await wrapper.get('.page-editor form').trigger('submit')
  await flushPromises()
  expect(writes).toHaveLength(1)
  expect(writes[0]!.path).toBe(`/model-entries/${id}`)
  expect(writes[0]!.body.name).toBe(entry.key)
  expect(writes[0]!.body).not.toHaveProperty('key')
})
it.each([
  'https://newapiai.com/v1',
  'https://api.openlux.ai/v1',
  'https://yunwu.ai/v1',
  'https://relay.example/gateway/api/v1',
])('uses one NewAPI option and saves its configured API base (%s)', async (base) => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return {
        providers: ['openrouter', 'newapi', 'yunwu'],
        balance_providers: ['openrouter'],
        default_api_bases: { newapi: 'https://newapiai.com/v1' },
      }
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
})
it.each(['openlux', 'yunwu'])(
  'edits a legacy %s channel through the Yunwu UI',
  async (provider) => {
    authorize(['model:manage'])
    fakeApi((path) =>
      path === '/model-access/options'
        ? { providers: ['openrouter', 'newapi', 'yunwu'], balance_providers: ['openrouter'] }
        : path.startsWith('/model-channels')
          ? [{ ...channel, provider }]
          : [],
    )
    const wrapper = mount(ModelManager)
    await flushPromises()
    await button(wrapper, '编辑渠道').trigger('click')
    const control = selectControl(wrapper, '服务商')
    expect(control.props('modelValue')).toBe('yunwu')
    expect(control.text()).not.toContain('Openlux')
    expect(control.text()).toContain('Yunwu')
    expect((wrapper.get('input[type=url]').element as HTMLInputElement).value).toBe(
      channel.api_base,
    )
  },
)
it('creates a Yunwu protocol channel for Openlux and keeps it separate from NewAPI', async () => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return {
        providers: ['openrouter', 'newapi', 'yunwu'],
        balance_providers: ['openrouter', 'newapi'],
        default_api_bases: { yunwu: 'https://yunwu.ai/v1' },
      }
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
      ? {
          providers: ['openrouter', 'newapi', 'yunwu'],
          balance_providers: ['openrouter', 'newapi'],
        }
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
        ? { providers: ['newapi'], balance_providers: ['newapi'] }
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
      return { providers: ['openrouter'], balance_providers: ['openrouter'] }
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
        return { providers: ['openrouter'], balance_providers: ['openrouter'] }
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
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role=dialog]').text()).toContain('只能停用，不能删除')
    referenced = false
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[role=dialog]').exists()).toBe(false)
    expect(deletes).toHaveLength(2)
  },
)
it('uses the official address and no fake upstream model when configuring live mode', async () => {
  authorize(['model:manage'])
  fakeApi((path) => (path === '/model-access/options' ? { providers: ['openrouter'] } : []))
  const wrapper = mount(ModelManager)
  await flushPromises()
  expect(wrapper.text()).toContain('模型接入')
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
      return { providers: ['openrouter'], balance_providers: ['openrouter'] }
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

it.each([
  [
    'aliyun_bailian',
    '阿里云百炼（DashScope）',
    'https://dashscope.aliyuncs.com/compatible-mode/v1',
  ],
  ['volcengine', '火山方舟（豆包）', 'https://ark.cn-beijing.volces.com/api/v3'],
])('creates the %s cloud channel with its default URL', async (provider, label, base) => {
  authorize(['model:manage'])
  const writes: unknown[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return {
        providers: ['openrouter', 'aliyun_bailian', 'volcengine'],
        balance_providers: [],
        default_api_bases: { [provider]: base },
      }
    if (init.method === 'POST') {
      writes.push(JSON.parse(String(init.body)))
      return { ...channel, provider, api_base: base }
    }
    return []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await button(wrapper, '新建渠道').trigger('click')
  await chooseSelect(wrapper, provider, '服务商')
  expect(selectControl(wrapper, '服务商').text()).toContain(label)
  expect((wrapper.get('input[type=url]').element as HTMLInputElement).value).toBe(base)
  await wrapper.get('[role=dialog] input').setValue('Cloud')
  await wrapper.get('input[type=password]').setValue('cloud-key')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes).toHaveLength(1)
  expect(writes[0]).toMatchObject({ provider, api_base: base, api_key: 'cloud-key' })
})
