import { request, requestPage } from './client'

export type SessionState = 'starting' | 'ready' | 'stopping' | 'review' | 'stopped' | 'failed'
export interface EnvironmentSession {
  id: string
  purpose: 'interactive' | 'backup'
  state: SessionState
  can_stop: boolean
  restored_from?: string | null
}
// `id` is null for a declared environment that has not been used yet.
export interface Environment {
  id: string | null
  kind: 'chromium'
  key: string
  name: string
  session: EnvironmentSession | null
  backup_at: string | null
  backup_failed: boolean
  backup_enabled: boolean
}
// Opening, stopping and backing up wait for the provider; the API bounds each call.
const LONG = 220000
const path = (id: string) => `/environments/${encodeURIComponent(id)}`
const sessionPath = (id: string, sessionId: string) =>
  `${path(id)}/sessions/${encodeURIComponent(sessionId)}`
export const environmentsApi = {
  list: (offset: number) => request<Environment[]>(`/environments?offset=${offset}&limit=50`),
  listPage: (offset: number, limit: number) =>
    requestPage<Environment>('/environments', offset, limit),
  ensure: (kind: string, key: string) =>
    request<Environment>('/environments', 'POST', { kind, key }),
  open: (id: string) =>
    request<EnvironmentSession>(`${path(id)}/open`, 'POST', undefined, true, undefined, LONG),
  backup: (id: string) =>
    request<{ status: string }>(`${path(id)}/backup`, 'POST', undefined, true, undefined, LONG),
  stop: (id: string, sessionId: string) =>
    request<EnvironmentSession>(
      `${sessionPath(id, sessionId)}/stop`,
      'POST',
      undefined,
      true,
      undefined,
      LONG,
    ),
  check: (id: string, sessionId: string) =>
    request<EnvironmentSession>(`${sessionPath(id, sessionId)}/check`, 'POST'),
  screen: (id: string, sessionId: string) => {
    const url = new URL(`/api/v1${sessionPath(id, sessionId)}/screen`, location.origin)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    return url.href
  },
}
// The screen page for an environment key; business pages link here instead of embedding a viewer.
export const screenRoute = (kind: string, key: string, back?: string) => ({
  path: `/environments/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`,
  ...(back ? { query: { back } } : {}),
})
