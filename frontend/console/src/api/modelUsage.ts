import { request } from './client'

export const dimensions = {
  channel: '渠道 / 站点',
  model: '业务模型',
  upstream_model: '上游模型',
  user: '用户 / 系统',
  source: '调用来源',
  flow_run: 'Flow 运行',
  node: '流程步骤',
  model_role: '模型角色',
  agent: 'Agent',
  operation: '操作类型',
  status: '调用结果',
  cost_status: '查费状态',
  cost_source: '费用来源',
  estimate_status: '预估状态',
  error: '错误码',
} as const
export type Dimension = keyof typeof dimensions
export const dimensionFilters: Record<Dimension, string> = {
  channel: 'channel_id',
  model: 'model_entry_id',
  upstream_model: 'provider_model',
  user: 'user_id',
  source: 'source',
  flow_run: 'flow_run_id',
  node: 'node_key',
  model_role: 'model_role',
  agent: 'agent_key',
  operation: 'operation',
  status: 'status',
  cost_status: 'cost_status',
  cost_source: 'cost_source',
  estimate_status: 'estimate_status',
  error: 'error_code',
}
export const sourceLabels: Record<string, string> = {
  direct: '普通调用',
  flow: 'Flow',
  agent: 'Agent',
}
export const feeSourceLabels: Record<string, string> = {
  response: '响应报告',
  query: '账务查询',
  unknown: '未知',
}
export const filterKeys = [
  'cost_basis',
  'estimate_status',
  'since',
  'until',
  'channel_id',
  'model_entry_id',
  'provider_model',
  'user_id',
  'source',
  'flow_run_id',
  'node_key',
  'model_role',
  'agent_key',
  'agent_call_id',
  'operation',
  'status',
  'cost_status',
  'cost_source',
  'cost_known',
  'cost_unit',
  'error_code',
  'missing',
] as const
export interface UsageCost {
  unit: string
  amount: string
  known_calls: number
  avg_cost: string
}
export interface UsageMetrics {
  estimate_coverage?: Record<string, number>
  calls: number
  succeeded: number
  failed: number
  unknown: number
  running: number
  known_calls: number
  unknown_cost_calls: number
  pending_calls: number
  input_tokens: string | null
  output_tokens: string | null
  input_reported: number
  output_reported: number
  duration_reported: number
  avg_duration_ms: number | null
  p95_duration_ms: number | null
  costs: UsageCost[]
}
export interface UsageGroup extends UsageMetrics {
  key: string | null
  label: string
  filters: Record<string, string>
}
export interface UsagePoint extends UsageMetrics {
  start: string
  label: string
  filters: Record<string, string>
}
export interface UsageStatistics {
  cost_basis?: 'estimated' | 'actual'
  since: string
  until: string
  timezone: string
  summary: UsageMetrics
  trend: UsagePoint[]
  groups: UsageGroup[]
  total_groups: number
}
export interface UsageOption {
  id: string
  label: string
}
export const usageApi = {
  statistics: (filters: Record<string, string>) =>
    request<UsageStatistics>(`/model-invocations/statistics?${new URLSearchParams(filters)}`),
  options: (dimension: Dimension, filters: Record<string, string>, q: string, offset: number) =>
    request<{ items: UsageOption[]; has_more: boolean }>(
      `/model-invocations/options?${new URLSearchParams({ ...filters, dimension, q, offset: String(offset), limit: '50' })}`,
    ),
}
export const percent = (part: number, whole: number) =>
  whole ? `${((part / whole) * 100).toFixed(1)}%` : '—'
export const duration = (value: number | null) =>
  value === null ? '未报告' : `${(value / 1000).toFixed(2)} 秒`
