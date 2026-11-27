import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it } from 'vitest'
import ModelManager from '../views/models/ModelsView.vue'
import { modelOptionsFixture } from './model-options.fixture'
import CloudBills from '../views/models/components/CloudBills.vue'
import ListPager from '../components/ListPager.vue'
import { authorize, chooseSelect, deferred, fakeApi } from './support'

const channel = {
  id: '22222222-2222-4222-8222-222222222222',
  name: 'Cloud',
  provider: 'volcengine',
  api_base: 'https://ark.cn-beijing.volces.com/api/v3',
  has_api_key: true,
  has_billing_api_key: true,
  upstream_account_ref: null,
  is_active: true,
  balance_snapshot: null,
  balance_check_status: 'never',
  balance_checked_at: null,
  balance_last_attempt_at: null,
}
function report(period: string, offset = 0, limit = 50) {
  return {
    period,
    offset,
    limit,
    scope: 'cloud_account',
    fetched_at: '2026-10-01T00:00:00Z',
    has_more: offset === 0,
    items: [
      {
        product: '火山方舟',
        product_code: 'ark',
        category: '按量付费',
        unit: 'CNY',
        amounts: { 应付金额: '0.123456789012345678' },
      },
    ],
  }
}

afterEach(() => localStorage.clear())

it.each(['aliyun_bailian', 'volcengine'])(
  'saves structured %s accounting credentials and clears input on close',
  async (provider) => {
    authorize(['model:manage'])
    const writes: Record<string, unknown>[] = []
    fakeApi((path, init) => {
      if (path === '/model-access/options')
        return modelOptionsFixture({ creatable: ['openrouter', provider], balance: [] })
      if (init.method === 'POST') {
        writes.push(JSON.parse(String(init.body)))
        return channel
      }
      return []
    })
    const wrapper = mount(ModelManager)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '新建渠道')!
      .trigger('click')
    await chooseSelect(wrapper, provider, '服务商')
    await wrapper.get('[role=dialog] input').setValue('Cloud')
    await wrapper.get('input[type=password]').setValue('inference-key')
    const passwords = wrapper.findAll('input[type=password]')
    expect(passwords).toHaveLength(4)
    await passwords[1]!.setValue('account-id')
    await passwords[2]!.setValue('account-secret')
    await passwords[3]!.setValue('sts-token')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toMatchObject({
      provider,
      api_base: modelOptionsFixture().channels.find((item) => item.id === provider)!
        .default_api_base,
      api_key: 'inference-key',
      billing_credentials: {
        access_key_id: 'account-id',
        access_key_secret: 'account-secret',
        security_token: 'sts-token',
      },
    })
    expect(writes[0]).not.toHaveProperty('billing_api_key')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '新建渠道')!
      .trigger('click')
    await chooseSelect(wrapper, provider, '服务商')
    expect(
      wrapper
        .findAll('input[type=password]')
        .every((item) => !(item.element as HTMLInputElement).value),
    ).toBe(true)
  },
)

it('keeps saved cloud credentials write-only and supports clearing them', async () => {
  authorize(['model:manage'])
  const writes: Record<string, unknown>[] = []
  fakeApi((path, init) => {
    if (path === '/model-access/options')
      return modelOptionsFixture({ creatable: ['volcengine'], balance: [] })
    if (init.method === 'PATCH') {
      writes.push(JSON.parse(String(init.body)))
      return channel
    }
    return path.startsWith('/model-channels') ? [channel] : []
  })
  const wrapper = mount(ModelManager)
  await flushPromises()
  await wrapper
    .findAll('button')
    .find((b) => b.text() === '编辑渠道')!
    .trigger('click')
  expect(
    wrapper
      .findAll('input[type=password]')
      .every((item) => !(item.element as HTMLInputElement).value),
  ).toBe(true)
  await wrapper.get('[role=dialog] input[type=checkbox]').setValue(true)
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(writes[0]).toMatchObject({ clear_billing_api_key: true })
  expect(writes[0]).not.toHaveProperty('billing_credentials')
})

it('loads bounded bill pages and clears results when the month changes', async () => {
  authorize(['model:manage'])
  const requests: URLSearchParams[] = []
  fakeApi((path) => {
    const query = new URLSearchParams(path.split('?')[1])
    requests.push(query)
    return report(query.get('period')!, Number(query.get('offset')), Number(query.get('limit')))
  })
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('input[type=month]').setValue('2026-09')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('含该云账户下其他产品和渠道')
  expect(wrapper.text()).toContain('火山方舟')
  expect(wrapper.text()).toContain('0.12345678')
  expect(wrapper.text()).toContain('上游未提供账单总条数')
  expect(wrapper.findComponent(ListPager).exists()).toBe(false)
  await wrapper
    .findAll('button')
    .find((b) => b.text() === '下一页账单')!
    .trigger('click')
  await flushPromises()
  expect(requests.map((q) => [q.get('offset'), q.get('limit')])).toEqual([
    ['0', '50'],
    ['50', '50'],
  ])
  expect(wrapper.text()).toContain('第 2 页')
  expect(
    wrapper
      .findAll('button')
      .find((b) => b.text() === '下一页账单')
      ?.attributes(),
  ).toHaveProperty('disabled')
  await chooseSelect(wrapper, '20', '每页账单条数')
  expect(requests.at(-1)?.get('offset')).toBe('0')
  expect(requests.at(-1)?.get('limit')).toBe('20')
  expect(wrapper.text()).toContain('第 1 页')
  await wrapper.get('input[type=month]').setValue('2026-08')
  expect(wrapper.text()).not.toContain('火山方舟')
  expect(wrapper.text()).not.toContain('上游未提供账单总条数')
  const reopened = mount(CloudBills, { props: { channel } })
  await reopened.get('form').trigger('submit')
  await flushPromises()
  expect(requests.at(-1)?.get('limit')).toBe('20')
})

it('uses the exact provider total for numbered bill pagination and never substitutes the item count', async () => {
  const requests: URLSearchParams[] = []
  fakeApi((path) => {
    const query = new URLSearchParams(path.split('?')[1])
    requests.push(query)
    const offset = Number(query.get('offset'))
    const limit = Number(query.get('limit'))
    return { ...report(query.get('period')!, offset, limit), total: 101 }
  })
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  const pager = () => wrapper.getComponent(ListPager)
  expect(pager().props()).toMatchObject({ total: 101, count: 1, size: 50, page: 0 })
  await pager().get('input[aria-label=跳转页码]').setValue('3')
  await pager().get('form').trigger('submit')
  await flushPromises()
  expect(requests.at(-1)?.get('offset')).toBe('100')
  expect(pager().props('page')).toBe(2)
  expect(wrapper.text()).not.toContain('上游未提供账单总条数')
  pager().vm.$emit('size-change', 100)
  await flushPromises()
  expect(requests.at(-1)?.get('offset')).toBe('0')
  expect(requests.at(-1)?.get('limit')).toBe('100')
  expect(pager().props()).toMatchObject({ total: 101, page: 0, size: 100 })
})

it('treats a reported zero bill count as exact', async () => {
  fakeApi(() => ({ ...report('2026-09'), items: [], has_more: false, total: 0 }))
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.getComponent(ListPager).props()).toMatchObject({ count: 0, total: 0 })
  expect(wrapper.text()).toContain('本页没有账单记录，不代表没有消费。')
  expect(wrapper.text()).not.toContain('上游未提供账单总条数')
})

it('does not offer jumps beyond the cloud bill query limit even when the total is exact', async () => {
  fakeApi(() => ({ ...report('2026-09'), total: 12_000 }))
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('共 12000 条账单；最多可查询至第 201 页。')
  expect(wrapper.find('input[aria-label=跳转页码]').exists()).toBe(false)
})

it('ignores an old bill response after the channel changes', async () => {
  authorize(['model:manage'])
  const pending = deferred<unknown>()
  fakeApi(() => pending.promise)
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('input').setValue('2026-09')
  await wrapper.get('form').trigger('submit')
  await wrapper.setProps({ channel: { ...channel, id: 'other-channel' } })
  pending.resolve(report('2026-09'))
  await flushPromises()
  expect(wrapper.text()).not.toContain('火山方舟')
  expect(wrapper.text()).not.toContain('查询中')
})

it('displays permission failures without presenting them as zero spending', async () => {
  authorize(['model:manage'])
  fakeApi(
    () =>
      new Response(JSON.stringify({ error: { code: 'denied', message: '缺少账务授权' } }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toBeTruthy()
  expect(wrapper.findAll('article')).toHaveLength(0)
})
