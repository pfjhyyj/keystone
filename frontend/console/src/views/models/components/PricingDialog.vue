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
type Template = 'token' | 'tier' | 'image' | 'request' | 'json'
interface Editor {
  enabled: boolean
  template: Template
  currency: string
  input: string
  output: string
  rates: Record<string, string>
  higher: Record<string, string>
  threshold: number
  factor: string
  json: string
  source: PriceSource
}
const editors = reactive<Record<string, Editor>>({})
const targetKey = (target: PricingTarget) => `${target.operation}:${target.delivery}`
const target = computed(() =>
  view.value?.operations.find((item) => targetKey(item) === selected.value),
)
const editor = computed(() => editors[selected.value])
const result = ref<EstimateResult | null>(null),
  sample = ref('')
const baseline = ref('')
const fingerprint = computed(() => JSON.stringify([mode.value, editors]))
watch([fingerprint, sample, selected], () => {
  result.value = null
})
const partitionLabels: Record<string, string> = {
  none: '不按 Token 收费',
  aggregate: '全部统一计价',
  cache: '普通输入与缓存分价',
  modality: '文本与图片分价',
  reasoning: '普通输出与推理分价',
}
const currencies = ['CNY', 'USD', 'EUR', 'GBP', 'JPY', 'HKD', 'SGD', 'AUD', 'CAD', 'KRW']
function newEditor(t: PricingTarget, price?: OperationPrice): Editor {
  const root = price?.rule.root
  const simple = root?.kind === 'price' && !price?.rule.time_basis ? root : undefined
  const tokens = simple?.items.every(
    (item) => t.profile.quantities[item.quantity] === 'token' && item.per === 1000000,
  )
  const single =
    simple?.items.length === 1 && simple.items[0]?.per === 1 ? simple.items[0] : undefined
  return {
    enabled: !!price,
    template: tokens
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
    higher: {},
    threshold: 32000,
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
  for (const item of value.operations) {
    const existing = value.config.rules.find(
      (rule) => rule.operation === item.operation && rule.delivery === item.delivery,
    )
    editors[targetKey(item)] = newEditor(item, existing)
  }
  selected.value = value.operations.find((item) => editors[targetKey(item)]?.enabled)
    ? targetKey(value.operations.find((item) => editors[targetKey(item)]?.enabled)!)
    : value.operations[0]
      ? targetKey(value.operations[0])
      : ''
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
function leaf(t: PricingTarget, e: Editor, id: string, rates: Record<string, string>): PriceLeaf {
  const token = e.template === 'token' || e.template === 'tier'
  const items = quantities(t, e).map((quantity) => {
    const rate = rates[quantity]?.trim() ?? ''
    if (!/^\d+(\.\d{1,18})?$/.test(rate)) throw new Error('请填写每一项单价；免费项请明确填 0。')
    return { id: quantity.split('.').at(-1)!, quantity, rate, per: token ? 1000000 : 1 }
  })
  if (!items.length) throw new Error('请至少选择一种 Token 计价分区，或切换为按次、按张。')
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
  const first = leaf(t, e, e.template === 'tier' ? 'tier_a' : 'standard', e.rates)
  return {
    format: 1,
    currency: e.currency,
    root:
      e.template === 'tier'
        ? {
            kind: 'branch',
            when: { field: 'usage.input_total_tokens', op: 'lte', value: e.threshold },
            then: first,
            else: leaf(t, e, 'tier_b', e.higher),
          }
        : first,
  }
}
function draft(): PricingConfig {
  if (mode.value !== 'override') return { mode: mode.value, rules: [] }
  const rules = (view.value?.operations ?? [])
    .filter((item) => editors[targetKey(item)]?.enabled)
    .map((item) => {
      const current = editors[targetKey(item)]!
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
  if (e.template !== 'json') {
    const keys = quantities(t, e)
    const token = ['token', 'tier'].includes(e.template)
    const makeLeaf = (id: string, rates: Record<string, string>): PriceLeaf => ({
      kind: 'price',
      id,
      input_partition: token ? e.input : 'none',
      output_partition: token ? e.output : 'none',
      factor: e.factor,
      items: keys.map((quantity) => ({
        id: quantity.split('.').at(-1)!,
        quantity,
        rate: rates[quantity] ?? '',
        per: t.profile.quantities[quantity] === 'token' ? 1000000 : 1,
      })),
    })
    e.json = JSON.stringify(
      {
        format: 1,
        currency: e.currency,
        root:
          e.template === 'tier'
            ? {
                kind: 'branch',
                when: { field: 'usage.input_total_tokens', op: 'lte', value: e.threshold },
                then: makeLeaf('tier_a', e.rates),
                else: makeLeaf('tier_b', e.higher),
              }
            : makeLeaf('standard', e.rates),
      },
      null,
      2,
    )
  }
  e.template = 'json'
}
function selectOperation() {
  result.value = null
  sample.value = ''
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
  const t = target.value
  if (!t) return
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
              : key === 'usage.input_regular_tokens'
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
    { context: {}, facts: { format: 1, profile: t.profile.id, values } },
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
              v-for="item in view.operations"
              :key="targetKey(item)"
              :value="targetKey(item)"
              :label="`${operationLabels[item.operation]} · ${item.delivery === 'async' ? '异步' : '同步'}`"
            />
          </AppSelect>
        </el-form-item>
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
                <div v-if="['token', 'tier'].includes(editor.template)" class="pricing-pair">
                  <el-form-item label="输入计价分区"
                    ><AppSelect v-model="editor.input" aria-label="输入计价分区"
                      ><el-option
                        v-for="(_, key) in target.profile.input_partitions"
                        :key="key"
                        :value="key"
                        :label="partitionLabels[key] ?? key" /></AppSelect
                  ></el-form-item>
                  <el-form-item label="输出计价分区"
                    ><AppSelect v-model="editor.output" aria-label="输出计价分区"
                      ><el-option
                        v-for="(_, key) in target.profile.output_partitions"
                        :key="key"
                        :value="key"
                        :label="partitionLabels[key] ?? key" /></AppSelect
                  ></el-form-item>
                </div>
                <el-form-item
                  v-if="editor.template === 'tier'"
                  label="完整输入 Token 分界（包含缓存）"
                  ><el-input-number
                    v-model="editor.threshold"
                    :min="0"
                    :max="1000000000"
                    :precision="0"
                    aria-label="上下文分界"
                /></el-form-item>
                <p v-if="editor.template === 'tier'">
                  第一档 ≤ {{ editor.threshold }}；第二档 &gt;
                  {{ editor.threshold }}。本次全部用量按命中档位计价。
                </p>
                <div
                  v-for="quantity in quantities(target, editor)"
                  :key="quantity"
                  :class="{ 'pricing-pair': editor.template === 'tier' }"
                >
                  <el-form-item
                    :label="`${quantityLabels[quantity] ?? quantity} · ${['token', 'tier'].includes(editor.template) ? '每百万 Token' : editor.template === 'image' ? '每张' : '每次'}${editor.template === 'tier' ? '（第一档）' : ''}`"
                  >
                    <el-input
                      v-model="editor.rates[quantity]"
                      :aria-label="`${quantityLabels[quantity]}单价`"
                      placeholder="请填写；免费项填 0"
                      inputmode="decimal"
                    />
                  </el-form-item>
                  <el-form-item
                    v-if="editor.template === 'tier'"
                    :label="`${quantityLabels[quantity]}（第二档）`"
                    ><el-input
                      v-model="editor.higher[quantity]"
                      :aria-label="`${quantityLabels[quantity]}第二档单价`"
                      placeholder="请填写；免费项填 0"
                      inputmode="decimal"
                  /></el-form-item>
                </div>
              </template>
              <el-form-item v-else label="结构化价格规则"
                ><el-input
                  v-model="editor.json"
                  type="textarea"
                  :rows="16"
                  aria-label="结构化价格规则"
              /></el-form-item>
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
