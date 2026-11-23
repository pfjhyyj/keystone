import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'
import * as confirmation from '../utils/confirm'
import AppSelect from '../components/AppSelect.vue'
import { makeRouter } from '../router'
import ChannelsView from '../views/messaging/ChannelsView.vue'
import ChannelDetailView from '../views/messaging/ChannelDetailView.vue'
import RecordsView from '../views/messaging/RecordsView.vue'
import GroupsPanel from '../views/messaging/components/GroupsPanel.vue'
import MembersPanel from '../views/messaging/components/MembersPanel.vue'
import type { MessageChannel } from '../api/messaging'
import { identity } from '../stores/session'
import { authorize, fakeApi as fakeRequests } from './support'

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
  connection: { state: 'connected', workers: 2 },
  groups: 1,
  members: 3,
} as MessageChannel
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
const body = () => new DOMWrapper(document.body) as unknown as VueWrapper
// Route changes re-check the session; it keeps the permissions each test authorizes.
const fakeApi = (handler: Parameters<typeof fakeRequests>[0]) =>
  fakeRequests((path, init) => (path === '/auth/me' ? identity.value : handler(path, init)))
async function routed(path: string) {
  const router = makeRouter(createMemoryHistory('/console/'))
  await router.push(path)
  return router
}

describe('messaging channels', () => {
  it('keeps the credential write-only and sends it only when replaced', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined, unknown][] = []
    fakeApi((path, init) => {
      if (init.method && init.method !== 'GET') {
        writes.push([path, init.method, init.body ? JSON.parse(String(init.body)) : null])
        return channel
      }
      return [channel]
    })
    const router = await routed('/messaging')
    const wrapper = mount(ChannelsView, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('ops.alerts')
    expect(wrapper.text()).toContain('不支持')

    await button(wrapper, '编辑').trigger('click')
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

  it('tests a group bot from the list and gives a keyless one its key on edit', async () => {
    authorize(['messaging:manage'])
    const writes: [string, string | undefined, unknown][] = []
    let rows = [channel]
    fakeApi((path, init) => {
      if (init.method && init.method !== 'GET') {
        writes.push([path, init.method, init.body ? JSON.parse(String(init.body)) : null])
        return path.endsWith('/test') ? { delivery_id: delivery.id } : channel
      }
      return rows
    })
    const router = await routed('/messaging')
    let wrapper = mount(ChannelsView, { global: { plugins: [router] } })
    await flushPromises()
    await button(wrapper, '发送测试消息').trigger('click')
    await flushPromises()
    expect(writes[0]).toEqual([`/messaging/destinations/${destination.id}/test`, 'POST', null])
    expect(wrapper.text()).toContain('测试消息已登记')
    wrapper.unmount()

    writes.length = 0
    rows = [{ ...channel, destination: null as unknown as typeof destination }]
    wrapper = mount(ChannelsView, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('未设置')
    expect(button(wrapper, '发送测试消息').attributes('disabled')).toBeDefined()
    await button(wrapper, '编辑').trigger('click')
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
      return rows
    })
    const confirm = vi.spyOn(confirmation, 'confirmAction').mockResolvedValueOnce(false)
    const router = await routed('/messaging')
    const wrapper = mount(ChannelsView, { global: { plugins: [router] } })
    await flushPromises()
    const remove = async () => {
      await wrapper.get('.row-more-trigger').trigger('click')
      await flushPromises()
      await button(body(), '删除渠道').trigger('click')
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
    expect(wrapper.text()).toContain('还没有消息渠道')
    confirm.mockRestore()
  })

  it('shows apps with their connection and opens a new app on its groups', async () => {
    authorize(['messaging:manage'])
    const writes: [string, unknown][] = []
    const refused = {
      ...app,
      id: '88888888-8888-4888-8888-888888888888',
      name: '旧应用',
      connection: {
        state: 'refused',
        workers: 0,
        error_code: 'feishu_ws_1000040350',
        error_hint: '长连接数超过飞书上限',
      },
    }
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        writes.push([path, JSON.parse(String(init.body))])
        return app
      }
      return [channel, app, refused]
    })
    const router = await routed('/messaging')
    const wrapper = mount(ChannelsView, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('1 个群 · 3 位成员')
    expect(wrapper.text()).toContain('已连接')
    expect(wrapper.text()).toContain('连接被拒')
    expect(wrapper.text()).toContain('长连接数超过飞书上限')

    await button(wrapper, '新建渠道').trigger('click')
    await wrapper.get('[role=dialog] input[value=feishu_app]').setValue()
    const inputs = fields(wrapper)
    await inputs[0]!.setValue('飞书应用')
    await inputs[1]!.setValue('cli_a1b2c3')
    await inputs[2]!.setValue('app-secret')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes).toEqual([
      [
        '/messaging/channels',
        {
          name: '飞书应用',
          provider: 'feishu_app',
          app_id: 'cli_a1b2c3',
          credential: { app_secret: 'app-secret' },
        },
      ],
    ])
    expect(router.currentRoute.value.fullPath).toBe(`/messaging/channels/${app.id}?tab=groups`)
  })
})

describe('an app channel page', () => {
  it('shows the connection, then manages groups picked from the bot chats', async () => {
    authorize(['messaging:manage', 'message_delivery:read'])
    const writes: [string, unknown][] = []
    const reads: string[] = []
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        writes.push([path, init.body ? JSON.parse(String(init.body)) : null])
        return path.includes('/test') ? { delivery_id: delivery.id } : target
      }
      reads.push(path)
      if (path === `/messaging/channels/${app.id}`) return app
      if (path.includes('/chats'))
        return {
          items: [
            { chat_id: 'oc_daily', name: '日报群', external: false },
            { chat_id: 'oc_ops', name: '运维群', external: false },
          ],
          page_token: null,
        }
      return path.startsWith('/messaging/destinations') ? [target] : []
    })
    const router = await routed(`/messaging/channels/${app.id}`)
    const page = mount(ChannelDetailView, {
      props: { id: app.id },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(page.text()).toContain('cli_a1b2c3')
    expect(page.text()).toContain('已连接')
    expect(page.text()).toContain('2 个 Worker 在线')
    expect(page.text()).toContain('查看这个渠道的消息记录')
    page.unmount()

    const wrapper = mount(GroupsPanel, { props: { channel: app }, attachTo: document.body })
    await flushPromises()
    expect(reads).toContain(`/messaging/destinations?channel_id=${app.id}&offset=0&limit=50`)
    expect(wrapper.text()).toContain('ops.daily')
    await button(wrapper, '添加群').trigger('click')
    await flushPromises()
    expect(reads).toContain(`/messaging/channels/${app.id}/chats`)
    const [chatSelect] = wrapper.findAllComponents(AppSelect)
    chatSelect!.vm.$emit('update:modelValue', 'oc_ops')
    await flushPromises()
    await wrapper.get('[role=dialog] input[placeholder="ops.alerts"]').setValue('ops.alerts')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes[0]).toEqual([
      '/messaging/destinations',
      { key: 'ops.alerts', channel_id: app.id, name: '运维群', external_id: 'oc_ops' },
    ])

    await wrapper.get('.row-more-trigger').trigger('click')
    await flushPromises()
    await button(body(), '发送测试卡片').trigger('click')
    await flushPromises()
    expect(writes[1]).toEqual([`/messaging/destinations/${target.id}/test?card=true`, null])
    expect(wrapper.text()).toContain('确认收到')
    wrapper.unmount()
  })

  it('looks a Feishu account up with debounce and binds a member by the contact', async () => {
    authorize(['messaging:manage', 'user:read'])
    const writes: [string, unknown][] = []
    const alice = {
      id: '66666666-6666-4666-8666-666666666666',
      username: 'alice',
      display_name: 'Alice',
    }
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        const sent = JSON.parse(String(init.body))
        writes.push([path, sent])
        if (path.endsWith('/lookup'))
          return { external_user_id: 'ou_alice', name: '爱丽丝', name_hint: null, bound_to: null }
        return {}
      }
      if (path.startsWith('/users')) return [alice]
      return [
        {
          id: '77777777-7777-4777-8777-777777777777',
          channel_id: app.id,
          user: { id: alice.id, label: 'Alice' },
          external_user_id: 'ou_alice',
          created_at: stamp,
        },
      ]
    })
    const wrapper = mount(MembersPanel, { props: { channel: app } })
    await flushPromises()
    expect(wrapper.text()).toContain('ou_alice')

    vi.useFakeTimers()
    try {
      await button(wrapper, '绑定成员').trigger('click')
      await flushPromises()
      const [userSelect, contactSelect] = wrapper.findAllComponents(AppSelect)
      userSelect!.vm.$emit('update:modelValue', alice.id)
      const search = contactSelect!.vm.$attrs['remote-method'] as (q: string) => void
      // Partial input never reaches Feishu; rapid typing only looks up the last value.
      search('138')
      search('13800000000')
      await vi.advanceTimersByTimeAsync(100)
      search('13900000000')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(writes).toEqual([
        ['/messaging/accounts/lookup', { channel_id: app.id, mobile: '13900000000' }],
      ])
      expect(wrapper.text()).toContain('爱丽丝')
      contactSelect!.vm.$emit('update:modelValue', '13900000000')
      await flushPromises()
      await wrapper.get('[role=dialog] form').trigger('submit')
      await flushPromises()
      expect(writes.at(-1)).toEqual([
        '/messaging/accounts',
        { channel_id: app.id, user_id: alice.id, mobile: '13900000000' },
      ])
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('message records', () => {
  it('lists sent and received records without content and filters them', async () => {
    authorize(['messaging:manage', 'message_delivery:read'])
    const reads: string[] = []
    const inbound = {
      id: '66666666-6666-4666-8666-666666666666',
      channel_id: app.id,
      kind: 'card_action',
      delivery_id: delivery.id,
      action_key: 'ack',
      external_user_id: 'ou_stranger',
      actor: null,
      status: 'rejected',
      attempts: 1,
      error_code: 'message_actor_unbound',
      error_hint: '点击人的飞书账号没有绑定 Keystone 用户',
      received_at: stamp,
      processed_at: null,
    }
    fakeApi((path) => {
      reads.push(path)
      if (path.startsWith('/messaging/deliveries')) return [delivery]
      if (path.startsWith('/messaging/inbounds')) return [inbound]
      return [channel, app]
    })
    const router = await routed(`/messaging/records?channel=${channel.id}`)
    const wrapper = mount(RecordsView, { global: { plugins: [router] } })
    await flushPromises()
    expect(reads).toContain(`/messaging/deliveries?offset=0&limit=50&channel_id=${channel.id}`)
    const sent = wrapper.get('#pane-sent')
    expect(sent.text()).toContain('结果未知')
    expect(sent.text()).toContain('send_unconfirmed')
    expect(sent.text()).toContain('运维群')
    const received = wrapper.get('#pane-received')
    expect(received.text()).toContain('点击卡片按钮')
    expect(received.text()).toContain('已拒绝')
    expect(received.text()).toContain('没有绑定 Keystone 用户')

    const [, statusSelect] = wrapper.findAllComponents(AppSelect)
    statusSelect!.vm.$emit('update:modelValue', 'failed')
    statusSelect!.vm.$emit('change', 'failed')
    await flushPromises()
    expect(reads.at(-1)).toBe(
      `/messaging/deliveries?offset=0&limit=50&channel_id=${channel.id}&status=failed`,
    )
  })
})
