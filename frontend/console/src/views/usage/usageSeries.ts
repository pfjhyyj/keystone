import {
  dimensions,
  feeSourceLabels,
  sourceLabels,
  usageApi,
  type Dimension,
  type UsageGroup,
  type UsagePoint,
} from '../../api/modelUsage'
import { callStates, costCheckStates, money, operationLabels } from '../../api/models'
import { estimateStates } from '../../api/pricing'
import { shanghaiInput } from './time'

// The server bounds per-group series pages; an export walks pages up to this many groups.
export const SERIES_PAGE = 20
export const EXPORT_GROUP_LIMIT = 1000

export function groupLabel(dimension: Dimension, row: { key: string | null; label: string }) {
  if (row.key === null) return row.label
  const known: Partial<Record<Dimension, Record<string, string>>> = {
    estimate_status: estimateStates,
    source: sourceLabels,
    cost_source: feeSourceLabels,
    operation: operationLabels,
    status: callStates,
    cost_status: costCheckStates,
  }
  return known[dimension]?.[row.key] ?? row.label
}

// A bucket without this unit is zero only when every call in it has a known cost.
export function knownAmount(point: UsageMetricsLike, unit: string) {
  const cost = point.costs.find((item) => item.unit === unit)
  if (cost) return cost.amount
  return point.unknown_cost_calls ? null : '0'
}
type UsageMetricsLike = Pick<UsagePoint, 'costs' | 'unknown_cost_calls'>

// Every bucket start in the range, on the fixed +08:00 reporting clock (no DST in Shanghai).
export function bucketStarts(since: string, until: string, bucket: string) {
  const offset = 8 * 3600000,
    end = new Date(until).getTime()
  const local = new Date(new Date(since).getTime() + offset)
  let cursor =
    bucket === 'month'
      ? Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), 1)
      : bucket === 'hour'
        ? Math.floor(local.getTime() / 3600000) * 3600000
        : Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate())
  const starts: number[] = []
  while (cursor - offset <= end && starts.length < 5000) {
    starts.push(cursor - offset)
    const next = new Date(cursor)
    cursor =
      bucket === 'month'
        ? Date.UTC(next.getUTCFullYear(), next.getUTCMonth() + 1, 1)
        : cursor + (bucket === 'hour' ? 3600000 : 86400000)
  }
  return starts
}

export async function seriesGroups(params: Record<string, string>, maxGroups = EXPORT_GROUP_LIMIT) {
  const groups: UsageGroup[] = []
  for (let offset = 0; ; offset += SERIES_PAGE) {
    const page = await usageApi.statistics({
      ...params,
      series: 'true',
      offset: String(offset),
      limit: String(SERIES_PAGE),
    })
    if (page.total_groups > maxGroups)
      throw new Error(`分组共 ${page.total_groups} 个，超过导出上限 ${maxGroups}，请缩小筛选范围`)
    groups.push(...page.groups)
    if (!page.groups.length || offset + SERIES_PAGE >= page.total_groups) return groups
  }
}

function cell(value: string | number | null) {
  let text = value === null ? '' : String(value)
  // Labels come from data; keep spreadsheet formulas inert.
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

// One row per group and reporting bucket; money stays per unit and unknown stays blank.
export function usageCsv(
  groups: UsageGroup[],
  dimension: Dimension,
  costBasis: 'actual' | 'estimated',
) {
  const units = [
    ...new Set(
      groups.flatMap((group) =>
        (group.trend ?? []).flatMap((point) => point.costs.map((c) => c.unit)),
      ),
    ),
  ].sort()
  const basis = costBasis === 'actual' ? '上游实扣' : '预估费用'
  const header = [
    '时段开始（上海时间）',
    dimensions[dimension],
    '分组键',
    '调用次数',
    '成功',
    '失败',
    '结果未知',
    '进行中',
    '费用已知调用',
    '费用未知调用',
    costBasis === 'actual' ? '待核对调用' : '等待计量调用',
    '输入 Token',
    '输入已报告调用',
    '输出 Token',
    '输出已报告调用',
    ...units.map((unit) => `${basis}已知金额（${unit}）`),
  ]
  const rows = groups.flatMap((group) =>
    (group.trend ?? []).map((point) => [
      shanghaiInput(point.start).replace('T', ' '),
      groupLabel(dimension, group),
      group.key ?? '',
      point.calls,
      point.succeeded,
      point.failed,
      point.unknown,
      point.running,
      point.known_calls,
      point.unknown_cost_calls,
      point.pending_calls,
      point.input_tokens,
      point.input_reported,
      point.output_tokens,
      point.output_reported,
      ...units.map((unit) => {
        const amount = knownAmount(point, unit)
        return amount === null ? null : money(amount)
      }),
    ]),
  )
  return '﻿' + [header, ...rows].map((row) => row.map(cell).join(',')).join('\r\n') + '\r\n'
}

export function downloadText(name: string, text: string, type = 'text/csv;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
