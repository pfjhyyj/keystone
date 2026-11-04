import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import OverviewView from '../views/overview/OverviewView.vue'
import { fakeApi } from './support'

it('shows backlog counts and preserves unknown observations', async () => {
  fakeApi((path) =>
    path === '/health'
      ? { status: 'ok', service: 'keystone-api', version: '0.1.0' }
      : path === '/ready'
        ? { status: 'ready', checks: { database: 'ok', schema: 'ok', storage: 'ok' } }
        : {
            status: 'offline',
            last_heartbeat_at: null,
            backlog: {
              flow: {
                status: 'ok',
                pending: 4,
                oldest_pending_at: '2026-10-19T16:00:00Z',
                oldest_pending_age_seconds: 120,
              },
              files: {
                status: 'unknown',
                pending: null,
                oldest_pending_at: null,
                oldest_pending_age_seconds: null,
              },
            },
          },
  )
  const wrapper = mount(OverviewView)
  await flushPromises()
  expect(wrapper.text()).toContain('4 项')
  expect(wrapper.text()).toContain('120 秒')
  expect(wrapper.text()).toContain('无法确认')
})

afterEach(() => vi.useRealTimers())

it('accepts an empty registration set and displays retired identities without reporting readiness', async () => {
  fakeApi((path) => {
    if (path === '/health') return { status: 'ok', service: 'keystone-api', version: '0.1.0' }
    if (path === '/ready')
      return {
        status: 'ready',
        checks: { database: 'ok', schema: 'ok', storage: 'ok' },
      }
    if (path === '/worker/status')
      return {
        status: 'offline',
        last_heartbeat_at: null,
        ready_workers: 0,
        expected_workers: 0,
        instances: [
          {
            worker_id: 'keystone-worker-02',
            instance_id: null,
            execution_version: null,
            status: 'offline',
            state: 'retired',
            expected: false,
            last_heartbeat_at: null,
          },
        ],
        maintenance: {},
      }
    throw new Error(path)
  })
  const wrapper = mount(OverviewView)
  await flushPromises()
  expect(wrapper.text()).toContain('0 / 0 个进程就绪')
  expect(wrapper.text()).toContain('已退役')
  expect(wrapper.text()).toContain('身份登记')
  wrapper.unmount()
})

it('polls Worker readiness and heartbeat independently and stops polling after leaving', async () => {
  vi.useFakeTimers()
  let worker = { status: 'unready', last_heartbeat_at: '2026-10-19T16:00:00Z' }
  let calls = 0
  fakeApi((path) => {
    calls++
    if (path === '/health') return { status: 'ok', service: 'keystone-api', version: '0.1.0' }
    if (path === '/ready')
      return {
        status: 'ready',
        checks: { database: 'ok', schema: 'ok', storage: 'ok' },
      }
    if (path === '/worker/status') return worker
    throw new Error(path)
  })
  const wrapper = mount(OverviewView)
  await flushPromises()
  const card = () =>
    wrapper.findAll('.status-card').find((item) => item.text().includes('Worker 服务'))!
  expect(card().text()).toContain('未就绪')
  expect(wrapper.text()).toContain('在线')
  expect(wrapper.get('time').attributes('datetime')).toBe(worker.last_heartbeat_at)
  worker = { status: 'ready', last_heartbeat_at: '2026-10-19T16:00:05Z' }
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(card().text()).toContain('就绪')
  expect(card().find('.is-warning').exists()).toBe(false)
  expect(wrapper.get('time').attributes('datetime')).toBe(worker.last_heartbeat_at)
  worker = { status: 'offline', last_heartbeat_at: worker.last_heartbeat_at }
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(card().text()).toContain('离线')
  expect(wrapper.text()).toContain('在线')
  wrapper.unmount()
  const finalCount = calls
  await vi.advanceTimersByTimeAsync(10000)
  expect(calls).toBe(finalCount)
})

it('does not report offline when the Worker status query fails', async () => {
  fakeApi((path) => {
    if (path === '/health') return { status: 'ok', service: 'keystone-api', version: '0.1.0' }
    if (path === '/ready')
      return {
        status: 'ready',
        checks: { database: 'ok', schema: 'ok', storage: 'ok' },
      }
    throw new Error('Connection lost')
  })
  const wrapper = mount(OverviewView)
  await flushPromises()
  const card = wrapper.findAll('.status-card').find((item) => item.text().includes('Worker 服务'))!
  expect(card.text()).toContain('无法确认')
  expect(wrapper.text()).toContain('在线')
  expect(wrapper.text()).toContain('无法读取')
})

it('shows a missing fixed Worker and shared maintenance separately from the live instance', async () => {
  fakeApi((path) => {
    if (path === '/health') return { status: 'ok', service: 'keystone-api', version: '0.1.0' }
    if (path === '/ready')
      return {
        status: 'ready',
        checks: { database: 'ok', schema: 'ok', storage: 'ok' },
      }
    if (path === '/worker/status')
      return {
        status: 'unready',
        last_heartbeat_at: '2026-10-19T16:00:00Z',
        ready_workers: 1,
        expected_workers: 2,
        instances: [
          {
            worker_id: 'keystone-worker',
            instance_id: 'live-instance',
            status: 'ready',
            state: 'ready',
            execution_version: 'version-one',
            last_heartbeat_at: '2026-10-19T16:00:00Z',
            expected: true,
          },
          {
            worker_id: 'keystone-worker-02',
            instance_id: null,
            status: 'offline',
            state: 'missing',
            execution_version: null,
            last_heartbeat_at: null,
            expected: true,
          },
        ],
        maintenance: { model_costs: { status: 'ready', last_checked_at: '2026-10-19T16:00:00Z' } },
      }
    throw new Error(path)
  })
  const wrapper = mount(OverviewView)
  await flushPromises()
  expect(wrapper.text()).toContain('1 / 2 个进程就绪')
  expect(wrapper.text()).toContain('keystone-worker-02')
  expect(wrapper.text()).toContain('尚未启动')
  expect(wrapper.text()).toContain('费用扫描：就绪')
  wrapper.unmount()
})
