import { expect, it, vi, afterEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import UsageView from '../views/usage/UsageView.vue'
import StatisticsPicker from '../views/usage/components/StatisticsPicker.vue'
import MoneyAmount from '../components/MoneyAmount.vue'
import { compactMoney } from '../api/models'
import { ElOption, ElSelect } from 'element-plus'
import { type UsageMetrics, type UsageStatistics } from '../api/modelUsage'
import { identity } from '../stores/session'
import { authorize, deferred, fakeApi, chooseSelect, selectControl } from './support'
const channel = '22222222-2222-4222-8222-222222222222',
  call = '11111111-1111-4111-8111-111111111111'
const since = '2026-10-01T00:00:00.000Z',
  until = '2026-10-21T00:00:00.000Z'
const metrics: UsageMetrics = {
  calls: 2,
  succeeded: 1,
  failed: 1,
  unknown: 0,
  running: 0,
  known_calls: 1,
  unknown_cost_calls: 1,
  pending_calls: 1,
  input_tokens: '10',
  output_tokens: null,
  input_reported: 1,
  output_reported: 0,
  duration_reported: 2,
  avg_duration_ms: 1000,
  p95_duration_ms: 1900,
  costs: [{ amount: '0', unit: 'USD', known_calls: 1, avg_cost: '0' }],
}
function result(changes: Partial<UsageStatistics> = {}): UsageStatistics {
  return {
    since,
    until,
    timezone: 'Asia/Shanghai',
    summary: { ...metrics },
    total_groups: 1,
    trend: [
      { ...metrics, start: since, label: '2026-10-01T08:00:00+08:00', filters: { since, until } },
    ],
    groups: [{ ...metrics, key: channel, label: '渠道一', filters: { channel_id: channel } }],
    ...changes,
  }
}
function calls(n = 1) {
  return Array.from({ length: n }, (_, i) => ({
    id: i === 0 ? call : `11111111-1111-4111-8111-${String(i).padStart(12, '0')}`,
    channel_id: channel,
    channel_name: '渠道一',
    model_name: '模型一',
    provider_model: 'deepseek-flash',
    operation: 'text.generate',
    user_name: '管理员',
    status: 'succeeded',
    created_at: since,
    cost_amount: null,
    cost_check_status: 'pending',
    flow_context: null,
  }))
}
async function page(query: Record<string, string> = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/model-usage', component: UsageView }],
  })
  await router.push({ path: '/model-usage', query: { since, until, ...query } })
  await router.isReady()
  const wrapper = mount(UsageView, { global: { plugins: [router] } })
  await flushPromises()
  return { router, wrapper }
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})
it('keeps decimal precision available while shortening fee and average displays', () => {
  expect(compactMoney('0.000768666666666667')).toBe('0.00076866…')
  expect(compactMoney('192.851716')).toBe('192.851716')
  expect(compactMoney('0.000000000')).toBe('0')
  expect(compactMoney('-12.123456789')).toBe('-12.12345678…')
  expect(compactMoney('0.000000000000123456789')).toBe('1.234…e-13')
  expect(compactMoney('-0.0000000001')).toBe('-1e-10')
  expect(compactMoney('1.234E-10')).toBe('1.234E-10')
  expect(compactMoney('12345678901234567890.123456789')).toBe('12345678901234567890.12345678…')
  const amount = '0.000768666666666667'
  const wrapper = mount(MoneyAmount, { props: { amount, unit: 'USD' } })
  expect(wrapper.text()).toBe('0.00076866… USD')
  expect(wrapper.attributes('title')).toBe(`${amount} USD`)
})
it('keeps the selected name through slow, failed and unrelated option searches', async () => {
  authorize(['model_invocation:read'])
  const pending = deferred<unknown>()
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    const query = new URLSearchParams(path.split('?')[1]).get('q')
    if (query === 'slow') return pending.promise
    if (query === 'failed') return new Response('{}', { status: 500 })
    return { items: query === 'other' ? [] : [{ id: channel, label: '渠道一' }], has_more: false }
  })
  const wrapper = mount(StatisticsPicker, {
    props: {
      modelValue: channel,
      dimension: 'channel',
      label: '渠道筛选',
      filters: { since, until },
    },
  })
  await flushPromises()
  const select = wrapper.getComponent(ElSelect)
  await selectControl(wrapper).get('[role=combobox]').trigger('click')
  await flushPromises()
  const placeholder = () => wrapper.get('.el-select__placeholder').text()
  expect(placeholder()).toBe('渠道一')
  const before = paths.length
  await wrapper.setProps({ filters: { until, since, channel_id: channel } })
  await flushPromises()
  expect(paths).toHaveLength(before)
  select.props('remoteMethod')!('slow')
  await vi.waitFor(() => expect(paths.at(-1)).toContain('q=slow'))
  expect(placeholder()).toBe('渠道一')
  pending.resolve({ items: [], has_more: false })
  await flushPromises()
  expect(placeholder()).toBe('渠道一')
  for (const query of ['other', 'failed']) {
    select.props('remoteMethod')!(query)
    await vi.waitFor(() => expect(paths.at(-1)).toContain(`q=${query}`))
    await flushPromises()
    expect(placeholder()).toBe('渠道一')
  }
})
it('resolves a saved name independently of the open menu and selects IDs rather than typed names', async () => {
  authorize(['model_invocation:read'])
  const label = deferred<unknown>()
  fakeApi((path) => {
    const query = new URLSearchParams(path.split('?')[1]).get('q')
    return query === channel
      ? label.promise
      : { items: [{ id: call, label: '另一个渠道' }], has_more: false }
  })
  const wrapper = mount(StatisticsPicker, {
    props: {
      modelValue: channel,
      dimension: 'channel',
      label: '渠道筛选',
      filters: { since, until },
    },
  })
  await selectControl(wrapper).get('[role=combobox]').trigger('click')
  await flushPromises()
  expect(wrapper.get('.el-select__placeholder').text()).toBe('正在读取名称…')
  label.resolve({ items: [{ id: channel, label: '已保存渠道' }], has_more: false })
  await flushPromises()
  expect(wrapper.get('.el-select__placeholder').text()).toBe('已保存渠道')
  const select = wrapper.getComponent(ElSelect)
  select.props('remoteMethod')!('另一个渠道')
  await vi.waitFor(() =>
    expect(
      wrapper.findAllComponents(ElOption).filter((item) => item.props('label') === '另一个渠道'),
    ).toHaveLength(1),
  )
  expect(
    wrapper.findAllComponents(ElOption).some((item) => item.props('value') === '另一个渠道'),
  ).toBe(false)
  await chooseSelect(wrapper, call)
  expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual([call])
})
it('gives history readers independent analytics without management APIs or mixed units', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.startsWith('/model-invocations/statistics') ? result() : []
  })
  const { wrapper } = await page()
  expect(wrapper.text()).toContain('仅显示本人调用')
  expect(wrapper.text()).toContain('费用未知：1 次')
  expect(wrapper.text()).toContain('0 USD')
  expect(wrapper.text()).toContain('50.0%')
  expect(wrapper.text()).toContain('输入覆盖 1/2')
  expect(wrapper.text()).not.toContain('新建渠道')
  expect(wrapper.find('[aria-label=用户筛选]').exists()).toBe(false)
  expect(paths.every((path) => path.startsWith('/model-invocations/'))).toBe(true)
  expect(paths.filter((path) => path.includes('/statistics'))).toHaveLength(1)
})
it('preserves submitted bounds and filters through drill-down, pagination, details and browser back', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    if (path.startsWith('/model-invocations/statistics')) return result()
    if (path.startsWith('/model-invocations/options')) return { items: [], has_more: false }
    if (path.startsWith(`/model-invocations/${call}`))
      return { ...calls()[0], cost_source: 'unknown', usage: {} }
    return calls(50)
  })
  const { wrapper, router } = await page({ source: 'flow', model_role: 'writer' })
  await button(wrapper, '渠道一').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toMatchObject({
    tab: 'details',
    channel_id: channel,
    source: 'flow',
    model_role: 'writer',
    since,
    until,
  })
  const details = wrapper.get('[aria-label=调用明细]')
  await details
    .findAll('button')
    .find((node) => node.text() === '下一页')!
    .trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBe('1')
  expect(
    paths.some((path) => path.startsWith('/model-invocations?') && path.includes('offset=50')),
  ).toBe(true)
  await details
    .findAll('button')
    .find((node) => node.text() === '查看调用')!
    .trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toMatchObject({
    page: '1',
    channel_id: channel,
    invocation: call,
  })
  await wrapper.get('[role=dialog] button[aria-label="关闭对话框"]').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBe('1')
  router.back()
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBeUndefined()
  for (const path of paths.filter((path) => path.startsWith('/model-invocations?'))) {
    const params = new URLSearchParams(path.split('?')[1])
    expect(params.get('since')).toBe(since)
    expect(params.get('until')).toBe(until)
    expect(params.get('source')).toBe('flow')
  }
})
it('persists grouping, unit-specific ranking and bucket choices in links', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? result() : []
  })
  const { wrapper, router } = await page({ tab: 'analysis' })
  await chooseSelect(wrapper, 'model', '分析维度')
  await chooseSelect(wrapper, 'USD', '分组排序')
  await chooseSelect(wrapper, 'month', '时间粒度')
  await flushPromises()
  expect(router.currentRoute.value.query).toMatchObject({
    group_by: 'model',
    order_by: 'cost',
    sort_unit: 'USD',
    bucket: 'month',
  })
  const params = new URLSearchParams(
    paths
      .filter((path) => path.includes('/statistics'))
      .at(-1)!
      .split('?')[1],
  )
  expect(params.get('group_by')).toBe('model')
  expect(params.get('sort_unit')).toBe('USD')
  expect(params.get('bucket')).toBe('month')
})
it('ignores late statistics after filters change and after read-all is removed', async () => {
  authorize(['model_invocation:read', 'model_invocation:read_all'])
  const pending = deferred<UsageStatistics>()
  let count = 0
  fakeApi((path) => {
    if (!path.includes('/statistics')) return { items: [], has_more: false }
    count++
    return count === 1 ? pending.promise : result({ summary: { ...metrics, calls: 7 } })
  })
  const { wrapper, router } = await page()
  await router.push({ path: '/model-usage', query: { since, until, channel_id: channel } })
  await flushPromises()
  pending.resolve(result({ summary: { ...metrics, calls: 999 } }))
  await flushPromises()
  expect(wrapper.text()).not.toContain('999')
  const before = count
  identity.value = { ...identity.value!, permissions: ['model_invocation:read'] }
  await flushPromises()
  expect(count).toBeGreaterThan(before)
  expect(wrapper.text()).toContain('仅显示本人调用')
})
it('does not query when permission is absent and clears values on revocation', async () => {
  authorize([])
  fakeApi(() => result())
  const { wrapper } = await page()
  expect(globalThis.fetch).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('没有调用记录与费用查看权限')
  authorize(['model_invocation:read'])
  await flushPromises()
  expect(wrapper.text()).toContain('0 USD')
  authorize([])
  await flushPromises()
  expect(wrapper.text()).not.toContain('0 USD')
})
it('refreshes only applied filters and has no automatic polling', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? result() : []
  })
  const { wrapper } = await page()
  await wrapper.get('input[placeholder="如 USD、credits"]').setValue('unapplied')
  paths.length = 0
  await button(wrapper, '刷新统计').trigger('click')
  await flushPromises()
  expect(paths).toHaveLength(1)
  expect(paths[0]).not.toContain('unapplied')
  expect(new URLSearchParams(paths[0]!.split('?')[1]).get('until')).toBe(until)
  vi.useFakeTimers()
  vi.advanceTimersByTime(60000)
  await flushPromises()
  expect(paths).toHaveLength(1)
})
it('uses the shared range for summary and detail and isolates a statistics failure from detail', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? new Response('{}', { status: 503 }) : calls()
  })
  const { wrapper } = await page({ tab: 'details', status: 'failed', cost_known: 'false' })
  expect(wrapper.text()).toContain('统计暂不可用')
  expect(wrapper.text()).toContain('模型一')
  for (const path of paths) {
    const params = new URLSearchParams(path.split('?')[1])
    expect(params.get('status')).toBe('failed')
    expect(params.get('cost_known')).toBe('false')
    expect(params.get('since')).toBe(since)
  }
})
it('searches scoped labels and keeps an off-page selection without querying configuration', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return { items: [{ id: channel, label: '渠道一' }], has_more: true }
  })
  const wrapper = mount(StatisticsPicker, {
    props: {
      modelValue: 'saved-off-page',
      label: '渠道筛选',
      dimension: 'channel',
      filters: { since, until },
    },
  })
  const control = selectControl(wrapper, '渠道筛选')
  await control.get('[role=combobox]').trigger('click')
  await flushPromises()
  expect(control.text()).toContain('saved-off-page')
  await vi.waitFor(() => expect(control.text()).toContain('渠道一'))
  await button(wrapper, '加载更多渠道筛选').trigger('click')
  await flushPromises()
  expect(paths.at(-1)).toContain('offset=1')
  expect(paths.every((path) => path.startsWith('/model-invocations/options?'))).toBe(true)
})

it('remounts an open invocation when read-all scope is withdrawn', async () => {
  authorize(['model_invocation:read', 'model_invocation:read_all'])
  let broad = true
  fakeApi((path) => {
    if (path.includes('/statistics')) return result()
    if (path.startsWith(`/model-invocations/${call}`))
      return broad
        ? { ...calls()[0], user_name: '受限的其他用户', cost_source: 'unknown', usage: {} }
        : new Response('{}', { status: 404 })
    return calls()
  })
  const { wrapper } = await page({ tab: 'details', invocation: call })
  expect(wrapper.text()).toContain('受限的其他用户')
  broad = false
  identity.value = { ...identity.value!, permissions: ['model_invocation:read'] }
  await flushPromises()
  expect(wrapper.text()).not.toContain('受限的其他用户')
})
it('clears cached facet names when the readable scope changes', async () => {
  authorize(['model_invocation:read', 'model_invocation:read_all'])
  let broad = true
  fakeApi(() => ({
    items: broad ? [{ id: channel, label: '其他用户的渠道' }] : [],
    has_more: false,
  }))
  const wrapper = mount(StatisticsPicker, {
    props: {
      modelValue: channel,
      dimension: 'channel',
      label: '渠道筛选',
      filters: { since, until },
    },
  })
  await flushPromises()
  await selectControl(wrapper, '渠道筛选').get('[role=combobox]').trigger('click')
  await vi.waitFor(() => expect(wrapper.text()).toContain('其他用户的渠道'))
  broad = false
  identity.value = { ...identity.value!, permissions: ['model_invocation:read'] }
  await flushPromises()
  expect(wrapper.text()).not.toContain('其他用户的渠道')
})

it('advances a submitted relative range on explicit refresh without applying or losing drafts', async () => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(until))
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? result() : []
  })
  const { wrapper, router } = await page({ range: '30' })
  await wrapper.get('input[placeholder="如 USD、credits"]').setValue('unapplied')
  paths.length = 0
  vi.setSystemTime(new Date(Date.parse(until) + 60000))
  await button(wrapper, '刷新统计').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.until).toBe(
    new Date(Date.parse(until) + 60000).toISOString(),
  )
  expect(router.currentRoute.value.query.cost_unit).toBeUndefined()
  expect(
    (wrapper.get('input[placeholder="如 USD、credits"]').element as HTMLInputElement).value,
  ).toBe('unapplied')
  expect(paths.filter((path) => path.includes('/statistics'))).toHaveLength(1)
})
it('rejects a manually entered invalid selector ID before issuing statistics requests', async () => {
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? result() : { items: [], has_more: false }
  })
  const { wrapper, router } = await page()
  const picker = wrapper
    .findAllComponents(StatisticsPicker)
    .find((item) => item.props('dimension') === 'channel')!
  picker.vm.$emit('update:modelValue', 'not-a-uuid')
  await flushPromises()
  paths.length = 0
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('完整 UUID')
  expect(router.currentRoute.value.query.channel_id).toBeUndefined()
  expect(paths).toEqual([])
})

it('uses Shanghai for shortcuts and datetime inputs without exposing a timezone control', async () => {
  const { statisticsRange, shanghaiInput, shanghaiDate } = await import('../views/usage/time')
  const now = new Date('2026-10-31T16:30:00Z') // Shanghai is already November 1.
  expect(statisticsRange('today', now)).toEqual({
    since: '2026-10-31T16:00:00.000Z',
    until: now.toISOString(),
  })
  expect(statisticsRange('month', now).since).toBe('2026-10-31T16:00:00.000Z')
  expect(shanghaiInput(now.toISOString())).toBe('2026-11-01T00:30:00')
  expect(shanghaiDate('2026-11-01T00:30:00').toISOString()).toBe(now.toISOString())
  authorize(['model_invocation:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return path.includes('/statistics') ? result() : []
  })
  const { wrapper } = await page()
  expect(wrapper.find('[aria-label="统计时区"]').exists()).toBe(false)
  expect(wrapper.text()).toContain('统一按上海时间')
  expect(paths.some((path) => new URLSearchParams(path.split('?')[1]).has('timezone'))).toBe(false)
})
