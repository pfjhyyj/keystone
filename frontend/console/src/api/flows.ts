import { request } from './client'
import type { ModelDelivery, ModelOperation } from './models'
export interface ModelRole {
  key: string
  label: string
  operation: ModelOperation
  delivery?: ModelDelivery
}
export interface FlowDefinition {
  revision: string
  model_roles: ModelRole[]
  key: string
  name: string
  description: string
  business_managed?: boolean
  input_schema: Record<string, unknown>
}
export interface FlowRun {
  definition_revision: string | null
  model_bindings: Record<string, string>
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
export type DiagnosticSection = 'budget' | 'models' | 'files'
export interface DiagnosticPage<T> {
  available: boolean
  items: T[]
  truncated: boolean
  next_offset: number | null
}
export interface InvocationDiagnostic {
  invocation_id: string
  operation: ModelOperation
  status: string
  model_role: string | null
  node_key: string | null
  agent_key: string | null
  agent_call_id: string | null
  workflow_id: string | null
  step_id: number | null
  error_code: string | null
}
export interface BudgetCounters {
  model_requests: string
  tool_calls: string
  iterations: string
  file_count: string
  file_bytes: string
  input_tokens: string
  output_tokens: string
  unreported_text_requests: string
}
export interface FlowLimits extends Omit<BudgetCounters, 'unreported_text_requests'> {
  concurrent_operations: string
  temporary_bytes: string
}
export interface Diagnostics {
  run_id: string
  budget: { recorded: boolean; limits: FlowLimits | null; counters: BudgetCounters | null } | null
  models: DiagnosticPage<InvocationDiagnostic> | null
  files: DiagnosticPage<{ file_id: string; slot: string; invocation_id: string | null }> | null
}
export const flowApi = {
  diagnostics: (id: string, section: DiagnosticSection, offset = 0) =>
    request<Diagnostics>(
      `/flow-runs/${id}/diagnostics?${new URLSearchParams({ section, offset: String(offset), limit: '50' })}`,
    ),
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
