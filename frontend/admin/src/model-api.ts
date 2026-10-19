import { request } from './auth'
export interface Channel {
  id: string
  name: string
  provider: string
  api_base: string
  credential_ref: string
  billing_credential_ref: string | null
  upstream_account_ref: string | null
  is_active: boolean
  balance_snapshot: {
    scope: string
    items: { kind: string; amount: string; unit: string }[]
  } | null
  balance_check_status: string
  balance_checked_at: string | null
  balance_last_attempt_at: string | null
}
export interface ModelEntry {
  id: string
  key: string
  channel_id: string
  provider_model: string
  is_active: boolean
}
export interface Invocation {
  id: string
  user_id: string | null
  channel_id: string
  model_entry_id: string
  provider_model: string
  status: string
  upstream_request_id: string | null
  cost_amount: string | null
  cost_unit: string | null
  cost_source: string
  cost_note: string | null
  created_at: string
  finished_at: string | null
  error_code: string | null
  usage: Record<string, unknown> | null
  flow_context: Record<string, unknown> | null
}
export interface Costs {
  attempts_by_status: Record<string, number>
  unknown_cost_attempts: number
  known_costs: { channel_id: string; unit: string; amount: string; attempts: number }[]
}
export interface ModelOptions {
  mode: string
  credential_refs: string[]
}
export const modelApi = {
  channels: (offset: number) => request<Channel[]>(`/model-channels?offset=${offset}&limit=50`),
  entries: (offset: number) => request<ModelEntry[]>(`/model-entries?offset=${offset}&limit=50`),
  catalog: (offset: number) =>
    request<{ mode: string; items: { key: string }[] }>(`/models?offset=${offset}&limit=50`),
  calls: (offset: number, filters: Record<string, string>) =>
    request<Invocation[]>(
      `/model-invocations?${new URLSearchParams({ ...filters, offset: String(offset), limit: '50' })}`,
    ),
  summary: (filters: Record<string, string>) =>
    request<Costs>(`/model-invocations/summary?${new URLSearchParams(filters)}`),
}
export const callStates: Record<string, string> = {
  running: '处理中',
  succeeded: '成功',
  failed: '已知失败',
  unknown: '结果未知',
}
export const balanceStates: Record<string, string> = {
  never: '未查询',
  ok: '查询成功',
  error: '查询失败',
  unavailable: '不可用',
}
export const providers = {
  openrouter: 'OpenRouter',
  deepseek: 'DeepSeek',
  aliyun_bailian: '阿里云百炼',
}
export const money = (value: string) =>
  value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value
