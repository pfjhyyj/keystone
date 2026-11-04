<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElForm, ElFormItem, ElRadioButton, ElRadioGroup } from 'element-plus'
import { operationLabels, money, type ModelEntry } from '../../../api/models'
import {
  pricingApi,
  estimateStates,
  estimateReasons,
  quantityLabels,
  type PricingView,
  type PricingTarget,
  type PricingConfig,
  type PriceRule,
  type PriceLeaf,
  type PriceNode,
  type OperationPrice,
  type EstimateResult,
  type PriceSource,
} from '../../../api/pricing'
import AppDialog from '../../../components/AppDialog.vue'
import AppSelect from '../../../components/AppSelect.vue'
import { useAction } from '../../../composables/useAction'

const props = defineProps<{ entry: ModelEntry }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const { busy, error, run } = useAction()
const view = ref<PricingView | null>(null)
const mode = ref<PricingConfig['mode']>('inherit'),
  selected = ref('')
type Template = 'token' | 'tier' | 'time' | 'image' | 'request' | 'json'
interface Editor {
  enabled: boolean
  template: Template
  currency: string
  input: string
  output: string
  rates: Record<string, string>
  /**
   * Tiers after the first; `after` is the exclusive lower bound (= previous tier's inclusive
   * upper bound). `rates` is the peak/only price, `idle` the idle-period price of that tier.
   */
  tiers: { after: number; rates: Record<string, string>; idle: Record<string, string> }[]
  /** Idle hours are [start, end) in local time; end < start crosses midnight. */
  time: {
    timezone: string
    start: number
    end: number
    tiered: boolean
    idle: Record<string, string>
  }
  factor: string
  json: string
  source: PriceSource
}
/**
 * Operations metered identically (same delivery and profile, e.g. the ChatCompletions
 * text/structured/vision trio) share one editor; saving writes one identical rule per member.
 * `key` is the first member's targetKey and also keys `editors`.
 */
interface Group {
  key: string
  family: string
  members: PricingTarget[]
}
const editors = reactive<Record<string, Editor>>({})
const groups = ref<Group[]>([])
/** Families shown split on load because their saved rules or references differ. */
const mismatched = ref<string[]>([])
const targetKey = (target: PricingTarget) => `${target.operation}:${target.delivery}`
const family = (target: PricingTarget) => `${target.delivery}:${JSON.stringify(target.profile)}`
const group = computed(() => groups.value.find((item) => item.key === selected.value))
const target = computed(() => group.value?.members[0])
const peers = computed(() => groups.value.filter((item) => item.family === group.value?.family))
const groupLabel = (item: Group) =>
  `${item.members.map((m) => operationLabels[m.operation]).join(' / ')} · ${
    item.members[0]!.delivery === 'async' ? '异步' : '同步'
  }`
const editor = computed(() => editors[selected.value])
const result = ref<EstimateResult | null>(null),
  sample = ref('')
const baseline = ref('')
const fingerprint = computed(() =>
  JSON.stringify([mode.value, editors, groups.value.map((item) => item.members.map(targetKey))]),
)
watch([fingerprint, sample, selected], () => {
  result.value = null
})
// Depth limit is 8 on the backend; a chain of n tiers is n-1 levels deep.
const MAX_TIERS = 6
const tierNames = ['一', '二', '三', '四', '五', '六']
const partitionLabels: Record<string, string> = {
  none: '不按 Token 收费',
  aggregate: '全部统一计价',
  cache: '普通输入与缓存读取分价',
  cache_read: '仅区分缓存命中／未命中',
  modality: '文本与图片分价',
  reasoning: '普通输出与推理分价',
}
const writesCache = (t: PricingTarget) =>
  !!t.profile.input_partitions.cache?.includes('usage.input_cache_write_tokens')
function partitionLabel(t: PricingTarget, key: string) {
  if (key === 'cache' && writesCache(t)) return '普通输入、缓存读取、缓存写入分价'
  return partitionLabels[key] ?? key
}
function partitionHelp(t: PricingTarget, key: string) {
  switch (key) {
    case 'aggregate':
      return '全部用量一个单价，不单独计缓存、图片或推理。'
    case 'cache':
      return writesCache(t)
        ? '普通输入已扣除缓存读写，缓存读取和缓存写入各自定价。上游未报告写入数时无法预估，这类渠道请改用「仅区分缓存命中／未命中」。'
        : '普通输入已扣除缓存读取部分，缓存读取单独定价。'
    case 'cache_read':
      return '只区分命中与未命中，缓存写入按未命中价计。适合只报告命中数的渠道，如百炼隐式缓存、方舟。'
    case 'modality':
      return '文本与图片 Token 分别定价。'
    case 'reasoning':
      return '普通输出与推理输出分别定价，输出总量不重复计费。'
    case 'none':
      return '这一侧不按 Token 收费。'
    default:
      return ''
  }
}
const rateMap = (node: PriceLeaf) =>
  Object.fromEntries(node.items.map((item) => [item.quantity, item.rate]))
/** Recognizes a leaf or full-context tier chain that the form can edit without loss. */
function tierChain(t: PricingTarget, node?: PriceNode) {
  const leaves: PriceLeaf[] = [],
    limits: number[] = []
  while (node?.kind === 'branch') {
    const when = node.when as { field?: unknown; op?: unknown; value?: unknown }
    if (
      when.field !== 'usage.input_total_tokens' ||
      when.op !== 'lte' ||
      typeof when.value !== 'number' ||
      node.then.kind !== 'price'
    )
      return
    leaves.push(node.then)
    limits.push(when.value)
    node = node.else
  }
  if (node?.kind !== 'price' || limits.length >= MAX_TIERS) return
  leaves.push(node)
  const first = leaves[0]!
  const expected = [
    ...(t.profile.input_partitions[first.input_partition] ?? []),
    ...(t.profile.output_partitions[first.output_partition] ?? []),
  ]
    .sort()
    .join()
  const editable =
    limits.every((value, index) => index === 0 || value > limits[index - 1]!) &&
    leaves.every(
      (item) =>
        item.input_partition === first.input_partition &&
        item.output_partition === first.output_partition &&
        item.factor === first.factor &&
        item.items.every((price) => price.per === 1000000) &&
        item.items
          .map((price) => price.quantity)
          .sort()
          .join() === expected,
    )
  return editable ? { leaves, limits } : undefined
}
type HourPredicate = { field?: unknown; op?: unknown; value?: unknown }
function idleWhen(time: Editor['time']) {
  const bounds = [
    { field: 'context.pricing_hour', op: 'gte', value: time.start },
    { field: 'context.pricing_hour', op: 'lt', value: time.end },
  ]
  return time.start < time.end ? { all: bounds } : { any: bounds }
}
/** Recognizes an idle/peak split produced by the time template. */
function timeSplit(t: PricingTarget, price?: OperationPrice) {
  const basis = price?.rule.time_basis,
    root = price?.rule.root
  if (
    basis?.source !== 'observed_response' ||
    basis.calendar !== 'all_days' ||
    root?.kind !== 'branch'
  )
    return
  const when = root.when as { all?: HourPredicate[]; any?: HourPredicate[] }
  const bounds = when.all ?? when.any
  const [low, high] = bounds ?? []
  if (
    bounds?.length !== 2 ||
    Object.keys(when).length !== 1 ||
    low?.field !== 'context.pricing_hour' ||
    low.op !== 'gte' ||
    high?.field !== 'context.pricing_hour' ||
    high.op !== 'lt' ||
    typeof low.value !== 'number' ||
    typeof high.value !== 'number' ||
    !!when.all !== low.value < high.value
  )
    return
  const idle = tierChain(t, root.then),
    peak = tierChain(t, root.else)
  if (
    !idle ||
    !peak ||
    idle.limits.join() !== peak.limits.join() ||
    idle.leaves[0]!.input_partition !== peak.leaves[0]!.input_partition ||
    idle.leaves[0]!.output_partition !== peak.leaves[0]!.output_partition ||
    idle.leaves[0]!.factor !== peak.leaves[0]!.factor
  )
    return
  return { timezone: basis.timezone, start: low.value, end: high.value, idle, peak }
}
const timezones = [
  'Asia/Shanghai',
  'Asia/Hong_Kong',
  'Asia/Tokyo',
  'Asia/Singapore',
  'America/New_York',
  'America/Los_Angeles',
  'Europe/London',
  'UTC',
]
function timezoneLabel(tz: string) {
  const map: Record<string, string> = {
    'Asia/Shanghai': '北京时间 UTC+8',
    'Asia/Hong_Kong': '香港时间 UTC+8',
    'Asia/Tokyo': '东京时间 UTC+9',
    'Asia/Singapore': '新加坡时间 UTC+8',
    'America/New_York': '纽约时间 UTC-5/-4',
    'America/Los_Angeles': '洛杉矶时间 UTC-8/-7',
    'Europe/London': '伦敦时间 UTC+0/+1',
    UTC: '世界标准时 UTC',
  }
  return map[tz] ?? tz
}
const currencies = ['CNY', 'USD', 'EUR', 'GBP', 'JPY', 'HKD', 'SGD', 'AUD', 'CAD', 'KRW']
function newEditor(t: PricingTarget, price?: OperationPrice): Editor {
  const root = price?.rule.root
  const timed = timeSplit(t, price)
  const parsed = price?.rule.time_basis ? undefined : tierChain(t, root)
  const chain = timed?.peak ?? (parsed?.limits.length ? parsed : undefined)
  const simple = chain
    ? chain.leaves[0]
    : root?.kind === 'price' && !price?.rule.time_basis
      ? root
      : undefined
  const tokens = simple?.items.every(
    (item) => t.profile.quantities[item.quantity] === 'token' && item.per === 1000000,
  )
  const single =
    simple?.items.length === 1 && simple.items[0]?.per === 1 ? simple.items[0] : undefined
  return {
    enabled: !!price,
    template: timed
      ? 'time'
      : chain
        ? 'tier'
        : tokens
          ? 'token'
          : single?.quantity === 'usage.generated_images'
            ? 'image'
            : single?.quantity === 'events.upstream_success_count'
              ? 'request'
              : root
                ? 'json'
                : t.profile.input_partitions.aggregate
                  ? 'token'
                  : 'image',
    currency: price?.rule.currency ?? 'CNY',
    input: simple?.input_partition ?? (t.profile.input_partitions.aggregate ? 'aggregate' : 'none'),
    output:
      simple?.output_partition ??
      (t.operation === 'text.embed'
        ? 'none'
        : t.profile.output_partitions.aggregate
          ? 'aggregate'
          : 'none'),
    rates:
      simple && (tokens || single)
        ? Object.fromEntries(simple.items.map((item) => [item.quantity, item.rate]))
        : {},
    tiers: chain?.limits.length
      ? chain.leaves.slice(1).map((item, index) => ({
          after: chain.limits[index]!,
          rates: rateMap(item),
          idle: timed ? rateMap(timed.idle.leaves[index + 1]!) : {},
        }))
      : [{ after: 32000, rates: {}, idle: {} }],
    time: timed
      ? {
          timezone: timed.timezone,
          start: timed.start,
          end: timed.end,
          tiered: timed.peak.limits.length > 0,
          idle: rateMap(timed.idle.leaves[0]!),
        }
      : { timezone: 'Asia/Shanghai', start: 22, end: 8, tiered: false, idle: {} },
    factor: simple?.factor ?? '1',
    json: price ? JSON.stringify(price.rule, null, 2) : '',
    source: price ? { ...price.source } : { label: '管理员配置', version: 'custom.1' },
  }
}
async function load() {
  const value = await pricingApi.get(props.entry.id)
  view.value = value
  mode.value = value.config.mode
  for (const key of Object.keys(editors)) delete editors[key]
  const existing = (item: PricingTarget) =>
    value.config.rules.find(
      (rule) => rule.operation === item.operation && rule.delivery === item.delivery,
    )
  const signature = (item: PricingTarget) => {
    const price = existing(item)
    return JSON.stringify([price ? [price.rule, price.source] : null, item.reference])
  }
  const split = new Set<string>()
  for (const item of value.operations) {
    const peer = value.operations.find(
      (other) => family(other) === family(item) && signature(other) !== signature(item),
    )
    if (peer) split.add(family(item))
  }
  const next: Group[] = []
  for (const item of value.operations) {
    const host = !split.has(family(item)) && next.find((g) => g.family === family(item))
    if (host) host.members.push(item)
    else {
      next.push({ key: targetKey(item), family: family(item), members: [item] })
      editors[targetKey(item)] = newEditor(item, existing(item))
    }
  }
  groups.value = next
  mismatched.value = [...split]
  selected.value = (next.find((g) => editors[g.key]?.enabled) ?? next[0])?.key ?? ''
  baseline.value = fingerprint.value
  result.value = null
  sample.value = ''
}
function quantities(t: PricingTarget, e: Editor): string[] {
  if (e.template === 'image') return ['usage.generated_images']
  if (e.template === 'request') return ['events.upstream_success_count']
  return [
    ...(t.profile.input_partitions[e.input] ?? []),
    ...(t.profile.output_partitions[e.output] ?? []),
  ]
}
function leaf(
  t: PricingTarget,
  e: Editor,
  id: string,
  rates: Record<string, string>,
  strict = true,
): PriceLeaf {
  const token = e.template === 'token' || e.template === 'tier' || e.template === 'time'
  const items = quantities(t, e).map((quantity) => {
    const rate = rates[quantity]?.trim() ?? ''
    if (strict && !/^\d+(\.\d{1,18})?$/.test(rate))
      throw new Error('请填写每一项单价；免费项请明确填 0。')
    return { id: quantity.split('.').at(-1)!, quantity, rate, per: token ? 1000000 : 1 }
  })
  if (strict && !items.length)
    throw new Error('请至少选择一种 Token 计价分区，或切换为按次、按张。')
  return {
    kind: 'price',
    id,
    input_partition: token ? e.input : 'none',
    output_partition: token ? e.output : 'none',
    factor: e.factor,
    items,
  }
}
function rule(t: PricingTarget, e: Editor): PriceRule {
  if (e.template === 'json') {
    try {
      return JSON.parse(e.json) as PriceRule
    } catch {
      throw new Error('价格规则 JSON 格式有误，请修正后再试。')
    }
  }
  return { format: 1, currency: e.currency, root: formRoot(t, e), ...timeBasis(e) }
}
function timeBasis(e: Editor): Pick<PriceRule, 'time_basis'> {
  if (e.template !== 'time') return {}
  return {
    time_basis: { source: 'observed_response', timezone: e.time.timezone, calendar: 'all_days' },
  }
}
/** Tier i covers (tiers[i-1].after, tiers[i].after]; the last tier is unbounded. */
function formRoot(t: PricingTarget, e: Editor, strict = true): PriceNode {
  if (e.template === 'tier')
    return chain(t, e, 'tier_', [e.rates, ...e.tiers.map((item) => item.rates)], strict)
  if (e.template !== 'time') return leaf(t, e, 'standard', e.rates, strict)
  const { start, end, tiered } = e.time
  if (
    strict &&
    ([start, end].some((hour) => !Number.isInteger(hour) || hour < 0 || hour > 23) || start === end)
  )
    throw new Error('闲时开始和结束须为 0–23 的整点，且不能相同。')
  const idle = tiered
    ? chain(t, e, 'idle_tier_', [e.time.idle, ...e.tiers.map((item) => item.idle)], strict)
    : leaf(t, e, 'idle', e.time.idle, strict)
  const peak = tiered
    ? chain(t, e, 'peak_tier_', [e.rates, ...e.tiers.map((item) => item.rates)], strict)
    : leaf(t, e, 'peak', e.rates, strict)
  return { kind: 'branch', when: idleWhen(e.time), then: idle, else: peak }
}
function chain(
  t: PricingTarget,
  e: Editor,
  prefix: string,
  rateSets: Record<string, string>[],
  strict: boolean,
): PriceNode {
  const limits = e.tiers.map((item) => item.after)
  if (
    strict &&
    limits.some(
      (value, index) =>
        !Number.isInteger(value) || value < 0 || (index > 0 && value <= limits[index - 1]!),
    )
  )
    throw new Error('阶梯分界须为逐档递增的非负整数。')
  const leaves = rateSets.map((rates, index) => leaf(t, e, `${prefix}${index + 1}`, rates, strict))
  let root: PriceNode = leaves.at(-1)!
  for (let index = limits.length - 1; index >= 0; index--)
    root = {
      kind: 'branch',
      when: { field: 'usage.input_total_tokens', op: 'lte', value: limits[index] },
      then: leaves[index]!,
      else: root,
    }
  return root
}
function addTier() {
  const e = editor.value
  if (!e || e.tiers.length + 1 >= MAX_TIERS) return
  const last = e.tiers.at(-1)?.after ?? 0
  e.tiers.push({ after: last ? last * 2 : 32000, rates: {}, idle: {} })
}
function removeTier(index: number) {
  editor.value?.tiers.splice(index, 1)
}
function tierRange(e: Editor, index: number) {
  const low = index === 0 ? undefined : e.tiers[index - 1]!.after
  const high = e.tiers[index]?.after
  if (low === undefined) return `完整输入 ≤ ${high}`
  if (high === undefined) return `完整输入 > ${low}`
  return `${low} < 完整输入 ≤ ${high}`
}
const opSymbols: Record<string, string> = {
  eq: '==',
  lt: '<',
  lte: '<=',
  gt: '>',
  gte: '>=',
  in: 'in',
}
function predicate(when: Record<string, unknown>): string {
  if (Array.isArray(when.all))
    return when.all.map((item) => predicate(item as Record<string, unknown>)).join(' && ')
  if (Array.isArray(when.any))
    return `(${when.any.map((item) => predicate(item as Record<string, unknown>)).join(' || ')})`
  const field = String(when.field ?? '').replace(/^(usage|context|events)\./, '')
  return `${field} ${opSymbols[String(when.op)] ?? when.op} ${JSON.stringify(when.value)}`
}
function expressionOf(node: PriceNode, indent = ''): string {
  if (node.kind === 'branch')
    return [
      `${predicate(node.when)}`,
      `${indent}  ? ${expressionOf(node.then, indent + '    ')}`,
      `${indent}  : ${expressionOf(node.else, indent + '    ')}`,
    ].join('\n')
  const sum = node.items
    .map((item) => {
      const name = quantityLabels[item.quantity] ?? item.quantity
      const unit = [1, 1000000].includes(item.per) ? '' : ` / ${item.per}`
      return `${name} × ${item.rate || '?'}${unit}`
    })
    .join(' + ')
  const factor = node.factor && node.factor !== '1' ? ` × ${node.factor}` : ''
  return `tier("${node.id}", ${factor ? `(${sum})${factor}` : sum})`
}
/** Read-only NewAPI-style view of the current draft; never parsed back. */
const expression = computed(() => {
  const t = target.value,
    e = editor.value
  if (!t || !e) return ''
  try {
    const root =
      e.template === 'json' ? (JSON.parse(e.json) as PriceRule).root : formRoot(t, e, false)
    return root ? expressionOf(root) : ''
  } catch {
    return ''
  }
})
const expressionUnit = computed(() => {
  const e = editor.value
  if (!e) return ''
  if (e.template === 'image') return `${e.currency} / 张`
  if (e.template === 'request') return `${e.currency} / 次`
  return e.template === 'json' ? `${e.currency}，单价按各项 per` : `${e.currency} / 百万 Token`
})
function draft(): PricingConfig {
  if (mode.value !== 'override') return { mode: mode.value, rules: [] }
  const owner = (item: PricingTarget) =>
    groups.value.find((g) => g.members.includes(item))?.key ?? targetKey(item)
  const rules = (view.value?.operations ?? [])
    .filter((item) => editors[owner(item)]?.enabled)
    .map((item) => {
      const current = editors[owner(item)]!
      return {
        operation: item.operation,
        delivery: item.delivery,
        rule: rule(item, current),
        source: {
          ...current.source,
          label: current.source.label.trim(),
          version: current.source.version.trim(),
          url: current.source.url?.trim() || null,
        },
      }
    })
  if (!rules.length) throw new Error('请至少为一个操作启用自定义价格。')
  return { mode: 'override', rules }
}
function setMode(value: string | number | boolean | undefined) {
  mode.value = value as PricingConfig['mode']
  if (value === 'override' && editor.value && !Object.values(editors).some((item) => item.enabled))
    editor.value.enabled = true
  result.value = null
}
function jsonTemplate() {
  const t = target.value,
    e = editor.value
  if (!t || !e) return
  if (e.template !== 'json')
    e.json = JSON.stringify(
      { format: 1, currency: e.currency, root: formRoot(t, e, false), ...timeBasis(e) },
      null,
      2,
    )
  e.template = 'json'
}
function selectOperation() {
  result.value = null
  sample.value = ''
}
/** Splits the selected group; every member starts from a copy of the shared editor. */
function splitGroup() {
  const current = group.value
  if (!current || current.members.length < 2) return
  const base = editors[current.key]!
  for (const member of current.members.slice(1))
    editors[targetKey(member)] = JSON.parse(JSON.stringify(base)) as Editor
  groups.value.splice(
    groups.value.indexOf(current),
    1,
    ...current.members.map((member) => ({
      key: targetKey(member),
      family: current.family,
      members: [member],
    })),
  )
}
/** Re-merges all groups of the selected family, keeping the selected editor's prices. */
function mergeGroup() {
  const current = group.value
  if (!current || !view.value) return
  const all = peers.value
  const members = view.value.operations.filter((item) => all.some((g) => g.members.includes(item)))
  const base = editors[current.key]!
  const index = groups.value.indexOf(all[0]!)
  for (const g of all) delete editors[g.key]
  const key = targetKey(members[0]!)
  editors[key] = base
  groups.value = groups.value.filter((g) => !all.includes(g))
  groups.value.splice(index, 0, { key, family: current.family, members })
  mismatched.value = mismatched.value.filter((item) => item !== current.family)
  selected.value = key
  selectOperation()
}
function copyReference() {
  if (!target.value?.reference) return
  const e = newEditor(target.value, target.value.reference)
  e.source = {
    label: '管理员覆盖（参考官方原价）',
    version: 'custom.1',
    url: target.value.reference.source.url,
  }
  editors[selected.value] = e
  mode.value = 'override'
}
function sampleData() {
  const t = target.value,
    e = editor.value
  if (!t) return
  let timed = e?.template === 'time'
  if (e?.template === 'json')
    try {
      timed = !!(JSON.parse(e.json) as PriceRule).time_basis
    } catch {
      timed = false
    }
  const values = Object.fromEntries(
    Object.entries(t.profile.quantities).map(([key, unit]) => [
      key,
      {
        value: t.profile.structural_zero?.includes(key)
          ? 0
          : key.includes('success_count') || key.includes('generated_images')
            ? 1
            : key === 'usage.input_total_tokens'
              ? 1000
              : key === 'usage.input_regular_tokens' || key === 'usage.input_uncached_tokens'
                ? 900
                : key === 'usage.input_cache_read_tokens'
                  ? 100
                  : key === 'usage.output_total_tokens'
                    ? 200
                    : key === 'usage.output_regular_tokens'
                      ? 150
                      : key === 'usage.reasoning_tokens'
                        ? 50
                        : key === 'usage.input_text_tokens'
                          ? 1000
                          : key === 'usage.output_image_tokens'
                            ? 200
                            : 0,
        unit,
        source: 'manual',
        complete: true,
      },
    ]),
  )
  sample.value = JSON.stringify(
    {
      context: timed ? { pricing_hour: 23 } : {},
      facts: { format: 1, profile: t.profile.id, values },
    },
    null,
    2,
  )
}
async function preview() {
  await run(async () => {
    if (!target.value) return
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(sample.value || '{}') as Record<string, unknown>
    } catch {
      throw new Error('试算计量 JSON 格式有误。')
    }
    result.value = null
    result.value = await pricingApi.preview(props.entry.id, {
      ...parsed,
      config: draft(),
      operation: target.value.operation,
      delivery: target.value.delivery,
      context: parsed.context ?? {},
      facts: parsed.facts ?? { profile: target.value.profile.id, values: {} },
    })
  })
}
async function save() {
  await run(async () => {
    if (!view.value) return
    await pricingApi.save(props.entry.id, view.value.revision, draft())
    baseline.value = fingerprint.value
    emit('saved')
    emit('close')
  })
}
onMounted(() => run(load))
</script>
<template>
  <AppDialog
    drawer
    append-to-body
    :title="`${entry.name || entry.key} · 预估价格`"
    :busy="busy"
    protect-changes
    :dirty="baseline !== fingerprint"
    @close="emit('close')"
  >
    <el-alert v-if="error" role="alert" type="error" :closable="false">{{ error }}</el-alert>
    <p v-if="busy && !view" role="status">正在读取价格…</p>
    <template v-if="view">
      <p>按真实调用计量计算预估费用。实扣、免费额度和账户优惠独立记录。</p>
      <el-alert v-if="!view.binding_valid" type="warning" :closable="false"
        >模型目标已变化，原自定义价格不再生效。请核对后保存。</el-alert
      >
      <el-form class="pricing-form" label-position="top" :disabled="busy" @submit.prevent="save">
        <el-form-item label="价格来源">
          <el-radio-group :model-value="mode" @update:model-value="setMode" aria-label="价格来源">
            <el-radio-button value="inherit">继承参考价</el-radio-button>
            <el-radio-button value="override">自定义</el-radio-button>
            <el-radio-button value="disabled">关闭预估</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="适用操作">
          <AppSelect v-model="selected" aria-label="适用操作" @change="selectOperation">
            <el-option
              v-for="item in groups"
              :key="item.key"
              :value="item.key"
              :label="groupLabel(item)"
            />
          </AppSelect>
        </el-form-item>
        <p v-if="group && group.members.length > 1" class="pricing-help">
          这些操作计量方式相同，价格只需填写一次，保存时为每个操作各写一条相同的规则。
          <el-button link type="primary" @click="splitGroup">分别设置</el-button>
        </p>
        <template v-else-if="group && peers.length > 1">
          <el-alert v-if="mismatched.includes(group.family)" type="warning" :closable="false"
            >组内价格不一致，已分开显示。</el-alert
          >
          <p class="pricing-help">
            此操作与其他 {{ peers.length - 1 }} 个操作计量方式相同。
            <el-button link type="primary" @click="mergeGroup"
              >合并设置（以当前操作的价格为准）</el-button
            >
          </p>
        </template>
        <p v-if="!target">此模型没有可用的调用操作。</p>
        <template v-if="target && editor">
          <template v-if="mode === 'inherit'">
            <template v-if="target.reference">
              <p>{{ target.reference.source.label }} · {{ target.reference.source.version }}</p>
              <p v-if="target.reference.source.checked_at">
                核对时间：{{ target.reference.source.checked_at }}
              </p>
              <pre class="pricing-json">{{ JSON.stringify(target.reference.rule, null, 2) }}</pre>
            </template>
            <el-alert v-else type="info" :closable="false"
              >没有匹配此渠道和上游模型的参考价。请选择自定义并填写采购单价；未配置价格不会按 0
              处理。</el-alert
            >
          </template>
          <p v-else-if="mode === 'disabled'">关闭后新调用不再预估，历史调用仍保留原价格快照。</p>
          <template v-else>
            <el-checkbox v-model="editor.enabled">为此操作覆盖参考价</el-checkbox>
            <p class="pricing-help">
              未覆盖的操作继续继承其精确匹配的参考价。更换模型目标前请先恢复继承。
            </p>
            <template v-if="editor.enabled">
              <div class="row-actions">
                <el-button
                  v-if="target.profile.input_partitions.aggregate"
                  @click="editor.template = 'token'"
                  >Token 单价</el-button
                >
                <el-button
                  v-if="target.profile.input_partitions.aggregate"
                  @click="editor.template = 'tier'"
                  >上下文阶梯</el-button
                >
                <el-button
                  v-if="target.profile.input_partitions.aggregate"
                  @click="editor.template = 'time'"
                  >闲忙时</el-button
                >
                <el-button
                  v-if="target.profile.quantities['usage.generated_images']"
                  @click="editor.template = 'image'"
                  >按张</el-button
                >
                <el-button @click="editor.template = 'request'">按次</el-button>
                <el-button @click="jsonTemplate">高级 JSON</el-button>
                <el-button v-if="target.reference" @click="copyReference">复制参考规则</el-button>
              </div>
              <template v-if="editor.template !== 'json'">
                <div class="pricing-pair">
                  <el-form-item label="币种"
                    ><AppSelect v-model="editor.currency" aria-label="币种"
                      ><el-option
                        v-for="unit in currencies"
                        :key="unit"
                        :value="unit"
                        :label="unit" /></AppSelect
                  ></el-form-item>
                  <el-form-item label="价格系数"
                    ><el-input v-model="editor.factor" aria-label="价格系数" inputmode="decimal"
                  /></el-form-item>
                </div>
                <div
                  v-if="['token', 'tier', 'time'].includes(editor.template)"
                  class="pricing-pair"
                >
                  <el-form-item label="输入计价分区"
                    ><AppSelect v-model="editor.input" aria-label="输入计价分区"
                      ><el-option
                        v-for="(_, key) in target.profile.input_partitions"
                        :key="key"
                        :value="key"
                        :label="partitionLabel(target, key)" /></AppSelect
                  ></el-form-item>
                  <el-form-item label="输出计价分区"
                    ><AppSelect v-model="editor.output" aria-label="输出计价分区"
                      ><el-option
                        v-for="(_, key) in target.profile.output_partitions"
                        :key="key"
                        :value="key"
                        :label="partitionLabel(target, key)" /></AppSelect
                  ></el-form-item>
                  <p class="pricing-help">{{ partitionHelp(target, editor.input) }}</p>
                  <p class="pricing-help">{{ partitionHelp(target, editor.output) }}</p>
                </div>
                <template v-if="editor.template === 'time'">
                  <el-form-item label="时区"
                    ><AppSelect v-model="editor.time.timezone" aria-label="时区"
                      ><el-option
                        v-for="zone in timezones"
                        :key="zone"
                        :value="zone"
                        :label="timezoneLabel(zone)" /></AppSelect
                  ></el-form-item>
                  <div class="pricing-pair pricing-hours">
                    <el-form-item label="闲时开始（整点）"
                      ><el-input-number
                        v-model="editor.time.start"
                        :min="0"
                        :max="23"
                        :precision="0"
                        :step="1"
                        :controls="false"
                        aria-label="闲时开始小时"
                    /></el-form-item>
                    <el-form-item label="闲时结束（整点，不含）"
                      ><el-input-number
                        v-model="editor.time.end"
                        :min="0"
                        :max="23"
                        :precision="0"
                        :step="1"
                        :controls="false"
                        aria-label="闲时结束小时"
                    /></el-form-item>
                  </div>
                  <p class="pricing-help">
                    闲时为 {{ editor.time.start }}:00 至{{
                      editor.time.end <= editor.time.start ? '次日' : ''
                    }}
                    {{
                      editor.time.end
                    }}:00（不含结束整点），其余为忙时。按本系统收到完整响应的时刻计，跨时段请求不拆分。厂商可能按请求开始时间计费，两者可能不一致。忙时价须单独填写，不能留空。单价均为每百万
                    Token。
                  </p>
                  <el-checkbox v-model="editor.time.tiered" aria-label="叠加上下文阶梯"
                    >叠加上下文阶梯</el-checkbox
                  >
                  <p v-if="editor.time.tiered" class="pricing-help">
                    先按时段再按本次完整输入
                    Token（含缓存）选档，再对全部用量使用该档单价，不分段累进。
                  </p>
                  <div class="pricing-tiers-scroll">
                    <table class="pricing-tiers">
                      <thead>
                        <tr>
                          <th scope="col">计价项</th>
                          <template v-if="editor.time.tiered">
                            <th
                              v-for="(_, index) in editor.tiers.length + 1"
                              :key="index"
                              scope="col"
                              colspan="2"
                            >
                              <div>第{{ tierNames[index] }}档</div>
                              <div class="pricing-help">{{ tierRange(editor, index) }}</div>
                            </th>
                          </template>
                          <template v-else>
                            <th scope="col">闲时</th>
                            <th scope="col">忙时</th>
                          </template>
                        </tr>
                        <tr v-if="editor.time.tiered">
                          <th scope="col"></th>
                          <template v-for="(_, index) in editor.tiers.length + 1" :key="index">
                            <th scope="col">闲时</th>
                            <th scope="col">忙时</th>
                          </template>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-if="editor.time.tiered">
                          <th scope="row">上限（含）</th>
                          <td v-for="(tier, index) in editor.tiers" :key="index" colspan="2">
                            <el-input-number
                              v-model="tier.after"
                              :min="0"
                              :max="1000000000"
                              :precision="0"
                              :controls="false"
                              :aria-label="`第${tierNames[index]}档上限`"
                            />
                          </td>
                          <td colspan="2" class="pricing-help">不限</td>
                        </tr>
                        <tr v-for="quantity in quantities(target, editor)" :key="quantity">
                          <th scope="row">{{ quantityLabels[quantity] ?? quantity }}</th>
                          <template v-if="editor.time.tiered">
                            <td>
                              <el-input
                                v-model="editor.time.idle[quantity]"
                                :aria-label="`${quantityLabels[quantity]}第一档闲时单价`"
                                placeholder="免费填 0"
                                inputmode="decimal"
                              />
                            </td>
                            <td>
                              <el-input
                                v-model="editor.rates[quantity]"
                                :aria-label="`${quantityLabels[quantity]}第一档忙时单价`"
                                placeholder="免费填 0"
                                inputmode="decimal"
                              />
                            </td>
                            <template v-for="(tier, index) in editor.tiers" :key="index">
                              <td>
                                <el-input
                                  v-model="tier.idle[quantity]"
                                  :aria-label="`${quantityLabels[quantity]}第${tierNames[index + 1]}档闲时单价`"
                                  placeholder="免费填 0"
                                  inputmode="decimal"
                                />
                              </td>
                              <td>
                                <el-input
                                  v-model="tier.rates[quantity]"
                                  :aria-label="`${quantityLabels[quantity]}第${tierNames[index + 1]}档忙时单价`"
                                  placeholder="免费填 0"
                                  inputmode="decimal"
                                />
                              </td>
                            </template>
                          </template>
                          <template v-else>
                            <td>
                              <el-input
                                v-model="editor.time.idle[quantity]"
                                :aria-label="`${quantityLabels[quantity]}闲时单价`"
                                placeholder="免费填 0"
                                inputmode="decimal"
                              />
                            </td>
                            <td>
                              <el-input
                                v-model="editor.rates[quantity]"
                                :aria-label="`${quantityLabels[quantity]}忙时单价`"
                                placeholder="免费填 0"
                                inputmode="decimal"
                              />
                            </td>
                          </template>
                        </tr>
                        <tr v-if="editor.time.tiered">
                          <th scope="row"></th>
                          <td colspan="2"></td>
                          <td v-for="(_, index) in editor.tiers" :key="index" colspan="2">
                            <el-button
                              link
                              type="danger"
                              :aria-label="`删除第${tierNames[index + 1]}档`"
                              @click="removeTier(index)"
                              >删除</el-button
                            >
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <el-button
                    v-if="editor.time.tiered"
                    :disabled="editor.tiers.length + 1 >= MAX_TIERS"
                    @click="addTier"
                    >添加档位</el-button
                  >
                </template>
                <template v-else-if="editor.template !== 'tier'">
                  <el-form-item
                    v-for="quantity in quantities(target, editor)"
                    :key="quantity"
                    :label="`${quantityLabels[quantity] ?? quantity} · ${editor.template === 'token' ? '每百万 Token' : editor.template === 'image' ? '每张' : '每次'}`"
                  >
                    <el-input
                      v-model="editor.rates[quantity]"
                      :aria-label="`${quantityLabels[quantity]}单价`"
                      placeholder="请填写；免费项填 0"
                      inputmode="decimal"
                    />
                  </el-form-item>
                </template>
                <template v-else>
                  <p class="pricing-help">
                    按本次完整输入
                    Token（含缓存）选档，再对全部用量使用该档单价，不分段累进。单价均为每百万
                    Token。
                  </p>
                  <div class="pricing-tiers-scroll">
                    <table class="pricing-tiers">
                      <thead>
                        <tr>
                          <th scope="col">计价项</th>
                          <th
                            v-for="(_, index) in editor.tiers.length + 1"
                            :key="index"
                            scope="col"
                          >
                            <div>第{{ tierNames[index] }}档</div>
                            <div class="pricing-help">{{ tierRange(editor, index) }}</div>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <th scope="row">上限（含）</th>
                          <td v-for="(tier, index) in editor.tiers" :key="index">
                            <el-input-number
                              v-model="tier.after"
                              :min="0"
                              :max="1000000000"
                              :precision="0"
                              :controls="false"
                              :aria-label="`第${tierNames[index]}档上限`"
                            />
                          </td>
                          <td class="pricing-help">不限</td>
                        </tr>
                        <tr v-for="quantity in quantities(target, editor)" :key="quantity">
                          <th scope="row">{{ quantityLabels[quantity] ?? quantity }}</th>
                          <td>
                            <el-input
                              v-model="editor.rates[quantity]"
                              :aria-label="`${quantityLabels[quantity]}第一档单价`"
                              placeholder="免费填 0"
                              inputmode="decimal"
                            />
                          </td>
                          <td v-for="(tier, index) in editor.tiers" :key="index">
                            <el-input
                              v-model="tier.rates[quantity]"
                              :aria-label="`${quantityLabels[quantity]}第${tierNames[index + 1]}档单价`"
                              placeholder="免费填 0"
                              inputmode="decimal"
                            />
                          </td>
                        </tr>
                        <tr>
                          <th scope="row"></th>
                          <td></td>
                          <td v-for="(_, index) in editor.tiers" :key="index">
                            <el-button
                              link
                              type="danger"
                              :aria-label="`删除第${tierNames[index + 1]}档`"
                              @click="removeTier(index)"
                              >删除</el-button
                            >
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <el-button :disabled="editor.tiers.length + 1 >= MAX_TIERS" @click="addTier"
                    >添加档位</el-button
                  >
                </template>
              </template>
              <el-form-item v-else label="结构化价格规则"
                ><el-input
                  v-model="editor.json"
                  type="textarea"
                  :rows="16"
                  aria-label="结构化价格规则"
              /></el-form-item>
              <el-form-item v-if="expression" :label="`规则预览（${expressionUnit}）`">
                <pre class="pricing-json" aria-label="规则预览">{{ expression }}</pre>
              </el-form-item>
              <el-form-item label="来源说明"
                ><el-input v-model="editor.source.label" aria-label="来源说明" :maxlength="160"
              /></el-form-item>
              <div class="pricing-pair">
                <el-form-item label="价格版本"
                  ><el-input v-model="editor.source.version" aria-label="价格版本" :maxlength="80"
                /></el-form-item>
                <el-form-item label="来源链接（可选）"
                  ><el-input
                    v-model="editor.source.url"
                    aria-label="来源链接"
                    placeholder="https://…"
                    :maxlength="512"
                /></el-form-item>
              </div>
            </template>
          </template>
          <details v-if="mode === 'override' && editor.template === 'json'" class="pricing-preview">
            <summary>可用计量与选档条件</summary>
            <p>
              缺失不代表 0。字段只接受本操作实际提供的计量，不根据请求张数、图片尺寸或延迟推测用量。
            </p>
            <dl v-for="(unit, quantity) in target.profile.quantities" :key="quantity">
              <dt>{{ quantityLabels[quantity] ?? quantity }}</dt>
              <dd>
                <code>{{ quantity }}</code> · {{ unit
                }}{{
                  target.profile.structural_zero?.includes(quantity) ? ' · 本协议固定为 0' : ''
                }}
              </dd>
            </dl>
            <dl v-for="(kind, field) in target.profile.context_fields" :key="field">
              <dt>
                <code>{{ field }}</code>
              </dt>
              <dd>{{ target.profile.context_enums?.[field]?.join(' / ') ?? kind }}</dd>
            </dl>
          </details>
          <details v-if="mode !== 'disabled'" class="pricing-preview">
            <summary>保存前试算</summary>
            <p>
              仅使用人工示例计量，不调用模型、不保存调用记录。条件参数写入 context，用量填写
              values。
            </p>
            <el-button @click="sampleData">填入示例计量</el-button>
            <el-form-item label="试算条件与计量 JSON"
              ><el-input
                v-model="sample"
                type="textarea"
                :rows="10"
                aria-label="试算条件与计量 JSON"
            /></el-form-item>
            <el-button @click="preview">试算当前草稿</el-button>
            <div v-if="result" role="status">
              <p>
                <strong>{{ estimateStates[result.status] ?? result.status }}</strong> ·
                {{
                  result.amount === null
                    ? (estimateReasons[result.reason ?? ''] ?? result.reason)
                    : `${money(result.amount)} ${result.currency}`
                }}
              </p>
              <p v-if="result.detail.leaf">命中档位：{{ result.detail.leaf }}</p>
              <p v-if="result.detail.missing?.length">
                缺失：{{
                  result.detail.missing.map((key) => quantityLabels[key] ?? key).join('、')
                }}
              </p>
              <pre class="pricing-json">{{ JSON.stringify(result.detail.items, null, 2) }}</pre>
            </div>
          </details>
        </template>
        <div class="row-actions pricing-actions">
          <el-button type="primary" :loading="busy" native-type="submit">保存价格</el-button>
          <el-button @click="run(load)">重新加载价格</el-button>
          <span>配置版本 {{ view.revision }}</span>
        </div>
      </el-form>
    </template>
  </AppDialog>
</template>
<style scoped>
.pricing-form :deep(.app-select) {
  width: 100%;
}
.pricing-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}
.pricing-json {
  overflow: auto;
  max-height: 360px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.pricing-preview {
  margin-top: 24px;
}
.pricing-preview summary {
  cursor: pointer;
  font-weight: 600;
}
.pricing-actions {
  margin-top: 24px;
}
.pricing-tiers-scroll {
  overflow-x: auto;
}
.pricing-tiers {
  width: 100%;
  min-width: 480px;
  margin-bottom: 12px;
  border-collapse: collapse;
}
.pricing-tiers th,
.pricing-tiers td {
  padding: 4px 6px;
  text-align: left;
  vertical-align: top;
  font-weight: normal;
}
.pricing-tiers thead th {
  font-weight: 600;
}
.pricing-tiers :deep(.el-input-number),
.pricing-hours :deep(.el-input-number) {
  width: 100%;
}
.pricing-help {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 520px) {
  .pricing-pair {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
