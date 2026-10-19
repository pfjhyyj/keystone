<script setup lang="ts">
import { onMounted, onScopeDispose, ref } from 'vue'
import { can } from '../../stores/session'
import { flowApi, flowStates, ongoing, type FlowDefinition, type FlowRun } from '../../api/flows'
import { usePagedList } from '../../composables/usePagedList'
import FlowStartDialog from './components/FlowStartDialog.vue'
import FlowRunDialog from './components/FlowRunDialog.vue'
import ListPager from '../../components/ListPager.vue'
const status = ref(''),
  applied = ref(''),
  starting = ref(false),
  selected = ref(''),
  notice = ref('')
const enabled = ref(false),
  definitions = ref<FlowDefinition[]>([]),
  idempotencyDays = ref(7),
  catalogError = ref(''),
  catalogLoading = ref(false)
const { rows, page, loading, error, refresh, reload, search } = usePagedList(
  'flow:read',
  (offset) => flowApi.runs(offset, applied.value),
)
let alive = true,
  timer: ReturnType<typeof setInterval> | undefined
async function catalog() {
  if (!can('flow:start') || catalogLoading.value) return
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const result = await flowApi.definitions()
    if (alive) {
      enabled.value = result.enabled
      definitions.value = result.items
      idempotencyDays.value = result.idempotency_days
    }
  } catch (reason) {
    if (alive) {
      enabled.value = false
      definitions.value = []
      catalogError.value = reason instanceof Error ? reason.message : '目录不可用'
    }
  } finally {
    if (alive) catalogLoading.value = false
  }
}
function accepted(run: FlowRun) {
  starting.value = false
  notice.value = `已受理：${run.id}`
  search()
  if (can('flow:read')) selected.value = run.id
}
onMounted(() => {
  void catalog()
  timer = setInterval(() => {
    if (!starting.value && rows.value.some(ongoing)) void refresh()
  }, 3000)
})
onScopeDispose(() => {
  alive = false
  clearInterval(timer)
})
</script>
<template>
  <section aria-label="Flow 流程">
    <div class="heading">
      <div>
        <h1>Flow 流程</h1>
        <p>发起任务，查看执行过程与结果。调用方式由部署配置决定，真实模型可能产生费用。</p>
      </div>
    </div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <section v-if="can('flow:start')" aria-label="流程目录">
      <div class="heading">
        <h2>流程目录</h2>
        <div class="row-actions">
          <el-button
            :disabled="!enabled || !definitions.length || catalogLoading"
            @click="starting = true"
            native-type="button"
            type="primary"
          >
            发起流程</el-button
          ><el-button
            :disabled="catalogLoading"
            @click="catalog"
            native-type="button"
            type="default"
          >
            刷新流程目录
          </el-button>
        </div>
      </div>
      <el-alert v-if="catalogError" role="alert" type="error" show-icon :closable="false">{{
        catalogError
      }}</el-alert>
      <p v-else-if="!enabled">Flow 尚未启用；完成执行服务配置后刷新目录。</p>
      <p v-else>Flow 已启用，此配置不表示 Worker 已在线。等待执行时可检查 Worker。</p>
      <ul>
        <li v-for="definition in definitions" :key="definition.key">
          {{ definition.name }} · {{ definition.description }}
        </li>
      </ul>
      <p v-if="enabled && !definitions.length">没有符合当前权限的流程定义。</p>
    </section>
    <section v-if="can('flow:read')" aria-label="运行记录">
      <h2>运行记录</h2>
      <p>
        {{ can('flow:manage') ? '可见全部运行' : '仅显示本人运行' }}。未完成的可见运行每 3
        秒刷新，离开页面后停止。
      </p>
      <form
        class="module-form"
        @submit.prevent="
          () => {
            applied = status
            search()
          }
        "
      >
        <div class="form-field">
          <span class="form-label">运行状态</span
          ><AppSelect v-model="status" aria-label="运行状态"
            ><el-option value="" :label="'全部'" /><el-option
              v-for="(label, key) in flowStates"
              :key="key"
              :value="key"
              :label="label"
          /></AppSelect>
        </div>
        <el-button :disabled="loading" native-type="submit" type="default">查询流程</el-button
        ><el-button :disabled="loading" @click="reload" native-type="button" type="default">
          刷新流程
        </el-button>
      </form>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false"
        >{{ error }}；自动刷新时可能保留上次确认的数据。</el-alert
      >
      <p v-if="loading" role="status">正在加载运行…</p>
      <div class="table-scroll">
        <el-table
          :fit="false"
          native-scrollbar
          table-layout="auto"
          :data="rows"
          row-key="id"
          stripe
          class="data-table"
        >
          <el-table-column label="流程" min-width="180"
            ><template #default="{ row: row }"
              >{{ definitions.find((d) => d.key === row.definition_key)?.name ?? row.definition_key
              }}<small>{{ row.id }}</small></template
            ></el-table-column
          >
          <el-table-column label="受理时间" min-width="180"
            ><template #default="{ row: row }">{{
              new Date(row.created_at).toLocaleString()
            }}</template></el-table-column
          >
          <el-table-column label="状态" min-width="120"
            ><template #default="{ row: row }"
              ><StatusTag :status="row.status">{{
                row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status]
              }}</StatusTag></template
            ></el-table-column
          >
          <el-table-column label="操作" min-width="220"
            ><template #default="{ row: row }"
              ><el-button @click="selected = row.id" native-type="button" type="primary" link
                >查看运行</el-button
              ></template
            ></el-table-column
          >
          <template #empty><el-empty :image-size="64" description="暂无符合条件的运行" /></template
        ></el-table>
      </div>
      <ListPager :page="page" :count="rows.length" :loading="loading" @change="page = $event" />
    </section>
    <FlowStartDialog
      v-if="starting || (can('flow:start') && definitions.length)"
      :open="starting"
      :definitions="definitions"
      :enabled="enabled"
      :idempotency-days="idempotencyDays"
      @close="starting = false"
      @accepted="accepted"
    />
    <FlowRunDialog
      v-if="selected"
      :key="selected"
      :id="selected"
      @close="selected = ''"
      @changed="reload"
    />
  </section>
</template>
