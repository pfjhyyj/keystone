import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MessagingView from '../views/messaging/MessagingView.vue'
import { authorize, fakeApi } from './support'

const stamp = '2026-01-01T00:00:00Z'
const channel = {
  id: '11111111-1111-4111-8111-111111111111',
  name: '运维群',
  provider: 'feishu_webhook',
  is_active: true,
  has_credential: true,
  created_at: stamp,
  updated_at: stamp,
}
const destination = {
  id: '22222222-2222-4222-8222-222222222222',
  key: 'ops.alerts',
  channel_id: channel.id,
  external_id: null,
  name: '运维群',
  is_active: true,
  created_at: stamp,
  updated_at: stamp,
}
const delivery = {
  id: '33333333-3333-4333-8333-333333333333',
  channel_id: channel.id,
  destination_key: 'ops.alerts',
  source_module: 'messaging',
  source_type: 'destination_test',
  source_id: destination.id,
  kind: 'text',
  status: 'unknown',
  attempts: 1,
  error_code: 'send_unconfirmed',
  expires_at: null,
  created_at: stamp,
  sent_at: null,
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!

describe('messaging administration', () => {
  it('keeps the credential write-only and sends it only when replaced', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined, unknown][] = []
    fakeApi((path, init) => {
      if (init.method && init.method !== 'GET') {
        writes.push([path, init.method, init.body ? JSON.parse(String(init.body)) : null])
        return path.endsWith('/test') ? { delivery_id: delivery.id } : channel
      }
      return path.startsWith('/messaging/channels') ? [channel] : [destination]
    })
    const wrapper = mount(MessagingView)
    await flushPromises()
    expect(wrapper.text()).toContain('已设置')
    expect(wrapper.text()).not.toContain('投递记录')

    await button(wrapper, '编辑渠道').trigger('click')
    expect(wrapper.find('[role=dialog] input[type=password]').exists()).toBe(false)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toEqual([
      `/messaging/channels/${channel.id}`,
      'PATCH',
      { name: '运维群', is_active: true },
    ])

    await button(wrapper, '新建渠道').trigger('click')
    const inputs = wrapper.findAll('[role=dialog] input')
    await inputs[0]!.setValue('新群')
    await inputs[1]!.setValue('https://open.feishu.cn/open-apis/bot/v2/hook/abc')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[1]).toEqual([
      '/messaging/channels',
      'POST',
      {
        name: '新群',
        provider: 'feishu_webhook',
        credential: { url: 'https://open.feishu.cn/open-apis/bot/v2/hook/abc' },
      },
    ])
  })

  it('queues a test message and shows delivery facts without content', async () => {
    authorize(['messaging:manage', 'message_delivery:read'])
    const writes: string[] = []
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        writes.push(path)
        return { delivery_id: delivery.id }
      }
      if (path.startsWith('/messaging/deliveries')) return [delivery]
      return path.startsWith('/messaging/channels') ? [channel] : [destination]
    })
    const wrapper = mount(MessagingView)
    await flushPromises()
    await button(wrapper, '发送测试消息').trigger('click')
    await flushPromises()
    expect(writes).toEqual([`/messaging/destinations/${destination.id}/test`])
    expect(wrapper.text()).toContain('测试消息已登记')
    expect(wrapper.text()).toContain('结果未知')
    expect(wrapper.text()).toContain('send_unconfirmed')
  })
})
