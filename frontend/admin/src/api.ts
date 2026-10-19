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

async function getStatus(path: string, allowUnavailable = false): Promise<unknown> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.timeout(6000),
  })
  if (!response.ok && !(allowUnavailable && response.status === 503)) {
    throw new Error('服务暂时无法访问')
  }
  return response.json()
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
