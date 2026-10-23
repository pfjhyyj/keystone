import { request } from './client'
export interface FlowDefinition {
  key: string
  name: string
  description: string
  input_schema: Record<string, unknown>
}
export interface FlowRun {
  id: string
  definition_key: string
  status: string
  actor_id: string
  created_at: string
  finished_at: string | null
  observed_at: string | null
  cancel_requested_at: string | null
  input: Record<string, unknown>
  result: Record<string, unknown> | null
  error_code: string | null
}
export interface Steps {
  items: {
    workflow_id: string
    step_id: number
    name: string
    started_at_epoch_ms: number | null
    completed_at_epoch_ms: number | null
  }[]
  available: boolean
  truncated: boolean
}
export const flowApi = {
  definitions: () =>
    request<{ idempotency_days: number; items: FlowDefinition[] }>('/flow-definitions'),
  runs: (offset: number, status: string) =>
    request<FlowRun[]>(
      `/flow-runs?${new URLSearchParams({ offset: String(offset), limit: '50', ...(status ? { status } : {}) })}`,
    ),
  detail: (id: string) => request<FlowRun>(`/flow-runs/${id}`),
}
export const flowStates: Record<string, string> = {
  pending: '等待执行',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
export const ongoing = (run: FlowRun) => ['pending', 'running'].includes(run.status)
