<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { ApiError } from '../../../api/client'
import { flowApi, type Diagnostics, type DiagnosticSection, type Steps } from '../../../api/flows'
import { downloadFile } from '../../../api/files'
import { callStates } from '../../../api/models'
import { can, sessionVersion } from '../../../stores/session'
import InvocationDialog from '../../models/components/InvocationDialog.vue'
const props = defineProps<{ id: string; refresh: number; steps: Steps | null }>()
const emit = defineEmits<{ unavailable: [message: string] }>()
const budget = ref<Diagnostics['budget']>(null),
  models = ref<Diagnostics['models']>(null),
  files = ref<Diagnostics['files']>(null),
  selected = ref(''),
  downloadError = ref(''),
  downloading = ref(false)
const errors = ref({ budget: '', models: '', files: '' })
const loading = ref({ budget: false, models: false, files: false })
const generations = { budget: 0, models: 0, files: 0 }
let alive = true
const labels = {
  model_requests: '模型请求 · 已预留',
  tool_calls: '工具操作 · 已记账',
  iterations: '迭代 · 已记账',
  file_count: '文件数 · 已预留',
  file_bytes: '文件字节 · 已预留',
  input_tokens: '输入 token · 已报告',
  output_tokens: '输出 token · 已预留',
} as const
const usage = computed(() =>
  Object.entries(labels).map(([key, label]) => {
    const consumed = BigInt(budget.value?.counters?.[key as keyof typeof labels] ?? '0')
    const limit = BigInt(budget.value?.limits?.[key as keyof typeof labels] ?? '0')
    const remaining = limit - consumed
    return {
      label,
      consumed: consumed.toString(),
      limit: limit.toString(),
      remaining: (remaining > 0n ? remaining : 0n).toString(),
    }
  }),
)
function position(workflow: string | null, step: number | null) {
  if (workflow === null || step === null) return '历史 Step 位置未记录'
  const record = props.steps?.items.find(
    (item) => item.workflow_id === workflow && item.step_id === step,
  )
  return `${record?.name ?? '步骤名称暂不可用'} · ${workflow} / ${step}`
}
async function load(section: DiagnosticSection, more = false) {
  const token = ++generations[section]
  const current = () =>
    alive &&
    token === generations[section] &&
    can('flow:read') &&
    (section !== 'models' || can('model_invocation:read')) &&
    (section !== 'files' || can('file:inspect'))
  if (!can('flow:read')) return
  if (section === 'models' && !can('model_invocation:read')) {
    models.value = null
    selected.value = ''
    loading.value.models = false
    errors.value.models = '无查看模型调用权限'
    return
  }
  if (section === 'files' && !can('file:inspect')) {
    files.value = null
    loading.value.files = false
    errors.value.files = '无查看文件元数据权限'
    return
  }
  const offset = more
    ? section === 'models'
      ? models.value?.next_offset
      : files.value?.next_offset
    : 0
  if (offset === null || offset === undefined) return
  loading.value[section] = true
  try {
    const result = await flowApi.diagnostics(props.id, section, offset)
    if (!current()) return
    if (section === 'budget') budget.value = result.budget
    else if (section === 'models')
      models.value = result.models && {
        ...result.models,
        items: [...(more ? (models.value?.items ?? []) : []), ...result.models.items],
      }
    else
      files.value = result.files && {
        ...result.files,
        items: [...(more ? (files.value?.items ?? []) : []), ...result.files.items],
      }
    errors.value[section] = ''
  } catch (reason) {
    if (!current()) return
    // Keep independent sections usable, but never keep data after authority is lost.
    if (section === 'budget') budget.value = null
    else if (section === 'models') {
      models.value = null
      selected.value = ''
    } else files.value = null
    errors.value[section] = reason instanceof Error ? reason.message : '诊断暂不可用'
    if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
      budget.value = null
      models.value = null
      files.value = null
      selected.value = ''
      emit('unavailable', errors.value[section])
    }
  } finally {
    if (alive && token === generations[section]) loading.value[section] = false
  }
}
async function download(id: string) {
  if (downloading.value) return
  downloading.value = true
  downloadError.value = ''
  try {
    await downloadFile(id, id)
  } catch (reason) {
    if (alive) downloadError.value = reason instanceof Error ? reason.message : '文件下载失败'
  } finally {
    if (alive) downloading.value = false
  }
}
watch(
  () => [
    props.refresh,
    sessionVersion.value,
    can('flow:read'),
    can('model_invocation:read'),
    can('file:inspect'),
  ],
  (value, previous) => {
    // Polling changes only refresh; authority changes must still clear private details.
    if (!previous || value.slice(1).some((item, index) => item !== previous[index + 1]))
      selected.value = ''
    if (!can('flow:read')) {
      budget.value = null
      models.value = null
      files.value = null
      for (const section of ['budget', 'models', 'files'] as const) {
        generations[section]++
        loading.value[section] = false
      }
      return
    }
    void Promise.all((['budget', 'models', 'files'] as const).map((section) => load(section)))
  },
  { immediate: true },
)
onScopeDispose(() => {
  alive = false
  for (const section of ['budget', 'models', 'files'] as const) generations[section]++
})
</script>
<template>
  <section aria-label="运行预算">
    <h3>运行累计预算</h3>
    <p>预留额跨恢复累计；已预留的输出 token 不是已报告用量，预算也不表示实际费用。</p>
    <p v-if="loading.budget" role="status">正在查询预算…</p>
    <el-alert v-if="errors.budget" role="alert" type="error" :closable="false">{{
      errors.budget
    }}</el-alert>
    <template v-else-if="budget">
      <p v-if="!budget.recorded">历史运行未记录此版本累计预算。</p>
      <template v-else>
        <dl v-for="item in usage" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd>
            {{ item.consumed }} / {{ item.limit }}；剩余
            {{ item.remaining }}
          </dd>
        </dl>
        <p>
          未结清文本请求：{{ budget.counters?.unreported_text_requests }}；输入 token
          尚未报告，不等于零用量。
        </p>
        <p>
          受控并发上限 {{ budget.limits?.concurrent_operations }}；临时空间上限
          {{ budget.limits?.temporary_bytes }} B。
        </p>
      </template>
    </template>
  </section>
  <section aria-label="关联模型调用">
    <h3>关联模型调用</h3>
    <el-alert v-if="errors.models" role="alert" type="error" :closable="false">{{
      errors.models
    }}</el-alert>
    <template v-else-if="models">
      <p v-if="!models.available">当前权限不可查看模型调用。</p>
      <p v-else-if="!models.items.length">暂无可见模型调用。</p>
      <dl v-for="item in models.items" :key="item.invocation_id">
        <dt>
          <el-button
            native-type="button"
            link
            class="flow-invocation-link"
            @click="selected = item.invocation_id"
            >{{ item.invocation_id }}</el-button
          >
        </dt>
        <dd>
          {{ item.operation }} · {{ callStates[item.status] }} · 角色
          {{ item.model_role ?? '未记录' }} · 节点 {{ item.node_key ?? '未记录' }}
        </dd>
        <dd>{{ position(item.workflow_id, item.step_id) }}</dd>
        <dd v-if="item.agent_key">
          Agent {{ item.agent_key }} / {{ item.agent_call_id ?? '未记录' }}
        </dd>
        <dd v-if="item.error_code">错误码 {{ item.error_code }}</dd>
      </dl>
      <el-button
        v-if="models.truncated"
        :disabled="loading.models"
        native-type="button"
        @click="load('models', true)"
        >加载更多调用</el-button
      >
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
  <section aria-label="关联文件">
    <h3>关联文件</h3>
    <p>文件引用只表示逻辑用途，读取内容仍检查当前文件授权。</p>
    <el-alert v-if="errors.files || downloadError" role="alert" type="error" :closable="false">{{
      errors.files || downloadError
    }}</el-alert>
    <template v-if="files">
      <p v-if="!files.available">当前权限不可查看文件元数据。</p>
      <p v-else-if="!files.items.length">暂无可见文件引用。</p>
      <dl v-for="item in files.items" :key="`${item.file_id}:${item.slot}`">
        <dt>{{ item.file_id }}</dt>
        <dd>{{ item.slot }}</dd>
        <dd v-if="item.invocation_id">来源调用 {{ item.invocation_id }}</dd>
        <dd v-else>Run 级用途；未记录可见的模型来源。</dd>
        <dd v-if="can('file:content_read')">
          <el-button :disabled="downloading" native-type="button" @click="download(item.file_id)"
            >下载文件</el-button
          >
        </dd>
      </dl>
      <el-button
        v-if="files.truncated"
        :disabled="loading.files"
        native-type="button"
        @click="load('files', true)"
        >加载更多文件</el-button
      >
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
.flow-invocation-link {
  max-width: 100%;
  height: auto;
  white-space: normal;
  text-align: left;
}
.flow-invocation-link :deep(span) {
  overflow-wrap: anywhere;
  min-width: 0;
}
</style>
