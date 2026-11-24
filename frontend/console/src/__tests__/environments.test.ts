import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'
import { makeRouter } from '../router'
import EnvironmentsView from '../views/environments/EnvironmentsView.vue'
import ScreenView from '../views/environments/ScreenView.vue'
import { identity } from '../stores/session'
import { authorize, fakeApi as fakeRequests } from './support'

const viewers = vi.hoisted(() => [] as FakeViewer[])
interface FakeViewer extends EventTarget {
  url: string
  pasted: string[]
  disconnected: boolean
}
vi.mock('@novnc/novnc', () => ({
  default: class extends EventTarget {
    pasted: string[] = []
    disconnected = false
    scaleViewport = false
    resizeSession = true
    // The extended clipboard lets the screen send any text.
    _clipboardServerCapabilitiesFormats = { 1: true }
    _clipboardServerCapabilitiesActions = { [1 << 27]: true }
    constructor(
      _target: HTMLElement,
      public url: string,
    ) {
      super()
      viewers.push(this as unknown as FakeViewer)
    }
    disconnect() {
      this.disconnected = true
    }
    focus() {}
    clipboardPasteFrom(text: string) {
      this.pasted.push(text)
    }
  },
}))

const environmentId = '11111111-1111-4111-8111-111111111111'
const sessionId = '22222222-2222-4222-8222-222222222222'
const ready = {
  id: environmentId,
  kind: 'chromium',
  key: 'notes.portal',
  name: '笔记门户',
  session: { id: sessionId, purpose: 'interactive', state: 'ready', can_stop: true },
  backup_at: null,
  backup_failed: false,
  backup_enabled: true,
}
const unused = { ...ready, id: null, key: 'default', name: '默认环境', session: null }
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)
const fakeApi = (handler: Parameters<typeof fakeRequests>[0]) =>
  fakeRequests((path, init) => (path === '/auth/me' ? identity.value : handler(path, init)))
async function routed(path: string) {
  const router = makeRouter(createMemoryHistory('/console/'))
  await router.push(path)
  return router
}

describe('environments', () => {
  it('lists my environments and offers stop and backup only with environment:stop', async () => {
    authorize(['environment:use'])
    const calls: string[] = []
    fakeApi((path, init) => {
      if (init.method === 'POST') calls.push(path)
      return [unused, { ...ready, key: 'notes.stopped', session: null }]
    })
    const router = await routed('/environments')
    const plain = mount(EnvironmentsView, { global: { plugins: [router] } })
    await flushPromises()
    expect(plain.text()).toContain('默认环境')
    expect(plain.text()).toContain('未启动')
    expect(button(plain, '备份')).toBeUndefined()
    plain.unmount()

    authorize(['environment:use', 'environment:stop'])
    const wrapper = mount(EnvironmentsView, { global: { plugins: [router] } })
    await flushPromises()
    // The unused default has no ID yet, so only the used environment can be backed up.
    await button(wrapper, '备份')!.trigger('click')
    await flushPromises()
    expect(calls).toEqual([`/environments/${environmentId}/backup`])
    expect(wrapper.find('a[href="/console/environments/chromium/default"]').exists()).toBe(true)
  })

  it('connects a ready session and sends pasted text through the screen', async () => {
    authorize(['environment:use'])
    viewers.length = 0
    fakeApi(() => [ready])
    const router = await routed('/environments/chromium/notes.portal?back=/messaging')
    const wrapper = mount(ScreenView, {
      props: { kind: 'chromium', envKey: 'notes.portal', back: '/messaging' },
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
    const [viewer] = viewers
    expect(viewer!.url).toBe(
      `ws://localhost:3000/api/v1/environments/${environmentId}/sessions/${sessionId}/screen`,
    )
    viewer!.dispatchEvent(new Event('connect'))
    await flushPromises()
    expect(button(wrapper, '结束会话')).toBeUndefined()

    await button(wrapper, '发送文本')!.trigger('click')
    await flushPromises()
    await wrapper.get('[role=dialog] textarea').setValue('订阅链接')
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(viewer!.pasted).toEqual(['订阅链接'])
    expect(wrapper.text()).toContain('文本已发送')

    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await button(wrapper, '返回业务页面')!.trigger('click')
    expect(push).toHaveBeenCalledWith('/messaging')
    // Leaving the page releases the screen so the next segment can take control.
    wrapper.unmount()
    expect(viewer!.disconnected).toBe(true)
  })

  it('creates the environment on first open and ignores an outside back link', async () => {
    authorize(['environment:use'])
    viewers.length = 0
    const posts: string[] = []
    let opened = false
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        posts.push(path)
        if (path === '/environments') return { ...ready, session: null }
        opened = true
        return ready.session
      }
      return opened ? [ready] : [unused]
    })
    const router = await routed('/environments/chromium/notes.portal')
    const wrapper = mount(ScreenView, {
      props: { kind: 'chromium', envKey: 'notes.portal', back: '//evil.test/' },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(viewers).toHaveLength(0)
    expect(button(wrapper, '返回业务页面')).toBeUndefined()

    await button(wrapper, '打开')!.trigger('click')
    await flushPromises()
    expect(posts).toEqual(['/environments', `/environments/${environmentId}/open`])
    expect(viewers).toHaveLength(1)
  })
})
