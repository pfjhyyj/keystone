<script setup lang="ts">
import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue'
import {
  flowApi,
  type FlowStep,
  type InvocationDiagnostic,
  type StepAttempt,
} from '../../../api/flows'
import { callStates, operationLabels } from '../../../api/models'
import { can, sessionVersion } from '../../../stores/session'
import { formatDuration } from '../../../utils/duration'
import FlowPayloadView from './FlowPayloadView.vue'

interface PreviewEntry {
  status: 'loading' | 'ready' | 'error'
  url?: string
  text?: string
  error?: string
}

const props = defineProps<{
  runId: string
  step: FlowStep
  calls?: InvocationDiagnostic[]
  durationLabel?: string | null
}>()
const emit = defineEmits<{
  'open-invocation': [id: string]
  'preview-image': [payload: { label: string; url: string }]
}>()

const open = ref(false)
const loading = ref(false)
const error = ref('')
const attempts = shallowRef<StepAttempt[]>([])
const truncated = ref(false)
const previews = ref<Record<string, PreviewEntry>>({})
let detailRequest: AbortController | undefined
const attachmentRequests = new Map<string, AbortController>()

const displayName = computed(() => {
  const latest = attempts.value[attempts.value.length - 1]
  return latest?.label || props.step.name
})

const showRawStepName = computed(() => displayName.value !== props.step.name)

function clearPreviews() {
  for (const controller of attachmentRequests.values()) controller.abort()
  attachmentRequests.clear()
  for (const entry of Object.values(previews.value)) {
    if (entry.url) URL.revokeObjectURL(entry.url)
  }
  previews.value = {}
}

function reset() {
  detailRequest?.abort()
  attempts.value = []
  truncated.value = false
  loading.value = false
  error.value = ''
  clearPreviews()
}

async function fetchAttachment(attempt: StepAttempt, index: number) {
  const key = `${attempt.attempt}:${index}`
  if (!can('flow:diagnose')) return
  const existing = previews.value[key]
  if (existing && (existing.status === 'loading' || existing.status === 'ready')) return
  attachmentRequests.get(key)?.abort()
  const controller = new AbortController()
  attachmentRequests.set(key, controller)
  previews.value = { ...previews.value, [key]: { status: 'loading' } }
  try {
    const blob = await flowApi.stepAttachment(
      props.runId,
      props.step,
      attempt.attempt,
      index,
      controller.signal,
    )
    if (controller.signal.aborted || !can('flow:diagnose')) return
    if (blob.type.startsWith('image/')) {
      previews.value = {
        ...previews.value,
        [key]: { status: 'ready', url: URL.createObjectURL(blob) },
      }
    } else {
      const text = await blob.text()
      if (!controller.signal.aborted && can('flow:diagnose')) {
        previews.value = { ...previews.value, [key]: { status: 'ready', text } }
      }
    }
  } catch (reason) {
    if (!controller.signal.aborted && can('flow:diagnose')) {
      previews.value = {
        ...previews.value,
        [key]: {
          status: 'error',
          error: reason instanceof Error ? reason.message : '附件暂时无法加载',
        },
      }
    }
  } finally {
    if (attachmentRequests.get(key) === controller) attachmentRequests.delete(key)
  }
}

function loadAttemptAttachments(attempt: StepAttempt) {
  attempt.attachments.forEach((_, index) => {
    void fetchAttachment(attempt, index)
  })
}

async function load() {
  reset()
  if (!can('flow:diagnose') || !open.value) return
  const controller = (detailRequest = new AbortController())
  loading.value = true
  try {
    const result = await flowApi.stepAttempts(props.runId, props.step, controller.signal)
    if (controller.signal.aborted || !can('flow:diagnose')) return
    attempts.value = result.items
    truncated.value = result.truncated
    const latest = result.items[result.items.length - 1]
    if (latest?.attachments.length) loadAttemptAttachments(latest)
  } catch (reason) {
    if (!controller.signal.aborted && can('flow:diagnose'))
      error.value = reason instanceof Error ? reason.message : '阶段详情暂时无法读取'
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

function toggle(event: Event) {
  open.value = (event.currentTarget as HTMLDetailsElement).open
  if (open.value) void load()
  else reset()
}

function groupedAttachments(attempt: StepAttempt) {
  const indexed = attempt.attachments.map((item, index) => ({ ...item, index }))
  const groups = [
    { key: 'input', title: '输入图片 / 文件', items: indexed.filter((a) => a.role === 'input') },
    { key: 'output', title: '输出图片 / 产物', items: indexed.filter((a) => a.role === 'output') },
    {
      key: 'artifact',
      title: '阶段产物',
      items: indexed.filter((a) => !a.role || a.role === 'artifact'),
    },
  ]
  return groups.filter((group) => group.items.length > 0)
}

function attemptDuration(item: StepAttempt) {
  if (item.duration_ms != null) return formatDuration(item.duration_ms)
  if (item.started_at && item.finished_at) {
    return formatDuration(Date.parse(item.finished_at) - Date.parse(item.started_at))
  }
  return null
}

function callLabel(operation: string) {
  return operationLabels[operation as keyof typeof operationLabels] ?? operation
}

watch([sessionVersion, () => can('flow:diagnose')], reset, { flush: 'sync' })
onScopeDispose(reset)
</script>

<template>
  <details class="step-detail" @toggle="toggle">
    <summary class="step-summary">
      <svg
        class="step-chevron"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="M6 3.5L10.5 8L6 12.5"
          stroke="currentColor"
          stroke-width="1.75"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
      <span class="step-title">{{ displayName }}</span>
      <span v-if="durationLabel" class="step-duration">· {{ durationLabel }}</span>
      <span v-for="call in calls ?? []" :key="call.invocation_id" class="step-call-badge">
        {{ call.model_name || call.provider_model || '模型调用' }}
      </span>
      <small v-if="showRawStepName" class="step-raw-name">{{ step.name }}</small>
    </summary>

    <div class="attempts">
      <div v-if="calls?.length" class="step-invocations">
        <div v-for="call in calls" :key="call.invocation_id" class="step-invocation-bar">
          <div class="step-invocation-info">
            <strong>{{ call.model_name || call.provider_model || '模型调用' }}</strong>
            <StatusTag :status="call.status">{{ callStates[call.status] }}</StatusTag>
            <span class="invocation-sub">
              {{ callLabel(call.operation) }}
              <template v-if="call.channel_name"> · {{ call.channel_name }}</template>
              <template v-if="formatDuration(call.duration_ms)">
                · 耗时 {{ formatDuration(call.duration_ms) }}</template
              >
            </span>
          </div>
          <el-button
            v-if="can('model_invocation:read')"
            native-type="button"
            link
            type="primary"
            @click="emit('open-invocation', call.invocation_id)"
            >查看调用</el-button
          >
        </div>
      </div>

      <p v-if="loading" role="status">正在读取阶段详情…</p>
      <div v-else-if="error" role="alert">
        <p>{{ error }}</p>
        <el-button link native-type="button" @click="load">重新读取</el-button>
      </div>
      <p v-else-if="!attempts.length" class="empty-note">
        {{
          calls?.length
            ? '本次模型调用的输入/输出诊断未记录（历史运行或已超过保留期清理）。'
            : '此阶段未记录诊断详情（历史运行或已超过保留期清理）。'
        }}
      </p>

      <div v-for="(item, idx) in attempts" :key="item.attempt" class="attempt">
        <div class="attempt-heading">
          <div class="attempt-title-group">
            <h4>{{ item.label }}</h4>
            <span v-if="attemptDuration(item)" class="attempt-duration"
              >耗时 {{ attemptDuration(item) }}</span
            >
          </div>
          <StatusTag :status="item.error ? 'failed' : 'succeeded'">{{
            item.error ? '失败' : '完成'
          }}</StatusTag>
        </div>
        <p v-if="item.kind === 'correction' || attempts.length > 1" class="attempt-number">
          {{ item.kind === 'correction' ? '格式纠正' : '执行' }} · 第 {{ item.attempt }} 次
        </p>

        <div v-if="item.error" class="attempt-error">
          <strong>报错</strong>
          <p>
            {{ item.error.message }} <code>{{ item.error.code }}</code>
          </p>
        </div>

        <template v-if="item.input != null || item.output != null">
          <dl class="payloads">
            <template v-if="item.input != null">
              <dt>阶段输入</dt>
              <dd><FlowPayloadView :value="item.input" /></dd>
            </template>
            <template v-if="item.output != null">
              <dt>阶段输出</dt>
              <dd><FlowPayloadView :value="item.output" /></dd>
            </template>
          </dl>
        </template>

        <div v-if="item.model_calls?.length" class="model-calls">
          <div
            v-for="(call, cIdx) in item.model_calls"
            :key="`${call.invocation_id ?? cIdx}`"
            class="model-call-card"
          >
            <div class="model-call-header">
              <strong>模型调用 · {{ callLabel(call.operation) }}</strong>
              <span v-if="formatDuration(call.duration_ms)" class="attempt-duration"
                >耗时 {{ formatDuration(call.duration_ms) }}</span
              >
              <el-button
                v-if="call.invocation_id && can('model_invocation:read')"
                link
                native-type="button"
                type="primary"
                @click="emit('open-invocation', call.invocation_id)"
                >费用与用量</el-button
              >
            </div>
            <div v-if="call.error" class="attempt-error">
              <p>
                {{ call.error.message }} <code>{{ call.error.code }}</code>
              </p>
            </div>
            <dl class="payloads">
              <template v-if="call.input != null">
                <dt>模型输入</dt>
                <dd><FlowPayloadView :value="call.input" /></dd>
              </template>
              <template v-if="call.output != null">
                <dt>模型输出</dt>
                <dd><FlowPayloadView :value="call.output" /></dd>
              </template>
            </dl>
          </div>
        </div>

        <div v-if="item.attachments.length" class="attachment-groups">
          <template v-if="idx < attempts.length - 1 && !previews[`${item.attempt}:0`]">
            <el-button
              link
              native-type="button"
              type="primary"
              @click="loadAttemptAttachments(item)"
              >加载第 {{ item.attempt }} 次尝试的 {{ item.attachments.length }} 个附件</el-button
            >
          </template>
          <template v-else>
            <div
              v-for="group in groupedAttachments(item)"
              :key="group.key"
              class="attachment-group"
            >
              <div class="group-title">{{ group.title }}</div>
              <div class="attachment-grid">
                <figure v-for="att in group.items" :key="att.index" class="attachment-card">
                  <template v-if="previews[`${item.attempt}:${att.index}`]?.status === 'ready'">
                    <button
                      v-if="previews[`${item.attempt}:${att.index}`]?.url"
                      type="button"
                      class="thumb-button"
                      @click="
                        emit('preview-image', {
                          label: att.label,
                          url: previews[`${item.attempt}:${att.index}`]!.url!,
                        })
                      "
                    >
                      <img
                        :src="previews[`${item.attempt}:${att.index}`]!.url"
                        :alt="att.label"
                        class="thumb-image"
                      />
                    </button>
                    <pre v-else class="text-preview">{{
                      previews[`${item.attempt}:${att.index}`]?.text
                    }}</pre>
                  </template>
                  <div
                    v-else-if="previews[`${item.attempt}:${att.index}`]?.status === 'error'"
                    class="thumb-fallback"
                  >
                    <span>{{ previews[`${item.attempt}:${att.index}`]?.error }}</span>
                    <el-button
                      link
                      native-type="button"
                      type="primary"
                      @click="fetchAttachment(item, att.index)"
                      >重试</el-button
                    >
                  </div>
                  <div v-else class="thumb-loading">加载中…</div>
                  <figcaption>{{ att.label }}</figcaption>
                </figure>
              </div>
            </div>
          </template>
        </div>
      </div>

      <p v-if="truncated">仅显示前 50 次已记录的尝试。</p>
    </div>
  </details>
</template>

<style scoped>
.step-detail > summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  overflow-wrap: anywhere;
  cursor: pointer;
  list-style: none;
}
.step-detail > summary::-webkit-details-marker {
  display: none;
}
.step-chevron {
  flex: 0 0 auto;
  color: var(--el-text-color-placeholder);
  transition:
    transform 0.15s ease,
    color 0.15s ease;
}
.step-detail > summary:hover .step-chevron {
  color: var(--el-text-color-regular);
}
.step-detail[open] > summary .step-chevron {
  transform: rotate(90deg);
  color: var(--el-text-color-primary);
}
.step-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.step-duration {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.step-call-badge {
  display: inline-flex;
  align-items: center;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--el-color-primary-light-8);
  background: #faf7f2;
  color: var(--el-color-primary-dark-2);
  font-size: 12px;
  line-height: 20px;
}
.step-raw-name {
  color: var(--el-text-color-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
.attempts {
  display: grid;
  gap: 12px;
  margin-top: 8px;
}
.step-invocations {
  display: grid;
  gap: 6px;
}
.step-invocation-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
}
.step-invocation-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
}
.invocation-sub,
.empty-note {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin: 0;
}
.attempt {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
}
.attempt-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.attempt-title-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.attempt-duration {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.attempt-heading h4,
.attempt-number,
.payloads,
.payloads dd,
.attempt-error p {
  margin: 0;
}
.attempt-number {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.attempt-error,
.payloads,
.model-calls,
.attachment-groups {
  margin-top: 12px;
}
.attempt-error {
  color: var(--el-color-danger);
  overflow-wrap: anywhere;
}
.payloads {
  display: grid;
  gap: 6px;
}
.payloads dt {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
.payloads dd + dt {
  margin-top: 6px;
}
.model-calls {
  display: grid;
  gap: 10px;
}
.model-call-card {
  padding: 10px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-blank);
}
.model-call-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.attachment-groups {
  display: grid;
  gap: 10px;
}
.group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}
.attachment-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 10px;
}
.attachment-card {
  margin: 0;
  display: grid;
  gap: 4px;
  min-width: 0;
}
.attachment-card figcaption {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}
.thumb-button {
  padding: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-light);
  cursor: zoom-in;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  aspect-ratio: 3 / 4;
}
.thumb-image {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.thumb-loading,
.thumb-fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 88px;
  padding: 8px;
  border: 1px dashed var(--el-border-color);
  border-radius: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
}
.text-preview {
  max-height: 180px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
