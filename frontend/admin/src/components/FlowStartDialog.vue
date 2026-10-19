<script setup lang="ts">
import { ref, watch } from 'vue'
import { can } from '../access'
import { request } from '../auth'
import { fileApi, uuidPattern } from '../file-api'
import type { FlowDefinition, FlowRun } from '../flow-api'
import { usePagedList } from '../composables/usePagedList'
import { useAction } from '../composables/useAction'
import AppDialog from './AppDialog.vue'
import ListPager from './ListPager.vue'
import ModelPicker from './ModelPicker.vue'
const props = defineProps<{ open: boolean; definitions: FlowDefinition[]; enabled: boolean; idempotencyDays: number }>()
const modelMode = ref('')
const emit = defineEmits<{ close: []; accepted: [run: FlowRun] }>()
const definitionKey = ref(''), fileId = ref(''), modelKey = ref(''), json = ref('{}'), requestKey = ref(crypto.randomUUID())
const { busy, error, notice, run } = useAction()
function newRequest() { requestKey.value = crypto.randomUUID(); notice.value = '已准备新的运行请求' }
const files = usePagedList('file:inspect', fileApi.list)
watch(() => props.definitions, values => { if (!definitionKey.value) definitionKey.value = values[0]?.key ?? '' }, { immediate: true })
watch(definitionKey, () => { fileId.value = ''; modelKey.value = ''; json.value = '{}' })
watch([definitionKey, fileId, modelKey, json], () => { requestKey.value = crypto.randomUUID(); notice.value = '' }, { flush: 'sync' })
async function submit() { if (!props.enabled || !can('flow:start')) return; await run(async () => {
  let input: unknown
  if (definitionKey.value === 'document.review') input = { source_file_id: fileId.value, model_key: modelKey.value }
  else { try { input = JSON.parse(json.value) } catch { throw new Error('流程输入须为有效 JSON 对象') } }
  if (!input || typeof input !== 'object' || Array.isArray(input) || new TextEncoder().encode(JSON.stringify(input)).length > 65536) throw new Error('流程输入须为不超过 64 KiB 的 JSON 对象')
  const accepted = await request<FlowRun>('/flow-runs', 'POST', { definition_key: definitionKey.value, request_key: requestKey.value, input })
  notice.value = `已受理：${accepted.id}`; emit('accepted', accepted)
}) }
</script>
<template>
  <AppDialog v-if="open" title="发起流程" :busy="busy" @close="$emit('close')">
    <p>流程按部署配置执行；启用 Flow 不表示 Worker 已在线。</p><p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <form class="dialog-form" @submit.prevent="submit"><label>流程<select v-model="definitionKey" aria-label="流程" :disabled="busy" required><option v-for="d in definitions" :key="d.key" :value="d.key">{{ d.name }}</option></select></label><p>{{ definitions.find(d => d.key === definitionKey)?.description }}</p>
      <template v-if="definitionKey === 'document.review'">
        <template v-if="can('file:inspect')"><label>可见源文件<select v-model="fileId" aria-label="可见源文件" :disabled="busy || files.loading.value"><option value="">选择文件或填写下方 ID</option><option v-if="fileId && !files.rows.value.some(row => row.id === fileId)" :value="fileId">{{ fileId }}（已选）</option><option v-for="row in files.rows.value.filter(row => row.status === 'active')" :key="row.id" :value="row.id">{{ row.name }}</option></select></label><ListPager v-if="files.page.value || files.rows.value.length >= 50" :page="files.page.value" :count="files.rows.value.length" :loading="busy || files.loading.value" @change="files.page.value = $event" /><button type="button" class="secondary" :disabled="busy || files.loading.value" @click="files.reload">刷新源文件</button><p v-if="files.error.value" role="alert">{{ files.error.value }}</p></template>
        <label>源文件 ID<input v-model="fileId" :pattern="uuidPattern" required :disabled="busy" placeholder="已知文件 UUID"></label><p>文件须可读取且为不超过 16 KiB 的 UTF-8 文本；可见文件不自动代表有内容读取权。</p>
        <ModelPicker v-model="modelKey" :disabled="busy" @mode="modelMode = $event" /><p v-if="modelMode === 'live'">本流程将调用真实模型，可能产生费用；取消不撤销已发生的调用。</p><p v-else-if="modelMode === 'mock'">本流程使用模拟渠道，费用仅用于验证。</p>
      </template>
      <template v-else><label>流程输入 JSON<textarea v-model="json" rows="8" :disabled="busy" required></textarea></label><details><summary>输入格式</summary><pre class="audit-details">{{ JSON.stringify(definitions.find(d => d.key === definitionKey)?.input_schema, null, 2) }}</pre></details></template>
      <p>保留相同输入重复提交会在 {{ idempotencyDays }} 天去重窗口内返回原运行。未确认受理时可原样重试；重新执行请准备新请求。关闭弹窗会保留本页请求，离开页面后不再保留。</p>
      <div class="row-actions"><button :disabled="busy || !enabled || !definitionKey">提交流程</button><button type="button" class="secondary" :disabled="busy" @click="newRequest">准备新的运行</button></div>
    </form>
  </AppDialog>
</template>
