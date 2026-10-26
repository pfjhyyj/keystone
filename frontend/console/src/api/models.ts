import { request } from './client'
export interface Channel {
  id: string
  name: string
  provider: string
  api_base: string
  has_api_key: boolean
  has_billing_api_key: boolean
  upstream_account_ref: string | null
  is_active: boolean
  balance_snapshot: {
    scope: string
    unlimited?: boolean
    items: { kind: string; amount: string; unit: string }[]
    token_items?: { kind: string; amount: string; unit: string }[]
  } | null
  balance_check_status: string
  balance_checked_at: string | null
  balance_last_attempt_at: string | null
}
export type ModelDelivery = 'sync' | 'async'
export type ModelOperation =
  | 'text.generate'
  | 'text.structured'
  | 'text.embed'
  | 'image.understand'
  | 'image.generate'
  | 'image.edit'
export const operationLabels: Record<ModelOperation, string> = {
  'text.generate': '文本生成',
  'text.structured': '结构化文本',
  'text.embed': '文本向量',
  'image.understand': '图片理解',
  'image.generate': '图片生成',
  'image.edit': '图片编辑',
}
export interface ModelEntry {
  id: string
  key: string
  name?: string
  channel_id: string
  provider_model: string
  operations: ModelOperation[]
  is_active: boolean
}
export interface CatalogModel {
  key: string
  name?: string
  operations: ModelOperation[]
  image_parameters?: ('aspect_ratio' | 'quality')[]
  max_reference_images?: number
  max_input_bytes?: number
}
export interface MediaResult {
  invocation_id: string
  content?: string
  files?: { file_id: string }[]
}
export interface Invocation {
  channel_name?: string
  model_name?: string
  model_key?: string
  user_name?: string | null
  id: string
  operation: ModelOperation
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
  cost_check_status?: string
  cost_check_reason?: string | null
  cost_check_rounds?: number
  cost_check_errors?: number
  cost_checked_at?: string | null
  cost_next_check_at?: string | null
  cost_deadline_at?: string | null
  created_at: string
  finished_at: string | null
  error_code: string | null
  usage: Record<string, unknown> | null
  flow_context: Record<string, unknown> | null
}
export interface Costs {
  cost_checks?: Record<string, number>
  attempts_by_status: Record<string, number>
  unknown_cost_attempts: number
  known_costs: { channel_id: string; unit: string; amount: string; attempts: number }[]
}
export interface ModelOptions {
  providers: string[]
  balance_providers: string[]
  default_api_bases: Record<string, string>
}
export const modelApi = {
  channels: (offset: number) => request<Channel[]>(`/model-channels?offset=${offset}&limit=50`),
  entries: (offset: number) => request<ModelEntry[]>(`/model-entries?offset=${offset}&limit=50`),
  catalog: (
    offset: number,
    operation: ModelOperation = 'text.generate',
    delivery: ModelDelivery = 'sync',
  ) =>
    request<{ items: CatalogModel[] }>(
      `/models?offset=${offset}&limit=50&operation=${encodeURIComponent(operation)}&delivery=${delivery}`,
    ),
  media: (operation: ModelOperation, body: unknown, signal: AbortSignal) => {
    const path = {
      'image.generate': '/models/images/generate',
      'image.edit': '/models/images/edit',
      'image.understand': '/models/vision',
    }[operation as 'image.generate' | 'image.edit' | 'image.understand']
    if (!path) throw new Error('不支持的图片操作')
    return request<MediaResult>(path, 'POST', body, true, signal, 905000)
  },
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
  newapi: 'NewAPI',
  openlux: 'Yunwu',
  yunwu: 'Yunwu',
  deepseek: 'DeepSeek',
  aliyun_bailian: '阿里云百炼',
}
export function channelProvider(row: Pick<Channel, 'provider' | 'api_base'>) {
  return row.provider === 'openlux' ||
    row.provider === 'yunwu' ||
    (row.provider === 'newapi' &&
      ['https://api.openlux.ai/v1', 'https://yunwu.ai/v1'].includes(row.api_base))
    ? 'yunwu'
    : row.provider
}
export const costCheckStates: Record<string, string> = {
  pending: '等待核对',
  confirmed: '费用已确认',
  blocked: '需要处理',
  unavailable: '无法查询',
  exhausted: '自动核对已结束',
}
export const money = (value: string) =>
  /^[+-]?\d+\.\d+$/.test(value) ? value.replace(/0+$/, '').replace(/\.$/, '') : value

// Display only: ledger values and detail views retain the original decimal string.
export function compactMoney(value: string) {
  const exact = money(value)
  const parts = /^(-?)(\d+)\.(\d+)$/.exec(exact)
  if (!parts || parts[3]!.length <= 8) return exact
  const sign = parts[1]!,
    integer = parts[2]!,
    fraction = parts[3]!
  const first = fraction.search(/[1-9]/)
  if (/^0+$/.test(integer) && first >= 8) {
    const digits = fraction.slice(first)
    return `${sign}${digits[0]}${digits.length > 1 ? `.${digits.slice(1, 4)}` : ''}${digits.length > 4 ? '…' : ''}e-${first + 1}`
  }
  return `${sign}${integer}.${fraction.slice(0, 8)}…`
}
