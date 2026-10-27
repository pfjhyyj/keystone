<script setup lang="ts">
import { computed, inject, onMounted, onScopeDispose, ref } from 'vue'
import { RouterLink, routeLocationKey, routerKey } from 'vue-router'
import { can } from '../../stores/session'
import { flowApi, flowStates, ongoing, type FlowDefinition, type FlowRun } from '../../api/flows'
import { usePagedList } from '../../composables/usePagedList'
import FlowStartDialog from './components/FlowStartDialog.vue'
import FlowRunDialog from './components/FlowRunDialog.vue'
import FlowCatalog from './components/FlowCatalog.vue'
import ListPager from '../../components/ListPager.vue'
const status = ref(''),
  applied = ref(''),
  starting = ref(false),
  startingKey = ref(''),
  selected = ref(''),
  notice = ref('')
const definitions = ref<FlowDefinition[]>([]),
  idempotencyDays = ref(7),
  catalogError = ref(''),
  catalogLoading = ref(false)
const { rows, page, loading, error, refresh, reload, search } = usePagedList(
  'flow:read',
  (offset) => flowApi.runs(offset, applied.value),
)
const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined),
  localTab = ref(can('flow:read') ? 'runs' : 'catalog')
const activeTab = computed({
  get: () =>
    [...(can('flow:read') ? ['runs'] : []), ...(can('flow:start') ? ['catalog'] : [])].includes(
      String(route?.query.tab),
    )
      ? String(route?.query.tab)
      : localTab.value,
  set: (value: string) => {
    localTab.value = value
    if (router) void router.replace({ query: { ...route?.query, tab: value } })
  },
})
let alive = true,
  timer: ReturnType<typeof setInterval> | undefined
async function catalog() {
  if (!can('flow:start') || catalogLoading.value) return
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const result = await flowApi.definitions()
    if (alive) {
      definitions.value = result.items
      idempotencyDays.value = result.idempotency_days
    }
  } catch (reason) {
    if (alive) {
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
function start(key: string) {
  if (!can('flow:start') || catalogLoading.value) return
  startingKey.value = key
  starting.value = true
}
function filter(value = status.value) {
  status.value = value
  applied.value = value
  search()
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
        <p>选择流程发起任务，跟踪执行步骤、结果与费用。使用模型的流程可能产生费用。</p>
      </div>
    </div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-tabs v-model="activeTab"
      ><el-tab-pane v-if="can('flow:read')" label="运行记录" name="runs" /><el-tab-pane
        v-if="can('flow:start')"
        label="流程目录"
        name="catalog"
    /></el-tabs>
    <section v-show="activeTab === 'catalog'" v-if="can('flow:start')" aria-label="流程目录">
      <div class="heading">
        <h2>流程目录</h2>
        <div class="row-actions">
          <el-button
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
      <p v-else-if="catalogLoading" role="status">正在加载流程目录…</p>
      <p v-else>
        任务由 Worker 执行，长时间等待时可到
        <RouterLink to="/platform">平台状态</RouterLink>查看 Worker 就绪状态和最近心跳。
      </p>
      <FlowCatalog :definitions="definitions" :loading="catalogLoading" @start="start" />
      <p v-if="!catalogLoading && !catalogError && !definitions.length">
        没有符合当前权限的流程定义。
      </p>
    </section>
    <section v-show="activeTab === 'runs'" v-if="can('flow:read')" aria-label="运行记录">
      <h2>运行记录</h2>
      <p>
        {{ can('flow:manage') ? '可见全部运行' : '仅显示本人运行' }}。未完成的可见运行每 3
        秒刷新，离开页面后停止。
      </p>
      <form class="module-form" @submit.prevent="filter()">
        <div class="form-field">
          <span class="form-label">运行状态</span
          ><AppSelect v-model="status" aria-label="运行状态" placeholder="全部状态"
            ><el-option value="" :label="'全部'" /><el-option
              v-for="(label, key) in flowStates"
              :key="key"
              :value="key"
              :label="label"
          /></AppSelect>
        </div>
        <el-button :disabled="loading" native-type="submit" type="default">查询流程</el-button
        ><el-button :disabled="loading" @click="filter('')" native-type="button" type="default"
          >重置筛选</el-button
        ><el-button :disabled="loading" @click="reload" native-type="button" type="default">
          刷新流程
        </el-button>
      </form>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false"
        >{{ error }}；自动刷新时可能保留上次确认的数据。</el-alert
      >
      <p v-if="loading" role="status">正在加载运行…</p>
      <div v-if="!error || rows.length" class="table-scroll">
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
              }}<small :title="row.id">{{ row.id.slice(0, 8) }}…</small></template
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
      :definition-key="startingKey"
      :definitions="definitions"
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
