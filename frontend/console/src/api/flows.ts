import { failure, headers, request, requestPage, sessionWork } from './client'
import type { ModelOperation } from './models'

export interface FlowSnapshot {
  format_version?: number
  revision?: string
  file_fields?: string[]
  output_file_fields?: string[]
  model_roles?: Record<string, { operation: string; delivery: string }>
  timeout_seconds?: number
}

export interface FlowRun {
  definition_revision: string | null
  definition_snapshot?: FlowSnapshot | null
  model_bindings: Record<string, string>
  id: string
  definition_key: string
  definition_name?: string | null
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

export interface StepAttachment {
  label: string
  file_id: string
  role?: 'input' | 'output' | 'artifact'
}

export interface StepModelCall {
  operation: string
  model_role?: string | null
  node_key?: string | null
  invocation_id?: string | null
  duration_ms?: number | null
  input?: Record<string, unknown> | null
  output?: unknown
  error?: { code: string; message: string } | null
}

export interface StepAttempt {
  attempt: number
  label: string
  kind: 'original' | 'correction'
  started_at?: string | null
  finished_at?: string | null
  duration_ms?: number | null
  input: Record<string, unknown> | null
  output: unknown
  error: { code: string; message: string } | null
  attachments: StepAttachment[]
  model_calls?: StepModelCall[]
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
  total?: number
}

export interface InvocationDiagnostic {
  invocation_id: string
  operation: ModelOperation
  status: string
  provider_model?: string
  model_name?: string | null
  channel_name?: string | null
  duration_ms?: number | null
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

async function fetchBlob(path: string, signal: AbortSignal) {
  const work = sessionWork(signal, 30000)
  try {
    const response = await fetch(`/api/v1${path}`, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: headers(path),
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
}

export function extractFileIdsByPaths(data: unknown, paths: string[] | undefined): string[] {
  if (!data || typeof data !== 'object' || !paths?.length) return []
  const ids = new Set<string>()
  const walk = (value: unknown, segments: string[], index: number) => {
    if (value == null) return
    if (index === segments.length) {
      if (typeof value === 'string' && value.trim()) ids.add(value)
      else if (Array.isArray(value)) {
        for (const item of value) if (typeof item === 'string' && item.trim()) ids.add(item)
      }
      return
    }
    const segment = segments[index]
    if (segment === '*') {
      if (Array.isArray(value)) for (const item of value) walk(item, segments, index + 1)
      return
    }
    if (typeof value === 'object' && !Array.isArray(value)) {
      walk((value as Record<string, unknown>)[segment], segments, index + 1)
    }
  }
  for (const path of paths) walk(data, path.split('.'), 0)
  return [...ids]
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
  stepAttachment: (
    id: string,
    step: FlowStep,
    attempt: number,
    index: number,
    signal: AbortSignal,
  ) =>
    fetchBlob(
      `/flow-runs/${id}/step-attachment?${new URLSearchParams({
        ...stepQuery(step),
        attempt: String(attempt),
        index: String(index),
      })}`,
      signal,
    ),
  outputFile: (id: string, fileId: string, signal: AbortSignal) =>
    fetchBlob(`/flow-runs/${id}/output-file?${new URLSearchParams({ file_id: fileId })}`, signal),
  diagnostics: (id: string, section: DiagnosticSection, offset = 0, limit = 50) =>
    request<Diagnostics>(
      `/flow-runs/${id}/diagnostics?${new URLSearchParams({ section, offset: String(offset), limit: String(limit), include_total: 'true' })}`,
    ),
  runsPage: (offset: number, limit: number, status: string) =>
    requestPage<FlowRun>(
      status ? `/flow-runs?${new URLSearchParams({ status })}` : '/flow-runs',
      offset,
      limit,
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
