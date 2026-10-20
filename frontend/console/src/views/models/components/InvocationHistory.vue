<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { can } from '../../../stores/session'
import { modelApi, callStates, money } from '../../../api/models'
import { uuidPattern } from '../../../api/files'
import { usePagedList } from '../../../composables/usePagedList'
import ListPager from '../../../components/ListPager.vue'
import CostSummary from '../../../components/models/CostSummary.vue'
import InvocationDialog from './InvocationDialog.vue'
const props = defineProps<{ version: number }>()
const initialUntil = Date.now()
const filters = reactive({ channel_id: '', flow_run_id: '', since: '', until: '' }),
  applied = ref<Record<string, string>>({
    since: new Date(initialUntil - 30 * 86400000).toISOString(),
    until: new Date(initialUntil).toISOString(),
  }),
  validation = ref(''),
  selected = ref('')
let relativeUntil = true,
  relativeSince = true
const list = usePagedList('model_invocation:read', (offset) =>
  modelApi.calls(offset, applied.value),
)
const totals = usePagedList('model_invocation:read', async () => [
  await modelApi.summary(applied.value),
])
function refresh() {
  if (relativeUntil) {
    const until = Date.now()
    applied.value = {
      ...applied.value,
      until: new Date(until).toISOString(),
      ...(relativeSince ? { since: new Date(until - 30 * 86400000).toISOString() } : {}),
    }
    list.search()
    totals.search()
  } else {
    void list.reload()
    void totals.reload()
  }
}
function apply() {
  validation.value = ''
  for (const value of [filters.channel_id, filters.flow_run_id])
    if (value.trim() && !new RegExp(`^${uuidPattern}$`).test(value.trim())) {
      validation.value = '渠道和运行 ID 须为完整 UUID'
      return
    }
  const until = filters.until ? new Date(filters.until).getTime() : Date.now(),
    since = filters.since ? new Date(filters.since).getTime() : until - 30 * 86400000
  if (
    !Number.isFinite(since) ||
    !Number.isFinite(until) ||
    since > until ||
    until - since > 366 * 86400000
  ) {
    validation.value = '时间范围须有效且不超过 366 天'
    return
  }
  applied.value = Object.fromEntries(
    Object.entries(filters)
      .filter(([, value]) => value.trim())
      .map(([key, value]) => [
        key,
        ['since', 'until'].includes(key) ? new Date(value).toISOString() : value.trim(),
      ]),
  )
  applied.value.since = new Date(since).toISOString()
  applied.value.until = new Date(until).toISOString()
  relativeUntil = !filters.until
  relativeSince = !filters.since
  selected.value = ''
  list.search()
  totals.search()
}
function reset() {
  Object.assign(filters, { channel_id: '', flow_run_id: '', since: '', until: '' })
  apply()
}
watch(() => props.version, refresh)
</script>
<template>
  <section aria-label="调用记录与费用">
    <h2>调用记录与费用</h2>
    <p>
      {{ can('model_invocation:read_all') ? '可见全部用户及系统调用' : '仅显示本人调用' }}。默认最近
      30 天，最多 366 天；列表和汇总使用相同筛选条件。
    </p>
    <form class="module-form" @submit.prevent="apply">
      <label>渠道 ID<el-input v-model="filters.channel_id" maxlength="36" /></label
      ><label>Flow 运行 ID<el-input v-model="filters.flow_run_id" maxlength="36" /></label
      ><label>开始时间<el-input v-model="filters.since" type="datetime-local" /></label
      ><label>结束时间<el-input v-model="filters.until" type="datetime-local" /></label
      ><el-button
        :disabled="list.loading.value || totals.loading.value"
        native-type="submit"
        type="default"
        >查询调用</el-button
      ><el-button @click="reset" native-type="button" type="default">重置筛选</el-button
      ><el-button
        :disabled="list.loading.value || totals.loading.value"
        @click="refresh"
        native-type="button"
        type="default"
      >
        刷新调用记录
      </el-button>
    </form>
    <el-alert
      v-if="validation || list.error.value"
      role="alert"
      type="error"
      show-icon
      :closable="false"
      >{{ validation || list.error.value }}</el-alert
    >
    <el-alert v-if="totals.error.value" role="alert" type="error" show-icon :closable="false"
      >费用汇总暂不可用：{{ totals.error.value }}</el-alert
    >
    <CostSummary v-if="totals.rows.value[0]" :value="totals.rows.value[0]" />
    <p v-if="list.loading.value" role="status">正在加载调用记录…</p>
    <div class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        table-layout="auto"
        :data="list.rows.value"
        row-key="id"
        stripe
        class="data-table"
      >
        <el-table-column label="登记时间" min-width="180"
          ><template #default="{ row: row }">{{
            new Date(row.created_at).toLocaleString()
          }}</template></el-table-column
        >
        <el-table-column label="模型" min-width="180"
          ><template #default="{ row: row }"
            >{{ row.provider_model }}<small>{{ row.id }}</small></template
          ></el-table-column
        >
        <el-table-column label="结果" min-width="120"
          ><template #default="{ row: row }">{{
            callStates[row.status]
          }}</template></el-table-column
        >
        <el-table-column label="上游报告费用" min-width="180"
          ><template #default="{ row: row }"
            >{{ row.cost_amount === null ? '未知' : `${money(row.cost_amount)} ${row.cost_unit}`
            }}<br />{{ row.cost_source }}</template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="220"
          ><template #default="{ row: row }"
            ><el-button @click="selected = row.id" native-type="button" type="primary" link
              >查看调用</el-button
            ></template
          ></el-table-column
        >
        <template #empty><el-empty :image-size="64" description="暂无符合条件的调用" /></template
      ></el-table>
    </div>
    <ListPager
      :page="list.page.value"
      :count="list.rows.value.length"
      :loading="list.loading.value"
      @change="list.page.value = $event"
    />
    <InvocationDialog
      v-if="selected"
      :key="selected"
      :id="selected"
      @close="selected = ''"
      @changed="refresh"
    />
  </section>
</template>
