<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { can } from '../../../stores/session'
import { request } from '../../../api/client'
import { flowForms } from '../forms'
import type { FlowDefinition, FlowRun } from '../../../api/flows'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import ModelPicker from '../../../components/models/ModelPicker.vue'

const props = defineProps<{
  open: boolean
  definitionKey?: string
  definitions: FlowDefinition[]
  idempotencyDays: number
}>()
const emit = defineEmits<{ close: []; accepted: [run: FlowRun] }>()
const definitionKey = ref(''),
  input = ref<Record<string, unknown>>({}),
  json = ref('{}'),
  requestKey = ref(crypto.randomUUID())
const bindings = ref<Record<string, string>>({})
const selected = computed(() => props.definitions.find((d) => d.key === definitionKey.value))
const form = computed(() => flowForms[definitionKey.value])
const { busy, error, notice, run } = useAction()

function newRequest() {
  requestKey.value = crypto.randomUUID()
  notice.value = '已准备新的运行请求'
}
watch(
  () => props.definitions,
  (values) => {
    if (!definitionKey.value) definitionKey.value = values[0]?.key ?? ''
  },
  { immediate: true },
)
watch(definitionKey, () => {
  input.value = {}
  json.value = '{}'
  bindings.value = Object.fromEntries((selected.value?.model_roles ?? []).map((r) => [r.key, '']))
})
watch(
  () => selected.value?.model_roles.map((role) => role.key) ?? [],
  (keys) => {
    const current = Object.keys(bindings.value)
    if (current.length !== keys.length || keys.some((key) => !current.includes(key)))
      bindings.value = Object.fromEntries(
        keys.map((key) => [
          key,
          typeof bindings.value[key] === 'string' ? bindings.value[key] : '',
        ]),
      )
  },
  { immediate: true },
)
watch(
  () => [props.open, props.definitionKey] as const,
  ([open, key]) => {
    if (open && key && props.definitions.some((definition) => definition.key === key))
      definitionKey.value = key
  },
  { immediate: true },
)
function requestChanged() {
  requestKey.value = crypto.randomUUID()
  notice.value = ''
}
watch([definitionKey, input, json, bindings], requestChanged, { deep: true, flush: 'sync' })
watch(() => selected.value?.revision, requestChanged, { flush: 'sync' })

async function submit() {
  if (!can('flow:start')) return
  await run(async () => {
    let value: unknown = input.value
    if (!form.value) {
      try {
        value = JSON.parse(json.value)
      } catch {
        throw new Error('流程输入须为有效 JSON 对象')
      }
    }
    if (
      !value ||
      typeof value !== 'object' ||
      Array.isArray(value) ||
      new TextEncoder().encode(JSON.stringify(value)).length > 65536
    )
      throw new Error('流程输入须为不超过 64 KiB 的 JSON 对象')
    if ((selected.value?.model_roles ?? []).some((r) => !bindings.value[r.key]))
      throw new Error('请为每个角色选择模型')
    const accepted = await request<FlowRun>('/flow-runs', 'POST', {
      definition_key: definitionKey.value,
      request_key: requestKey.value,
      definition_revision: selected.value?.revision,
      model_bindings: bindings.value,
      input: value,
    })
    notice.value = `已受理：${accepted.id}`
    emit('accepted', accepted)
  })
}
</script>

<template>
  <AppDialog v-if="open" title="发起流程" :busy="busy" @close="$emit('close')">
    <p>流程按部署配置执行；启用 Flow 不表示 Worker 已在线。</p>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <form class="dialog-form" @submit.prevent="submit">
      <div class="form-field">
        <span class="form-label">流程</span
        ><AppSelect v-model="definitionKey" aria-label="流程" :disabled="busy" required
          ><el-option v-for="d in definitions" :key="d.key" :value="d.key" :label="d.name"
        /></AppSelect>
      </div>
      <p>{{ definitions.find((d) => d.key === definitionKey)?.description }}</p>
      <p v-if="selected?.revision">契约版本：{{ selected.revision }}</p>
      <div v-for="role in selected?.model_roles ?? []" :key="role.key" class="form-field">
        <ModelPicker
          v-model="bindings[role.key]!"
          :label="role.label + '模型'"
          :operation="role.operation"
          :delivery="role.delivery ?? 'sync'"
          :disabled="busy"
        />
      </div>
      <component :is="form" v-if="form" :key="definitionKey" v-model="input" :disabled="busy" />
      <template v-else>
        <label
          >流程输入 JSON<el-input
            v-model="json"
            :rows="8"
            :disabled="busy"
            required
            type="textarea"
          />
        </label>
        <details>
          <summary>输入格式</summary>
          <pre class="audit-details">{{
            JSON.stringify(definitions.find((d) => d.key === definitionKey)?.input_schema, null, 2)
          }}</pre>
        </details>
      </template>
      <p>
        保留相同输入重复提交会在
        {{ idempotencyDays }}
        天去重窗口内返回原运行。未确认受理时可原样重试；重新执行请准备新请求。关闭弹窗会保留本页请求，离开页面后不再保留。
      </p>
      <div class="row-actions">
        <el-button :disabled="busy || !definitionKey" native-type="submit" type="primary"
          >提交流程</el-button
        ><el-button :disabled="busy" @click="newRequest" native-type="button" type="default">
          准备新的运行
        </el-button>
      </div>
    </form>
  </AppDialog>
</template>
