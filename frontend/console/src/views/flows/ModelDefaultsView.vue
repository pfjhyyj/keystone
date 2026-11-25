<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  flowModelDefaultsApi,
  type FlowModelDefault,
  type ModelChoice,
} from '../../api/flowModelDefaults'
import { useAction } from '../../composables/useAction'
import { can } from '../../stores/session'
import AppSelect from '../../components/AppSelect.vue'

const rows = ref<FlowModelDefault[]>([])
const choices = ref<Record<string, ModelChoice[]>>({})
const drafts = ref<Record<string, string>>({})
const loaded = ref(false)
const { busy, error, notice, run } = useAction()
const key = (row: FlowModelDefault) => `${row.definition_key}/${row.role}`
// One form per Flow, holding each of its model roles.
const groups = computed(() => {
  const result = new Map<string, { name: string; rows: FlowModelDefault[] }>()
  for (const row of rows.value) {
    const group = result.get(row.definition_key) ?? { name: row.definition_name, rows: [] }
    group.rows.push(row)
    result.set(row.definition_key, group)
  }
  return [...result.entries()]
})
const changed = (group: FlowModelDefault[]) =>
  group.some((row) => drafts.value[key(row)] && drafts.value[key(row)] !== row.model_key)
const usable = (row: FlowModelDefault, model: string) =>
  choices.value[key(row)]?.some((item) => item.key === model) ?? false
// A saved model that is no longer usable stays visible so the administrator sees why it must change.
function options(row: FlowModelDefault) {
  const list = choices.value[key(row)] ?? []
  return row.model_key && !usable(row, row.model_key)
    ? [...list, { key: row.model_key, name: `${row.model_key}（不可用）` }]
    : list
}

async function load() {
  rows.value = await flowModelDefaultsApi.list()
  drafts.value = Object.fromEntries(rows.value.map((row) => [key(row), row.model_key ?? '']))
  const lists = await Promise.all(
    rows.value.map(async (row) => [
      key(row),
      await flowModelDefaultsApi.models(row.definition_key, row.role),
    ]),
  )
  choices.value = Object.fromEntries(lists)
  loaded.value = true
}
async function save(group: FlowModelDefault[]) {
  await run(async () => {
    for (const row of group)
      if (drafts.value[key(row)] !== row.model_key)
        await flowModelDefaultsApi.save(row.definition_key, row.role, drafts.value[key(row)]!)
    notice.value = '默认模型已保存'
    await load()
  })
}
onMounted(() => run(load))
</script>
<template>
  <section aria-label="模型用途">
    <div class="heading">
      <h1>模型用途</h1>
      <p>业务发起流程时未指定模型的用途，使用这里设定的默认模型；已发起的运行不受修改影响。</p>
    </div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <p v-if="loaded && !groups.length" class="muted">当前没有需要模型的流程。</p>
    <form
      v-for="[definitionKey, group] in groups"
      :key="definitionKey"
      class="work-panel model-defaults"
      @submit.prevent="save(group.rows)"
    >
      <div class="panel-heading">
        <h2>{{ group.name }}</h2>
        <small class="muted">{{ definitionKey }}</small>
      </div>
      <div class="model-defaults-fields">
        <label v-for="row in group.rows" :key="key(row)" class="form-field"
          >{{ row.role_label
          }}<AppSelect
            v-model="drafts[key(row)]"
            :aria-label="`${group.name}的${row.role_label}模型`"
            placeholder="未设置"
            filterable
            required
            ><el-option
              v-for="model in options(row)"
              :key="model.key"
              :value="model.key"
              :label="model.name"
              :disabled="!usable(row, model.key)"
          /></AppSelect>
          <small v-if="choices[key(row)] && !choices[key(row)]!.length" class="muted"
            >没有可用于此用途的模型<template v-if="can('model:manage')"
              >，先在<RouterLink to="/models">模型接入</RouterLink>中添加并启用</template
            ></small
          >
        </label>
      </div>
      <div class="row-actions model-defaults-actions">
        <el-button type="primary" native-type="submit" :disabled="busy || !changed(group.rows)"
          >保存</el-button
        >
      </div>
    </form>
  </section>
</template>
<style scoped>
.model-defaults-fields {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
  padding: 16px 20px 0;
}
.model-defaults-actions {
  padding: 16px 20px 20px;
}
</style>
