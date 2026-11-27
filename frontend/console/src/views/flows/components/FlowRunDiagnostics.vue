<script setup lang="ts">
import { onScopeDispose, ref, watch } from 'vue'
import { ApiError } from '../../../api/client'
import {
  flowApi,
  type Diagnostics,
  type DiagnosticSection,
  type InvocationDiagnostic,
  type Steps,
} from '../../../api/flows'
import { callStates, operationLabels } from '../../../api/models'
import ListPager from '../../../components/ListPager.vue'
import { usePageSize } from '../../../composables/usePagedList'
import { can, sessionVersion } from '../../../stores/session'
import { formatDuration } from '../../../utils/duration'
import InvocationDialog from '../../models/components/InvocationDialog.vue'

const props = defineProps<{
  id: string
  refresh: number
  steps: Steps | null
  visibleSection?: DiagnosticSection | 'none'
}>()
const emit = defineEmits<{
  unavailable: [message: string]
  'models-loaded': [items: InvocationDiagnostic[]]
}>()

const models = ref<Diagnostics['models']>(null),
  files = ref<Diagnostics['files']>(null),
  selected = ref('')
const sections = ['models', 'files'] as const
const errors = ref({ models: '', files: '' })
const loading = ref({ models: false, files: false })
const pages = ref({ models: 0, files: 0 })
const sizes = {
  models: usePageSize('flow.diagnostics.models'),
  files: usePageSize('flow.diagnostics.files'),
}
const modelSize = sizes.models.size
const fileSize = sizes.files.size
const generations = { models: 0, files: 0 }

function stepName(workflow: string | null, step: number | null) {
  return props.steps?.items.find((item) => item.workflow_id === workflow && item.step_id === step)
    ?.name
}

function fileSlotLabel(slot: string) {
  if (slot === 'input') return '运行输入文件'
  if (slot === 'output') return '运行输出文件'
  if (slot.startsWith('artifact.model.')) return '模型产物'
  if (slot.startsWith('artifact.prepared.')) return '预处理图片'
  if (slot.startsWith('artifact.crop.')) return '参考裁片'
  if (slot === 'artifact.hem') return '裙长示意图'
  if (slot.startsWith('artifact.')) return `产物 · ${slot.slice('artifact.'.length)}`
  return slot
}

function showPager(
  section: DiagnosticSection,
  data: { items: unknown[]; total?: number; truncated: boolean } | null,
) {
  if (!data) return false
  const size = sizes[section].size.value
  return pages.value[section] > 0 || data.truncated || (data.total ?? data.items.length) > size
}

const errorLabels: Record<string, string> = {
  model_timeout: '模型调用超时',
  model_task_timeout: '上游任务超时',
  model_rate_limited: '模型服务限流',
  model_authentication_failed: '模型渠道认证失败',
  model_insufficient_credits: '模型渠道余额不足',
  model_response_unknown: '上游结果待确认',
  model_result_unavailable: '模型结果不可用',
  model_invalid_output: '模型返回格式不符合要求',
  model_upstream_error: '上游模型服务出错',
}

function clearSection(section: DiagnosticSection) {
  if (section === 'models') {
    models.value = null
    emit('models-loaded', [])
  } else files.value = null
}

function resetScope() {
  selected.value = ''
  for (const section of sections) {
    generations[section]++
    clearSection(section)
    pages.value[section] = 0
    loading.value[section] = false
    errors.value[section] = ''
  }
}

function allowed(section: DiagnosticSection) {
  return can('flow:read') && (section !== 'models' || can('model_invocation:read'))
}

async function load(section: DiagnosticSection, preserve = false) {
  if (!allowed(section)) return
  const token = ++generations[section]
  const id = props.id
  const session = sessionVersion.value
  const page = pages.value[section]
  const size = sizes[section].size.value
  const current = () =>
    token === generations[section] &&
    id === props.id &&
    session === sessionVersion.value &&
    allowed(section)
  if (!preserve) {
    if (section === 'models' && models.value) models.value = { ...models.value, items: [] }
    if (section === 'files' && files.value) files.value = { ...files.value, items: [] }
  }
  errors.value[section] = ''
  loading.value[section] = true
  try {
    const result = await flowApi.diagnostics(id, section, page * size, size)
    if (!current()) return
    const total = result[section]?.total
    if (total !== undefined) {
      const last = Math.max(0, Math.ceil(total / size) - 1)
      if (page > last) {
        pages.value[section] = last
        await load(section)
        return
      }
    }
    if (section === 'models') {
      models.value = result.models
      emit('models-loaded', result.models?.available ? result.models.items : [])
      if (!result.models?.available) selected.value = ''
    } else files.value = result.files
  } catch (reason) {
    if (!current()) return
    const message = reason instanceof Error ? reason.message : '诊断暂不可用'
    if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
      // A sibling request may still be in flight when Run access is lost.
      resetScope()
      errors.value[section] = message
      emit('unavailable', message)
    } else {
      clearSection(section)
      if (section === 'models') selected.value = ''
      errors.value[section] = message
    }
  } finally {
    if (token === generations[section]) loading.value[section] = false
  }
}

function goToPage(section: DiagnosticSection, target: number) {
  pages.value[section] = target
  void load(section)
}

function changeSize(section: DiagnosticSection, value: number) {
  const { size, setSize } = sizes[section]
  const previous = size.value
  setSize(value)
  if (previous !== size.value) goToPage(section, 0)
}

function openInvocation(invocationId: string) {
  if (can('model_invocation:read')) selected.value = invocationId
}

defineExpose({ openInvocation })

watch(
  () =>
    [
      props.refresh,
      props.id,
      sessionVersion.value,
      can('flow:read'),
      can('model_invocation:read'),
    ] as const,
  ([, ...scope], previous) => {
    const scopeChanged = !previous || scope.some((value, index) => value !== previous[index + 1])
    if (scopeChanged) resetScope()
    for (const section of sections) void load(section, !scopeChanged)
  },
  { immediate: true },
)

onScopeDispose(() => {
  for (const section of sections) generations[section]++
})
</script>

<template>
  <section v-show="!visibleSection || visibleSection === 'models'" aria-label="关联模型调用">
    <h3>关联模型调用</h3>
    <p v-if="can('flow:read') && !can('model_invocation:read')">无查看模型调用权限</p>
    <el-alert v-else-if="errors.models" role="alert" type="error" :closable="false">{{
      errors.models
    }}</el-alert>
    <template v-else-if="models">
      <p v-if="!models.available">当前权限不可查看模型调用。</p>
      <p v-else-if="!loading.models && !models.items.length">暂无可见模型调用。</p>
      <article v-for="item in models.items" :key="item.invocation_id" class="invocation-summary">
        <div class="invocation-heading">
          <strong>{{ item.model_name || item.provider_model || '模型名称未记录' }}</strong>
          <StatusTag :status="item.status">{{ callStates[item.status] }}</StatusTag>
        </div>
        <p class="invocation-meta">
          {{ operationLabels[item.operation] ?? item.operation
          }}<template v-if="item.channel_name"> · {{ item.channel_name }}</template
          ><template v-if="formatDuration(item.duration_ms)">
            · 耗时 {{ formatDuration(item.duration_ms) }}</template
          >
        </p>
        <p v-if="stepName(item.workflow_id, item.step_id) || item.node_key" class="invocation-meta">
          步骤：{{ stepName(item.workflow_id, item.step_id) || item.node_key }}
        </p>
        <p v-if="item.error_code">{{ errorLabels[item.error_code] ?? '模型调用遇到问题' }}</p>
        <el-button
          native-type="button"
          link
          type="primary"
          class="flow-invocation-link"
          @click="selected = item.invocation_id"
          >查看调用</el-button
        >
        <details>
          <summary>调用技术标识</summary>
          <dl>
            <dt>调用 ID</dt>
            <dd>{{ item.invocation_id }}</dd>
            <template v-if="item.provider_model">
              <dt>上游模型</dt>
              <dd>{{ item.provider_model }}</dd>
            </template>
            <template v-if="item.model_role">
              <dt>模型角色</dt>
              <dd>{{ item.model_role }}</dd>
            </template>
            <dt>步骤位置</dt>
            <dd>
              {{
                item.workflow_id !== null && item.step_id !== null
                  ? `${item.workflow_id} / ${item.step_id}`
                  : '历史 Step 位置未记录'
              }}
            </dd>
            <template v-if="item.agent_key">
              <dt>Agent</dt>
              <dd>{{ item.agent_key }} / {{ item.agent_call_id ?? '未记录' }}</dd>
            </template>
            <template v-if="item.error_code">
              <dt>错误码</dt>
              <dd>{{ item.error_code }}</dd>
            </template>
          </dl>
        </details>
      </article>
      <ListPager
        v-if="models.available && showPager('models', models)"
        :page="pages.models"
        :count="models.items.length"
        :total="models.total"
        :size="modelSize"
        :loading="loading.models"
        @change="goToPage('models', $event)"
        @size-change="changeSize('models', $event)"
      />
    </template>
    <el-button
      v-if="errors.models"
      :disabled="loading.models"
      native-type="button"
      @click="load('models')"
      >重试调用诊断</el-button
    >
    <p v-if="loading.models" role="status">正在查询调用…</p>
  </section>
  <section v-show="!visibleSection || visibleSection === 'files'" aria-label="关联文件">
    <h3>关联文件</h3>
    <p>这里列出本次运行的文件用途；查看文件内容请前往步骤详情或有权限的业务入口。</p>
    <el-alert v-if="errors.files" role="alert" type="error" :closable="false">{{
      errors.files
    }}</el-alert>
    <template v-if="files">
      <p v-if="!loading.files && !files.items.length">暂无文件引用。</p>
      <details v-for="item in files.items" :key="`${item.file_id}:${item.slot}`">
        <summary>{{ fileSlotLabel(item.slot) }}</summary>
        <dl>
          <dt>文件 ID</dt>
          <dd>{{ item.file_id }}</dd>
          <dt>用途标识</dt>
          <dd>{{ item.slot }}</dd>
          <dt>来源调用</dt>
          <dd>{{ item.invocation_id ?? '无可见的模型来源' }}</dd>
        </dl>
      </details>
      <ListPager
        v-if="showPager('files', files)"
        :page="pages.files"
        :count="files.items.length"
        :total="files.total"
        :size="fileSize"
        :loading="loading.files"
        @change="goToPage('files', $event)"
        @size-change="changeSize('files', $event)"
      />
    </template>
    <el-button
      v-if="errors.files"
      :disabled="loading.files"
      native-type="button"
      @click="load('files')"
      >重试文件诊断</el-button
    >
    <p v-if="loading.files" role="status">正在查询文件…</p>
  </section>
  <InvocationDialog
    v-if="selected && can('model_invocation:read')"
    :key="selected"
    :id="selected"
    @close="selected = ''"
  />
</template>

<style scoped>
.invocation-summary {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  overflow-wrap: anywhere;
}
.invocation-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.invocation-meta {
  margin: 4px 0;
  color: var(--el-text-color-secondary);
}
</style>
