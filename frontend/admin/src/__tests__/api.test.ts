import { expect, it } from 'vitest'
import { getReadiness } from '../api/system'
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
