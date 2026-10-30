import { flushPromises, mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ModelManager from '../views/models/ModelsView.vue'
import CloudBills from '../views/models/components/CloudBills.vue'
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
function report(period: string, offset = 0) {
  return {
    period,
    offset,
    limit: 50,
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

it.each(['aliyun_bailian', 'volcengine'])(
  'saves structured %s accounting credentials and clears input on close',
  async (provider) => {
    authorize(['model:manage'])
    const writes: Record<string, unknown>[] = []
    fakeApi((path, init) => {
      if (path === '/model-access/options')
        return { providers: ['openrouter', provider], balance_providers: [] }
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
      return { providers: ['volcengine'], balance_providers: [] }
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
    return report(query.get('period')!, Number(query.get('offset')))
  })
  const wrapper = mount(CloudBills, { props: { channel } })
  await wrapper.get('input').setValue('2026-09')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('含该云账户下其他产品和渠道')
  expect(wrapper.text()).toContain('火山方舟')
  expect(wrapper.text()).toContain('0.12345678')
  await wrapper
    .findAll('button')
    .find((b) => b.text() === '下一页账单')!
    .trigger('click')
  await flushPromises()
  expect(requests.map((q) => q.get('offset'))).toEqual(['0', '50'])
  expect(wrapper.text()).toContain('第 2 页')
  await wrapper.get('input').setValue('2026-08')
  expect(wrapper.text()).not.toContain('火山方舟')
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
