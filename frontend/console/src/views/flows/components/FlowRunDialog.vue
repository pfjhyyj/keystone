<script setup lang="ts">
// Independent detail sections and request generation, narrowed to one Run.
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { can, identity, sessionVersion } from '../../../stores/session'
import { ApiError, request } from '../../../api/client'
import {
  extractFileIdsByPaths,
  flowApi,
  flowStates,
  ongoing,
  type FlowRun,
  type FlowStep,
  type InvocationDiagnostic,
  type Steps,
} from '../../../api/flows'
import { callStates, operationLabels, type Costs } from '../../../api/models'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import CostSummary from '../../../components/models/CostSummary.vue'
import FlowPayloadView from './FlowPayloadView.vue'
import FlowRunDiagnostics from './FlowRunDiagnostics.vue'
import FlowStepDetails from './FlowStepDetails.vue'
import { confirmAction } from '../../../utils/confirm'
import { formatDuration } from '../../../utils/duration'

interface OutputPreview {
  fileId: string
  status: 'loading' | 'ready' | 'error'
  url?: string
  text?: string
  error?: string
}

const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const row = ref<FlowRun | null>(null),
  steps = ref<Steps | null>(null),
  costs = ref<Costs | null>(null),
  modelCalls = ref<InvocationDiagnostic[]>([])
const diagnosticsRef = ref<InstanceType<typeof FlowRunDiagnostics> | null>(null)
const detailError = ref(''),
  stepError = ref(''),
  costError = ref(''),
  loading = ref(false)
const outputPreviews = ref<Record<string, OutputPreview>>({})
const lightbox = ref<{ label: string; url: string } | null>(null)
const { busy, error, notice, run } = useAction()

const resultFacts = computed(() =>
  Object.entries(row.value?.result ?? {})
    .filter(
      ([key, value]) =>
        !/(?:^|_)ids?$/i.test(key) && ['string', 'number', 'boolean'].includes(typeof value),
    )
    .slice(0, 8),
)

const resultFileIds = computed(() => {
  if (!row.value?.result) return []
  const declared = extractFileIdsByPaths(
    row.value.result,
    row.value.definition_snapshot?.output_file_fields,
  )
  if (declared.length) return declared
  return Object.entries(row.value.result)
    .filter(([key, value]) => /file_id$/i.test(key) && typeof value === 'string' && value)
    .map(([, value]) => value as string)
})

const resultInvocationIds = computed(() => {
  if (!row.value?.result) return []
  return Object.entries(row.value.result)
    .filter(([key, value]) => /invocation_id$/i.test(key) && typeof value === 'string' && value)
    .map(([, value]) => value as string)
})

const resultMatchedCalls = computed(() => {
  if (!resultInvocationIds.value.length || !modelCalls.value.length) return []
  const byId = new Map(modelCalls.value.map((item) => [item.invocation_id, item]))
  return resultInvocationIds.value
    .map((identifier) => byId.get(identifier))
    .filter((item): item is InvocationDiagnostic => Boolean(item))
})

const resultLabel: Record<string, string> = {
  summary: '摘要',
  message: '说明',
  status: '结果状态',
  verdict: '审查结论',
  count: '数量',
  title: '标题',
  output: '输出',
}

const diagnosticRefresh = ref(0),
  detailTab = ref('execution'),
  copyStatus = ref('')

const duration = computed(() =>
  row.value?.finished_at
    ? formatDuration(Date.parse(row.value.finished_at) - Date.parse(row.value.created_at))
    : null,
)

const failureLabels: Record<string, string> = {
  flow_timeout: '运行超时',
  flow_cancelled: '运行已取消',
  flow_identity_unavailable: '执行身份已不可用',
  flow_execution_failed: '执行过程中发生错误',
  model_timeout: '模型调用超时',
  model_response_unknown: '模型结果待确认',
}

function stepDuration(step: FlowStep): string | null {
  if (step.started_at_epoch_ms != null && step.completed_at_epoch_ms != null) {
    const label = formatDuration(step.completed_at_epoch_ms - step.started_at_epoch_ms)
    return label ? `耗时 ${label}` : null
  }
  if (step.started_at_epoch_ms != null) return '执行中'
  return null
}

function stepTimestamp(step: FlowStep): string {
  const fmt = (ms: number) => new Date(ms).toLocaleTimeString()
  if (step.started_at_epoch_ms != null && step.completed_at_epoch_ms != null) {
    const date = new Date(step.started_at_epoch_ms).toLocaleDateString()
    return `${date} ${fmt(step.started_at_epoch_ms)} → ${fmt(step.completed_at_epoch_ms)}`
  }
  if (step.completed_at_epoch_ms != null) {
    return new Date(step.completed_at_epoch_ms).toLocaleString()
  }
  if (step.started_at_epoch_ms != null) {
    return `开始于 ${new Date(step.started_at_epoch_ms).toLocaleString()}`
  }
  return '尚无执行时间'
}

function callsForStep(step: FlowStep): InvocationDiagnostic[] {
  return modelCalls.value.filter(
    (item) => item.workflow_id === step.workflow_id && item.step_id === step.step_id,
  )
}

const outputControllers = new Map<string, AbortController>()

function clearOutputPreviews() {
  for (const controller of outputControllers.values()) controller.abort()
  outputControllers.clear()
  for (const entry of Object.values(outputPreviews.value)) {
    if (entry.url) URL.revokeObjectURL(entry.url)
  }
  outputPreviews.value = {}
}

async function loadOutputPreview(fileId: string) {
  if (!can('flow:diagnose') || !alive) return
  const existing = outputPreviews.value[fileId]
  if (existing && (existing.status === 'loading' || existing.status === 'ready')) return
  outputControllers.get(fileId)?.abort()
  const controller = new AbortController()
  outputControllers.set(fileId, controller)
  outputPreviews.value = { ...outputPreviews.value, [fileId]: { fileId, status: 'loading' } }
  try {
    const blob = await flowApi.outputFile(props.id, fileId, controller.signal)
    if (controller.signal.aborted || !alive || !can('flow:diagnose')) return
    if (blob.type.startsWith('image/')) {
      outputPreviews.value = {
        ...outputPreviews.value,
        [fileId]: { fileId, status: 'ready', url: URL.createObjectURL(blob) },
      }
    } else {
      const text = await blob.text()
      if (!controller.signal.aborted && alive && can('flow:diagnose')) {
        outputPreviews.value = {
          ...outputPreviews.value,
          [fileId]: { fileId, status: 'ready', text },
        }
      }
    }
  } catch (reason) {
    if (!controller.signal.aborted && alive && can('flow:diagnose')) {
      outputPreviews.value = {
        ...outputPreviews.value,
        [fileId]: {
          fileId,
          status: 'error',
          error: reason instanceof Error ? reason.message : '输出文件无法预览',
        },
      }
    }
  } finally {
    if (outputControllers.get(fileId) === controller) outputControllers.delete(fileId)
  }
}

watch(
  [resultFileIds, () => can('flow:diagnose')],
  ([ids, diagnose]) => {
    if (!diagnose || !ids.length) {
      clearOutputPreviews()
      return
    }
    for (const fileId of ids) void loadOutputPreview(fileId)
  },
  { immediate: true },
)

async function copyId(id: string) {
  let message: string
  try {
    await navigator.clipboard.writeText(id)
    message = '已复制运行 ID'
  } catch {
    message = '无法自动复制，请手动选择运行 ID。'
  }
  if (row.value?.id === id) copyStatus.value = message
}

function unavailable(message: string) {
  generation++
  row.value = null
  steps.value = null
  costs.value = null
  modelCalls.value = []
  clearOutputPreviews()
  detailError.value = message
  loading.value = false
  clearInterval(timer)
}

let generation = 0,
  alive = true,
  timer: ReturnType<typeof setInterval> | undefined

const flowReadable = () => can('flow:read') || can('flow:diagnose')
const cancelAllowed = computed(
  () =>
    row.value &&
    ongoing(row.value) &&
    !row.value.cancel_requested_at &&
    can('flow:read') &&
    can('flow:cancel') &&
    (row.value.actor_id === identity.value?.user.id || can('flow:manage')),
)

async function refresh() {
  if (!alive || loading.value || busy.value || !flowReadable()) return
  const token = ++generation,
    current = () => alive && token === generation && flowReadable()
  loading.value = true
  try {
    const value = await flowApi.detail(props.id)
    if (!current()) return
    row.value = value
    detailError.value = ''
    diagnosticRefresh.value++
    await Promise.all([
      (async () => {
        try {
          const value = await request<Steps>(`/flow-runs/${props.id}/steps`)
          if (current()) {
            steps.value = value
            stepError.value = ''
          }
        } catch (reason) {
          if (current()) {
            steps.value = null
            stepError.value = reason instanceof Error ? reason.message : '执行历史暂不可用'
          }
        }
      })(),
      (async () => {
        if (!can('flow:read') || !can('model_invocation:read')) {
          costs.value = null
          costError.value = '无查看费用权限'
          return
        }
        try {
          const value = await request<Costs>(`/flow-runs/${props.id}/costs?cost_basis=estimated`)
          if (current()) {
            costs.value = value
            costError.value = ''
          }
        } catch (reason) {
          if (current()) {
            costs.value = null
            costError.value = reason instanceof Error ? reason.message : '费用暂不可用'
          }
        }
      })(),
    ])
  } catch (reason) {
    if (current()) {
      detailError.value = reason instanceof Error ? reason.message : '运行查询失败'
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
        unavailable(detailError.value)
      }
    }
  } finally {
    if (current()) loading.value = false
  }
}

async function cancel() {
  if (!cancelAllowed.value || loading.value) return
  const id = props.id
  if (
    !(await confirmAction(
      '确认请求取消此流程？取消是异步请求，已发生的调用、费用和文件写入不会撤回。',
      { title: '取消流程', confirmText: '确认取消流程', cancelText: '继续等待', danger: true },
    ))
  )
    return
  if (!alive || props.id !== id || !cancelAllowed.value || loading.value) return
  await run(async () => {
    const value = await request<FlowRun>(`/flow-runs/${id}/cancel`, 'POST')
    if (!alive) return
    generation++
    row.value = value
    diagnosticRefresh.value++
    notice.value = '取消请求已受理，等待执行端确认；已发生的费用和文件写入不会撤回。'
    emit('changed')
  })
}

watch(flowReadable, (allowed) => {
  if (!allowed) unavailable('当前权限不可查看此运行。')
})

watch(sessionVersion, () => {
  generation++
  row.value = null
  steps.value = null
  costs.value = null
  modelCalls.value = []
  clearOutputPreviews()
  detailTab.value = 'execution'
  copyStatus.value = ''
  loading.value = false
  if (flowReadable()) void refresh()
})

onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (row.value && ongoing(row.value)) void refresh()
  }, 3000)
})

onScopeDispose(() => {
  alive = false
  generation++
  clearInterval(timer)
  clearOutputPreviews()
})
</script>

<template>
  <AppDialog drawer title="运行详情" :busy="busy" @close="$emit('close')"
    ><el-alert v-if="detailError" role="alert" type="error" show-icon :closable="false"
      >{{ detailError }}{{ row ? '；以下保留最近确认状态，请刷新。' : '' }}</el-alert
    >
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <div class="row-actions">
      <el-button :disabled="busy || loading" @click="refresh" native-type="button" type="default"
        >刷新运行详情</el-button
      ><el-button
        v-if="cancelAllowed"
        :disabled="busy || loading"
        @click="cancel"
        native-type="button"
        type="primary"
      >
        取消流程
      </el-button>
    </div>
    <p v-if="loading" role="status">正在确认运行状态…</p>
    <template v-if="row"
      ><div class="detail-summary">
        <div class="detail-title">
          <h2>{{ row.definition_name || row.definition_key }}</h2>
          <StatusTag :status="row.status">{{
            row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status]
          }}</StatusTag>
        </div>
        <p>
          受理于 {{ new Date(row.created_at).toLocaleString()
          }}<template v-if="duration"> · 耗时 {{ duration }}</template>
        </p>
        <p v-if="ongoing(row)">
          最近执行确认：{{
            row.observed_at ? new Date(row.observed_at).toLocaleString() : '尚无执行端确认'
          }}
        </p>
      </div>
      <details class="technical-details">
        <summary>运行信息与技术标识</summary>
        <dl>
          <dt>流程 / 运行 ID</dt>
          <dd>
            {{ row.definition_key }}<br />{{ row.id }}
            <el-button native-type="button" link @click="copyId(row.id)">复制运行 ID</el-button>
            <span v-if="copyStatus" role="status">{{ copyStatus }}</span>
          </dd>
          <dt>契约版本</dt>
          <dd>{{ row.definition_revision ?? '历史契约' }}</dd>
          <template v-if="Object.keys(row.model_bindings ?? {}).length">
            <dt>模型角色绑定</dt>
            <dd v-for="(model, role) in row.model_bindings" :key="role">{{ role }}：{{ model }}</dd>
          </template>
          <dt>状态</dt>
          <dd>
            <StatusTag :status="row.status">
              {{ row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status] }}
            </StatusTag>
          </dd>
          <dt>发起人 ID</dt>
          <dd>{{ row.actor_id }}</dd>
          <template v-if="row.error_code">
            <dt>执行错误码</dt>
            <dd>{{ row.error_code }}</dd>
          </template>
          <dt>受理 / 完成时间</dt>
          <dd>
            {{ new Date(row.created_at).toLocaleString() }} /
            {{ row.finished_at ? new Date(row.finished_at).toLocaleString() : '未完成' }}
          </dd>
          <dt>最近执行状态确认</dt>
          <dd>
            {{ row.observed_at ? new Date(row.observed_at).toLocaleString() : '尚无执行端确认' }}
          </dd>
        </dl>
      </details>
      <el-alert v-if="row.error_code" role="alert" type="error" show-icon :closable="false">
        {{ failureLabels[row.error_code] ?? '运行未能正常完成' }}。重新执行须使用新的运行请求。
      </el-alert>
      <el-tabs v-if="can('flow:read')" v-model="detailTab"
        ><el-tab-pane label="执行与结果" name="execution" /><el-tab-pane
          label="关联记录"
          name="diagnostics" /><el-tab-pane label="调用费用" name="costs"
      /></el-tabs>
      <FlowRunDiagnostics
        v-if="can('flow:read')"
        ref="diagnosticsRef"
        :id="id"
        :refresh="diagnosticRefresh"
        :steps="steps"
        :visible-section="
          detailTab === 'execution' ? 'models' : detailTab === 'diagnostics' ? 'files' : 'none'
        "
        @unavailable="unavailable"
        @models-loaded="modelCalls = $event"
      />
      <section v-show="detailTab === 'execution'" class="execution-section">
        <h3>结果</h3>
        <dl v-if="resultFacts.length" class="result-facts">
          <template v-for="[key, value] in resultFacts" :key="key"
            ><dt>{{ resultLabel[key] ?? key }}</dt>
            <dd>{{ value }}</dd></template
          >
        </dl>

        <div v-if="resultFileIds.length" class="output-previews">
          <template v-if="can('flow:diagnose')">
            <div class="output-grid">
              <figure v-for="(fileId, idx) in resultFileIds" :key="fileId" class="output-card">
                <template v-if="outputPreviews[fileId]?.status === 'ready'">
                  <button
                    v-if="outputPreviews[fileId]?.url"
                    type="button"
                    class="output-thumb-button"
                    @click="
                      lightbox = {
                        label: resultFileIds.length > 1 ? `输出结果 ${idx + 1}` : '输出结果',
                        url: outputPreviews[fileId]!.url!,
                      }
                    "
                  >
                    <img
                      :src="outputPreviews[fileId]!.url"
                      :alt="resultFileIds.length > 1 ? `输出结果 ${idx + 1}` : '输出结果'"
                    />
                  </button>
                  <pre v-else class="output-text">{{ outputPreviews[fileId]?.text }}</pre>
                </template>
                <div v-else-if="outputPreviews[fileId]?.status === 'error'" class="output-fallback">
                  <span>{{ outputPreviews[fileId]?.error }}</span>
                  <el-button
                    link
                    native-type="button"
                    type="primary"
                    @click="loadOutputPreview(fileId)"
                    >重试</el-button
                  >
                </div>
                <div v-else class="output-loading">正在加载输出预览…</div>
                <figcaption>
                  {{ resultFileIds.length > 1 ? `输出产物 ${idx + 1}` : '输出产物' }}
                </figcaption>
              </figure>
            </div>
          </template>
          <p v-else class="muted-note">
            已生成 {{ resultFileIds.length }} 个输出文件，查看内容需要诊断权限或前往业务入口。
          </p>
        </div>

        <div v-if="resultMatchedCalls.length" class="result-calls">
          <div
            v-for="call in resultMatchedCalls"
            :key="call.invocation_id"
            class="result-call-chip"
          >
            <span>
              {{ operationLabels[call.operation] ?? call.operation }} ·
              <strong>{{ call.model_name || call.provider_model }}</strong>
              <template v-if="formatDuration(call.duration_ms)">
                · 耗时 {{ formatDuration(call.duration_ms) }}</template
              >
            </span>
            <StatusTag :status="call.status">{{ callStates[call.status] }}</StatusTag>
            <el-button
              v-if="can('model_invocation:read')"
              link
              native-type="button"
              type="primary"
              @click="diagnosticsRef?.openInvocation(call.invocation_id)"
              >查看调用</el-button
            >
          </div>
        </div>

        <p v-if="!row.result" class="muted-note">尚无结果</p>
        <p
          v-else-if="!resultFacts.length && !resultFileIds.length && !resultMatchedCalls.length"
          class="muted-note"
        >
          已记录结果，原始字段可展开查看。
        </p>

        <h3>步骤记录</h3>
        <el-alert v-if="stepError" role="alert" type="error" show-icon :closable="false"
          >{{ stepError }}；不代表运行失败。</el-alert
        >
        <template v-else-if="steps"
          ><p v-if="!steps.available">尚未形成步骤历史。</p>
          <p v-if="steps.truncated">仅展示部分步骤历史。</p>
          <el-timeline class="flow-timeline">
            <el-timeline-item
              v-for="step in steps.items"
              :key="`${step.workflow_id}:${step.step_id}`"
              :timestamp="stepTimestamp(step)"
              type="primary"
              hollow
            >
              <FlowStepDetails
                v-if="can('flow:diagnose')"
                :run-id="id"
                :step="step"
                :calls="callsForStep(step)"
                :duration-label="stepDuration(step)"
                @open-invocation="diagnosticsRef?.openInvocation($event)"
                @preview-image="lightbox = $event"
              />
              <div v-else class="step-plain">
                <span class="step-plain-name">{{ step.name }}</span>
                <span v-if="stepDuration(step)" class="step-plain-duration"
                  >· {{ stepDuration(step) }}</span
                >
              </div>
            </el-timeline-item>
          </el-timeline></template
        >

        <details class="secondary-section">
          <summary>运行输入</summary>
          <FlowPayloadView :value="row.input" />
          <details class="raw-json-toggle">
            <summary>原始输入 JSON</summary>
            <pre class="audit-details">{{ JSON.stringify(row.input, null, 2) }}</pre>
          </details>
        </details>

        <details v-if="row.result" class="secondary-section">
          <summary>结果原始字段（含技术 ID）</summary>
          <pre class="audit-details">{{ JSON.stringify(row.result, null, 2) }}</pre>
        </details>
      </section>

      <section v-if="can('flow:read')" v-show="detailTab === 'costs'">
        <h3>模型调用预估费用 · 最近 30 天</h3>
        <el-alert v-if="costError" role="alert" type="error" show-icon :closable="false">{{
          costError
        }}</el-alert>
        <CostSummary v-if="costs" :value="costs" />
      </section>
    </template>

    <div
      v-if="lightbox"
      class="image-lightbox"
      role="dialog"
      :aria-label="lightbox.label"
      @click.self="lightbox = null"
    >
      <div class="lightbox-inner">
        <div class="lightbox-bar">
          <strong>{{ lightbox.label }}</strong>
          <el-button native-type="button" @click="lightbox = null">关闭预览</el-button>
        </div>
        <img :src="lightbox.url" :alt="lightbox.label" />
      </div>
    </div>
  </AppDialog>
</template>

<style scoped>
.execution-section {
  display: grid;
  gap: 10px;
}
.muted-note {
  margin: 0;
  color: var(--el-text-color-secondary);
}
.output-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
}
.output-card {
  margin: 0;
  display: grid;
  gap: 4px;
}
.output-card figcaption {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.output-thumb-button {
  padding: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-light);
  cursor: zoom-in;
  overflow: hidden;
  aspect-ratio: 3 / 4;
  display: flex;
  align-items: center;
  justify-content: center;
}
.output-thumb-button img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.output-text {
  max-height: 200px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.output-loading,
.output-fallback {
  min-height: 100px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
}
.result-calls {
  display: grid;
  gap: 6px;
}
.result-call-chip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
}
.step-plain {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
}
.step-plain-name {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.step-plain-duration {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.secondary-section {
  margin-top: 4px;
}
.secondary-section > summary {
  cursor: pointer;
  color: var(--el-text-color-secondary);
}
.raw-json-toggle {
  margin-top: 8px;
}
.image-lightbox {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: rgba(15, 23, 42, 0.72);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.lightbox-inner {
  max-width: min(92vw, 960px);
  max-height: 90vh;
  background: var(--el-bg-color);
  border-radius: 8px;
  padding: 12px;
  display: grid;
  gap: 10px;
}
.lightbox-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.lightbox-inner img {
  display: block;
  max-width: 100%;
  max-height: 78vh;
  object-fit: contain;
  margin: 0 auto;
}
</style>
