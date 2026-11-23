import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import * as confirmation from '../utils/confirm'
import AppSelect from '../components/AppSelect.vue'
import MessagingView from '../views/messaging/MessagingView.vue'
import { authorize, fakeApi } from './support'

const stamp = '2026-01-01T00:00:00Z'
const destination = {
  id: '22222222-2222-4222-8222-222222222222',
  key: 'ops.alerts',
  is_active: true,
}
const channel = {
  id: '11111111-1111-4111-8111-111111111111',
  name: '运维群',
  provider: 'feishu_webhook',
  app_id: null,
  is_active: true,
  has_credential: true,
  destination,
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
const app = {
  ...channel,
  id: '44444444-4444-4444-8444-444444444444',
  name: '飞书应用',
  provider: 'feishu_app',
  app_id: 'cli_a1b2c3',
  destination: null,
}
const target = {
  id: '55555555-5555-4555-8555-555555555555',
  key: 'ops.daily',
  channel_id: app.id,
  external_id: 'oc_daily',
  name: '日报群',
  is_active: true,
  created_at: stamp,
  updated_at: stamp,
}
// Plain inputs only: the provider radio buttons are inputs too.
const fields = (wrapper: VueWrapper) => wrapper.findAll('[role=dialog] input:not([type=radio])')
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
      return path.startsWith('/messaging/destinations') ? [] : [channel]
    })
    const wrapper = mount(MessagingView)
    await flushPromises()
    expect(wrapper.text()).toContain('已设置')
    expect(wrapper.text()).toContain('ops.alerts')
    expect(wrapper.text()).not.toContain('接收方')
    expect(wrapper.text()).not.toContain('发送目标')
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
    const inputs = fields(wrapper)
    await inputs[0]!.setValue('新群')
    await inputs[1]!.setValue('daily.report')
    await inputs[2]!.setValue('https://open.feishu.cn/open-apis/bot/v2/hook/abc')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[1]).toEqual([
      '/messaging/channels',
      'POST',
      {
        name: '新群',
        provider: 'feishu_webhook',
        credential: { url: 'https://open.feishu.cn/open-apis/bot/v2/hook/abc' },
        destination_key: 'daily.report',
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
      return path.startsWith('/messaging/destinations') ? [] : [channel]
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

  it('gives a channel saved without a key its destination on the next edit', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined, unknown][] = []
    fakeApi((path, init) => {
      if (init.method && init.method !== 'GET') {
        writes.push([path, init.method, JSON.parse(String(init.body))])
        return channel
      }
      return path.startsWith('/messaging/destinations') ? [] : [{ ...channel, destination: null }]
    })
    const wrapper = mount(MessagingView)
    await flushPromises()
    expect(wrapper.text()).toContain('未设置')
    expect(button(wrapper, '发送测试消息').attributes('disabled')).toBeDefined()
    await button(wrapper, '编辑渠道').trigger('click')
    await fields(wrapper)[1]!.setValue('ops.alerts')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes).toEqual([
      [`/messaging/channels/${channel.id}`, 'PATCH', { name: '运维群', is_active: true }],
      [
        '/messaging/destinations',
        'POST',
        { key: 'ops.alerts', channel_id: channel.id, name: '运维群' },
      ],
    ])
  })

  it('deletes a channel only after confirmation', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined][] = []
    let rows = [channel]
    fakeApi((path, init) => {
      if (init.method === 'DELETE') {
        writes.push([path, init.method])
        rows = []
        return null
      }
      return path.startsWith('/messaging/destinations') ? [] : rows
    })
    const confirm = vi.spyOn(confirmation, 'confirmAction').mockResolvedValueOnce(false)
    const wrapper = mount(MessagingView)
    await flushPromises()
    const remove = async () => {
      await wrapper.get('.row-more-trigger').trigger('click')
      await flushPromises()
      await button(new DOMWrapper(document.body) as unknown as VueWrapper, '删除渠道').trigger(
        'click',
      )
    }
    await remove()
    await flushPromises()
    expect(writes).toEqual([])
    expect(confirm.mock.calls[0]![0]).toContain('ops.alerts')

    confirm.mockResolvedValueOnce(true)
    await remove()
    await flushPromises()
    expect(writes).toEqual([[`/messaging/channels/${channel.id}`, 'DELETE']])
    expect(wrapper.text()).toContain('渠道已删除')
    expect(wrapper.text()).toContain('暂无消息渠道')
    confirm.mockRestore()
  })

  it('creates an app channel and adds a group picked from the bot chats', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined, unknown][] = []
    const reads: string[] = []
    fakeApi((path, init) => {
      if (init.method && init.method !== 'GET') {
        writes.push([path, init.method, JSON.parse(String(init.body))])
        return init.method === 'POST' && path === '/messaging/channels' ? app : target
      }
      reads.push(path)
      if (path.includes('/chats'))
        return {
          items: [
            { chat_id: 'oc_daily', name: '日报群', external: false },
            { chat_id: 'oc_ops', name: '运维群', external: false },
          ],
          page_token: null,
        }
      return path.startsWith('/messaging/destinations') ? [target] : [channel, app]
    })
    const wrapper = mount(MessagingView)
    await flushPromises()
    expect(wrapper.text()).toContain('cli_a1b2c3')
    expect(wrapper.text()).toContain('按发送目标配置')
    expect(reads).toContain('/messaging/destinations?provider=feishu_app&offset=0&limit=50')

    await button(wrapper, '新建渠道').trigger('click')
    await wrapper.get('[role=dialog] input[value=feishu_app]').setValue()
    const inputs = fields(wrapper)
    await inputs[0]!.setValue('飞书应用')
    await inputs[1]!.setValue('cli_a1b2c3')
    await inputs[2]!.setValue('app-secret')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toEqual([
      '/messaging/channels',
      'POST',
      {
        name: '飞书应用',
        provider: 'feishu_app',
        app_id: 'cli_a1b2c3',
        credential: { app_secret: 'app-secret' },
      },
    ])

    await button(wrapper, '添加群').trigger('click')
    await flushPromises()
    expect(reads).toContain(`/messaging/channels/${app.id}/chats`)
    const [, chatSelect] = wrapper.findAllComponents(AppSelect)
    chatSelect!.vm.$emit('update:modelValue', 'oc_ops')
    await flushPromises()
    await wrapper.get('[role=dialog] input[placeholder="ops.alerts"]').setValue('ops.alerts')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[1]).toEqual([
      '/messaging/destinations',
      'POST',
      { key: 'ops.alerts', channel_id: app.id, name: '运维群', external_id: 'oc_ops' },
    ])
  })
})
