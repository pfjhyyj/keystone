import { failure, headers, request, sessionWork } from './client'
import type { ModelOperation } from './models'
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
export interface FlowStep {
  workflow_id: string
  step_id: number
  name: string
  started_at_epoch_ms: number | null
  completed_at_epoch_ms: number | null
}
export interface StepAttempt {
  attempt: number
  label: string
  kind: 'original' | 'correction'
  input: Record<string, unknown> | null
  output: unknown
  error: { code: string; message: string } | null
  attachments: { label: string; file_id: string }[]
}
export interface Steps {
  items: FlowStep[]
  available: boolean
  truncated: boolean
}
export type DiagnosticSection = 'models' | 'files'
export interface DiagnosticPage<T> {
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
export interface Diagnostics {
  run_id: string
  models: (DiagnosticPage<InvocationDiagnostic> & { available: boolean }) | null
  files: DiagnosticPage<{ file_id: string; slot: string; invocation_id: string | null }> | null
}
function stepQuery(step: FlowStep) {
  return { workflow_id: step.workflow_id, step_id: String(step.step_id) }
}
export const flowApi = {
  stepAttempts: (id: string, step: FlowStep, signal: AbortSignal) =>
    request<{ items: StepAttempt[]; truncated: boolean }>(
      `/flow-runs/${id}/step-attempts?${new URLSearchParams(stepQuery(step))}`,
      'GET',
      undefined,
      true,
      signal,
    ),
  async stepAttachment(
    id: string,
    step: FlowStep,
    attempt: number,
    index: number,
    signal: AbortSignal,
  ) {
    const work = sessionWork(signal, 15000)
    try {
      const query = new URLSearchParams({
        ...stepQuery(step),
        attempt: String(attempt),
        index: String(index),
      })
      const response = await fetch(`/api/v1/flow-runs/${id}/step-attachment?${query}`, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: headers(),
        signal: work.signal,
      })
      if (!response.ok)
        work.fail(failure(response.status, await response.json().catch(() => undefined)))
      const body = await response.blob()
      work.assert()
      work.signal.throwIfAborted()
      return body
    } catch (error) {
      return work.rethrow(error)
    } finally {
      work.close()
    }
  },
  diagnostics: (id: string, section: DiagnosticSection, offset = 0) =>
    request<Diagnostics>(
      `/flow-runs/${id}/diagnostics?${new URLSearchParams({ section, offset: String(offset), limit: '50' })}`,
    ),
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
