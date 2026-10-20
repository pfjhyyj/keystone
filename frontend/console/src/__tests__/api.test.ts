import { expect, it } from 'vitest'
import { getReadiness, getWorkerStatus } from '../api/system'
import { fakeApi } from './support'

it.each([
  { status: 'ready', http: 200, database: 'ok', schema: 'ok', storage: 'ok' },
  {
    status: 'unavailable',
    http: 503,
    database: 'unconfigured',
    schema: 'unavailable',
    storage: 'unconfigured',
  },
])('reads current readiness when $status', async ({ status, http, database, schema, storage }) => {
  const value = {
    status,
    checks: { database, schema, storage },
    storage_sources: {},
    flow_enabled: false,
  }
  fakeApi(() => new Response(JSON.stringify(value), { status: http }))
  expect(await getReadiness()).toEqual(value)
})

it.each([undefined, true])('rejects invalid storage status %s', async (storage) => {
  fakeApi(() => ({
    status: 'ready',
    checks: { database: 'ok', schema: 'ok', storage },
    flow_enabled: false,
  }))
  await expect(getReadiness()).rejects.toThrow('无效的就绪响应')
})

it.each(['ready', 'unready', 'offline', 'unknown'])(
  'reads independent Worker status %s',
  async (status) => {
    const value = { status, last_heartbeat_at: '2026-10-19T16:00:00Z' }
    fakeApi(() => value)
    expect(await getWorkerStatus()).toEqual(value)
  },
)

it.each([
  { status: 'ready', last_heartbeat_at: null },
  { status: 'ready', last_heartbeat_at: 'not-a-date' },
  { status: 'healthy', last_heartbeat_at: null },
])('rejects invalid Worker status %#', async (value) => {
  fakeApi(() => value)
  await expect(getWorkerStatus()).rejects.toThrow('无效的 Worker 状态响应')
})
