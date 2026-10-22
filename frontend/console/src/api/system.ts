import { sessionWork } from './client'

export interface Health {
  status: 'ok'
  service: string
  version: string
}

export interface Readiness {
  status: 'ready' | 'unavailable'
  checks: { database: string; schema: string; storage: string }
  flow_enabled: boolean
}

export interface WorkerStatus {
  status: 'ready' | 'unready' | 'offline' | 'unknown'
  last_heartbeat_at: string | null
  ready_workers?: number
  expected_workers?: number
  instances?: {
    worker_id: string
    instance_id: string | null
    status: 'ready' | 'unready' | 'offline' | 'unknown'
    state: string
    execution_version: string | null
    last_heartbeat_at: string | null
    expected: boolean
  }[]
  maintenance?: Record<string, { status: 'ready' | 'unready'; last_checked_at: string | null }>
}

async function getStatus(path: string, allowUnavailable = false): Promise<unknown> {
  const work = sessionWork(undefined, 6000)
  try {
    const response = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: work.signal,
    })
    work.assert()
    if (!response.ok && !(allowUnavailable && response.status === 503))
      throw new Error('服务暂时无法访问')
    const result = await response.json()
    work.assert()
    work.signal.throwIfAborted()
    return result
  } catch (error) {
    return work.rethrow(error)
  } finally {
    work.close()
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export async function getHealth(): Promise<Health> {
  const value = await getStatus('/api/v1/health')
  if (
    !record(value) ||
    value.status !== 'ok' ||
    value.service !== 'keystone-api' ||
    typeof value.version !== 'string'
  )
    throw new Error('无效的健康响应')
  return value as unknown as Health
}

export async function getReadiness(): Promise<Readiness> {
  const value = await getStatus('/api/v1/ready', true)
  if (
    !record(value) ||
    !['ready', 'unavailable'].includes(String(value.status)) ||
    !record(value.checks) ||
    typeof value.checks.database !== 'string' ||
    typeof value.checks.schema !== 'string' ||
    typeof value.checks.storage !== 'string' ||
    typeof value.flow_enabled !== 'boolean'
  ) {
    throw new Error('无效的就绪响应')
  }
  return value as unknown as Readiness
}

export async function getWorkerStatus(): Promise<WorkerStatus> {
  const value = await getStatus('/api/v1/worker/status')
  if (
    !record(value) ||
    !['ready', 'unready', 'offline', 'unknown'].includes(String(value.status)) ||
    !(
      value.last_heartbeat_at === null ||
      (typeof value.last_heartbeat_at === 'string' &&
        Number.isFinite(Date.parse(value.last_heartbeat_at)))
    ) ||
    (value.status === 'ready' && value.last_heartbeat_at === null)
  )
    throw new Error('无效的 Worker 状态响应')
  const timestamp = (v: unknown) =>
    v === null || (typeof v === 'string' && Number.isFinite(Date.parse(v)))
  if (
    (value.ready_workers !== undefined &&
      (!Number.isInteger(value.ready_workers) || Number(value.ready_workers) < 0)) ||
    (value.expected_workers !== undefined &&
      (!Number.isInteger(value.expected_workers) || Number(value.expected_workers) < 0)) ||
    (value.instances !== undefined &&
      (!Array.isArray(value.instances) ||
        value.instances.some(
          (item: unknown) =>
            !record(item) ||
            typeof item.worker_id !== 'string' ||
            typeof item.state !== 'string' ||
            !(item.instance_id === null || typeof item.instance_id === 'string') ||
            !(item.execution_version === null || typeof item.execution_version === 'string') ||
            !['ready', 'unready', 'offline', 'unknown'].includes(String(item.status)) ||
            !timestamp(item.last_heartbeat_at) ||
            typeof item.expected !== 'boolean',
        ))) ||
    (value.maintenance !== undefined &&
      (!record(value.maintenance) ||
        Object.values(value.maintenance).some(
          (item) =>
            !record(item) ||
            !['ready', 'unready'].includes(String(item.status)) ||
            !timestamp(item.last_checked_at),
        )))
  )
    throw new Error('无效的 Worker 状态响应')
  return value as unknown as WorkerStatus
}
