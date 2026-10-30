import { request } from './client'
import type { ModelDelivery, ModelOperation } from './models'

export interface PriceItem {
  id: string
  quantity: string
  rate: string
  per: number
}
export interface PriceLeaf {
  kind: 'price'
  id: string
  input_partition: string
  output_partition: string
  items: PriceItem[]
  factor: string
}
export interface PriceBranch {
  kind: 'branch'
  when: Record<string, unknown>
  then: PriceNode
  else: PriceNode
}
export type PriceNode = PriceLeaf | PriceBranch
export interface PriceRule {
  format: 1
  currency: string
  root: PriceNode
  time_basis?: { source: string; timezone: string; calendar: string } | null
}
export interface PriceSource {
  label: string
  version: string
  url?: string | null
  checked_at?: string | null
}
export interface OperationPrice {
  operation: ModelOperation
  delivery: ModelDelivery
  rule: PriceRule
  source: PriceSource
}
export interface PricingConfig {
  mode: 'inherit' | 'override' | 'disabled'
  rules: OperationPrice[]
}
export interface PricingTarget {
  operation: ModelOperation
  delivery: ModelDelivery
  reference: OperationPrice | null
  profile: {
    id: string
    quantities: Record<string, 'token' | 'image' | 'request'>
    context_fields: Record<string, 'string' | 'integer'>
    input_partitions: Record<string, string[]>
    output_partitions: Record<string, string[]>
    context_enums?: Record<string, (string | number)[]>
    structural_zero?: string[]
  }
}
export interface PricingView {
  revision: number
  config: PricingConfig
  binding_valid: boolean
  operations: PricingTarget[]
}
export interface EstimateResult {
  status: string
  amount: string | null
  currency: string | null
  reason: string | null
  detail: {
    leaf?: string
    factor?: string
    missing?: string[]
    rule_hash?: string
    facts_hash?: string
    time_basis?: Record<string, unknown> | null
    items?: {
      id: string
      quantity: string
      value: number | null
      unit: string
      rate: string
      per: number
      amount: string | null
      source: string | null
    }[]
  }
}
export const estimateStates: Record<string, string> = {
  pending: '等待原调用计量',
  estimated: '已预估',
  partial: '计量不完整',
  unavailable: '无法预估',
  invalid: '规则或计量异常',
}
export const estimateReasons: Record<string, string> = {
  pricing_calendar_excluded: '调用日期不在此价格日历内',
  price_missing: '未配置此目标的价格',
  pricing_disabled: '已关闭预估',
  pricing_binding_changed: '价格绑定已变化，请重新配置',
  legacy_no_snapshot: '历史调用没有价格快照',
  metric_missing: '缺少计费用量',
  condition_missing: '缺少价格选档条件',
  meter_conflict: '上游计量冲突',
  price_overflow: '金额超出计算范围',
  pricing_invalid: '规则或快照无法计算',
  profile_version_unsupported: '不支持此计量规范版本',
}
export const quantityLabels: Record<string, string> = {
  'usage.input_cache_write_tokens': '缓存写入',
  'usage.input_total_tokens': '全部输入',
  'usage.input_regular_tokens': '普通输入（排除缓存）',
  'usage.input_uncached_tokens': '未命中缓存的输入（含写入）',
  'usage.input_cache_read_tokens': '缓存读取',
  'usage.output_total_tokens': '全部输出（含推理）',
  'usage.output_regular_tokens': '普通输出（排除推理）',
  'usage.reasoning_tokens': '推理输出',
  'usage.input_text_tokens': '文本输入',
  'usage.input_image_tokens': '图片输入',
  'usage.output_text_tokens': '文本输出',
  'usage.output_image_tokens': '图片输出',
  'usage.generated_images': '实际生成图片',
  'events.upstream_success_count': '原请求成功次数',
}
export const pricingApi = {
  get: (id: string) => request<PricingView>(`/model-entries/${id}/pricing`),
  save: (id: string, expected_revision: number, config: PricingConfig) =>
    request<PricingView>(`/model-entries/${id}/pricing`, 'PUT', { expected_revision, config }),
  preview: (id: string, body: unknown) =>
    request<EstimateResult>(`/model-entries/${id}/pricing/preview`, 'POST', body),
}
