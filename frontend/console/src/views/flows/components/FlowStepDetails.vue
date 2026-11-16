<script setup lang="ts">
import { onScopeDispose, ref, shallowRef, watch } from 'vue'
import { flowApi, type FlowStep, type StepAttempt } from '../../../api/flows'
import { can, sessionVersion } from '../../../stores/session'

const props = defineProps<{ runId: string; step: FlowStep }>()
const open = ref(false)
const loading = ref(false)
const error = ref('')
const attempts = shallowRef<StepAttempt[]>([])
const truncated = ref(false)
const preview = ref<{ key: string; label: string; url?: string; text?: string }>()
const previewLoading = ref('')
const previewError = ref('')
let detailRequest: AbortController | undefined
let previewRequest: AbortController | undefined

function clearPreview() {
  previewRequest?.abort()
  if (preview.value?.url) URL.revokeObjectURL(preview.value.url)
  preview.value = undefined
  previewLoading.value = ''
  previewError.value = ''
}
function reset() {
  detailRequest?.abort()
  attempts.value = []
  truncated.value = false
  loading.value = false
  error.value = ''
  clearPreview()
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
async function showAttachment(attempt: StepAttempt, index: number) {
  const key = `${attempt.attempt}:${index}`
  const closing = preview.value?.key === key
  clearPreview()
  if (closing || !can('flow:diagnose')) return
  const controller = (previewRequest = new AbortController())
  previewLoading.value = key
  try {
    const blob = await flowApi.stepAttachment(
      props.runId,
      props.step,
      attempt.attempt,
      index,
      controller.signal,
    )
    if (controller.signal.aborted || !can('flow:diagnose')) return
    const label = attempt.attachments[index].label
    if (['image/jpeg', 'image/png'].includes(blob.type)) {
      preview.value = { key, label, url: URL.createObjectURL(blob) }
    } else {
      const text = await blob.text()
      if (!controller.signal.aborted && can('flow:diagnose')) preview.value = { key, label, text }
    }
  } catch (reason) {
    if (!controller.signal.aborted && can('flow:diagnose'))
      previewError.value = reason instanceof Error ? reason.message : '产物暂时无法打开'
  } finally {
    if (!controller.signal.aborted) previewLoading.value = ''
  }
}
watch([sessionVersion, () => can('flow:diagnose')], reset, { flush: 'sync' })
onScopeDispose(reset)
</script>

<template>
  <details class="step-detail" @toggle="toggle">
    <summary>{{ step.name }}</summary>
    <div class="attempts">
      <p v-if="loading" role="status">正在读取阶段详情…</p>
      <div v-else-if="error" role="alert">
        <p>{{ error }}</p>
        <el-button link native-type="button" @click="load">重新读取</el-button>
      </div>
      <p v-else-if="!attempts.length">未记录阶段输入、输出或错误。</p>
      <div v-for="item in attempts" :key="item.attempt" class="attempt">
        <div class="attempt-heading">
          <h4>{{ item.label }}</h4>
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
        <dl class="payloads">
          <dt>输入</dt>
          <dd>
            <pre v-if="item.input != null" class="payload">{{
              JSON.stringify(item.input, null, 2)
            }}</pre>
            <span v-else>未记录</span>
          </dd>
          <dt>输出</dt>
          <dd>
            <pre v-if="item.output != null" class="payload">{{
              JSON.stringify(item.output, null, 2)
            }}</pre>
            <span v-else>未记录</span>
          </dd>
        </dl>
        <div v-if="item.attachments.length" class="attachments">
          <el-button
            v-for="(attachment, index) in item.attachments"
            :key="index"
            link
            native-type="button"
            type="primary"
            :loading="previewLoading === `${item.attempt}:${index}`"
            @click="showAttachment(item, index)"
            >{{ attachment.label }}</el-button
          >
        </div>
        <figure v-if="preview?.key.startsWith(`${item.attempt}:`)" class="preview">
          <figcaption>{{ preview.label }}</figcaption>
          <img v-if="preview.url" :src="preview.url" :alt="preview.label" />
          <pre v-else class="payload">{{ preview.text }}</pre>
        </figure>
      </div>
      <p v-if="previewError" role="alert">{{ previewError }}</p>
      <p v-if="truncated">仅显示前 50 次已记录的尝试。</p>
    </div>
  </details>
</template>

<style scoped>
.step-detail > summary {
  overflow-wrap: anywhere;
  cursor: pointer;
}
.attempts {
  display: grid;
  gap: 12px;
  margin-top: 8px;
}
.attempt {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}
.attempt-heading,
.attachments {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.attempt-heading {
  justify-content: space-between;
}
.attempt-heading h4,
.attempt-number,
.payloads,
.payloads dd,
.attempt-error p,
.preview {
  margin: 0;
}
.attempt-number,
.preview figcaption {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
}
.attempt-error,
.payloads,
.attachments,
.preview {
  margin-top: 12px;
}
.attempt-error {
  color: var(--el-color-danger);
  overflow-wrap: anywhere;
}
.payloads {
  display: grid;
  gap: 4px;
}
.payloads dd + dt {
  margin-top: 4px;
}
.payload {
  max-height: 240px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.attachments .el-button {
  margin: 0;
}
.preview img {
  display: block;
  max-width: 100%;
  height: auto;
}
</style>
