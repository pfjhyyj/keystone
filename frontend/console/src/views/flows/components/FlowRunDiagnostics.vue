<script setup lang="ts">
import { onScopeDispose, ref, watch } from 'vue'
import { ApiError } from '../../../api/client'
import { flowApi, type Diagnostics, type DiagnosticSection, type Steps } from '../../../api/flows'
import { callStates } from '../../../api/models'
import ListPager from '../../../components/ListPager.vue'
import { usePageSize } from '../../../composables/usePagedList'
import { can, sessionVersion } from '../../../stores/session'
import InvocationDialog from '../../models/components/InvocationDialog.vue'
const props = defineProps<{ id: string; refresh: number; steps: Steps | null }>()
const emit = defineEmits<{ unavailable: [message: string] }>()
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
function position(workflow: string | null, step: number | null) {
  if (workflow === null || step === null) return '历史 Step 位置未记录'
  const record = props.steps?.items.find(
    (item) => item.workflow_id === workflow && item.step_id === step,
  )
  return `${record?.name ?? '步骤名称暂不可用'} · ${workflow} / ${step}`
}
function clearSection(section: DiagnosticSection) {
  if (section === 'models') models.value = null
  else files.value = null
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
  <section aria-label="关联模型调用">
    <h3>关联模型调用</h3>
    <p v-if="can('flow:read') && !can('model_invocation:read')">无查看模型调用权限</p>
    <el-alert v-else-if="errors.models" role="alert" type="error" :closable="false">{{
      errors.models
    }}</el-alert>
    <template v-else-if="models">
      <p v-if="!models.available">当前权限不可查看模型调用。</p>
      <p v-else-if="!loading.models && !models.items.length">暂无可见模型调用。</p>
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
      <ListPager
        v-if="models.available"
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
  <section aria-label="关联文件">
    <h3>关联文件</h3>
    <p>查看本次运行保留的文件标识与用途，内容访问由业务入口授权。</p>
    <el-alert v-if="errors.files" role="alert" type="error" :closable="false">{{
      errors.files
    }}</el-alert>
    <template v-if="files">
      <p v-if="!loading.files && !files.items.length">暂无文件引用。</p>
      <dl v-for="item in files.items" :key="`${item.file_id}:${item.slot}`">
        <dt>{{ item.file_id }}</dt>
        <dd>{{ item.slot }}</dd>
        <dd v-if="item.invocation_id">来源调用 {{ item.invocation_id }}</dd>
        <dd v-else>Run 级用途；未记录可见的模型来源。</dd>
      </dl>
      <ListPager
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
