<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { can } from '../access'
import { modelApi, callStates, money } from '../model-api'
import { uuidPattern } from '../file-api'
import { usePagedList } from '../composables/usePagedList'
import ListPager from './ListPager.vue'
import CostSummary from './CostSummary.vue'
import InvocationDialog from './InvocationDialog.vue'
const props = defineProps<{ version: number }>()
const filters = reactive({ channel_id: '', flow_run_id: '', since: '', until: '' }), applied = ref<Record<string, string>>({}), validation = ref(''), selected = ref('')
const list = usePagedList('model_invocation:read', offset => modelApi.calls(offset, applied.value))
const totals = usePagedList('model_invocation:read', async () => [await modelApi.summary(applied.value)])
function refresh() { void list.reload(); void totals.reload() }
function apply() {
  validation.value = ''
  for (const value of [filters.channel_id, filters.flow_run_id]) if (value.trim() && !new RegExp(`^${uuidPattern}$`).test(value.trim())) { validation.value = '渠道和运行 ID 须为完整 UUID'; return }
  const until = filters.until ? new Date(filters.until).getTime() : Date.now(), since = filters.since ? new Date(filters.since).getTime() : until - 30 * 86400000
  if (!Number.isFinite(since) || !Number.isFinite(until) || since > until || until - since > 366 * 86400000) { validation.value = '时间范围须有效且不超过 366 天'; return }
  applied.value = Object.fromEntries(Object.entries(filters).filter(([, value]) => value.trim()).map(([key, value]) => [key, ['since', 'until'].includes(key) ? new Date(value).toISOString() : value.trim()]))
  applied.value.since = new Date(since).toISOString(); applied.value.until = new Date(until).toISOString()
  selected.value = ''; list.search(); totals.search()
}
function reset() { Object.assign(filters, { channel_id: '', flow_run_id: '', since: '', until: '' }); apply() }
watch(() => props.version, refresh)
</script>
<template>
  <section aria-label="调用记录与费用"><h2>调用记录与费用</h2><p>{{ can('model_invocation:read_all') ? '可见全部用户及系统调用' : '仅显示本人调用' }}。默认最近 30 天，最多 366 天；列表和汇总使用相同筛选条件。</p>
    <form class="module-form" @submit.prevent="apply"><label>渠道 ID<input v-model="filters.channel_id" maxlength="36"></label><label>Flow 运行 ID<input v-model="filters.flow_run_id" maxlength="36"></label><label>开始时间<input v-model="filters.since" type="datetime-local"></label><label>结束时间<input v-model="filters.until" type="datetime-local"></label><button :disabled="list.loading.value || totals.loading.value">查询调用</button><button type="button" class="secondary" @click="reset">重置筛选</button><button type="button" class="secondary" :disabled="list.loading.value || totals.loading.value" @click="refresh">刷新调用记录</button></form>
    <p v-if="validation || list.error.value" role="alert">{{ validation || list.error.value }}</p><p v-if="totals.error.value" role="alert">费用汇总暂不可用：{{ totals.error.value }}</p><CostSummary v-if="totals.rows.value[0]" :value="totals.rows.value[0]" />
    <p v-if="list.loading.value" role="status">正在加载调用记录…</p><div class="table-scroll"><table><thead><tr><th>登记时间</th><th>模型</th><th>结果</th><th>上游报告费用</th><th>操作</th></tr></thead><tbody><tr v-for="row in list.rows.value" :key="row.id"><td>{{ new Date(row.created_at).toLocaleString() }}</td><td>{{ row.provider_model }}<small class="cell-description">{{ row.id }}</small></td><td>{{ callStates[row.status] }}</td><td>{{ row.cost_amount === null ? '未知' : `${money(row.cost_amount)} ${row.cost_unit}` }}<br>{{ row.cost_source }}</td><td><button @click="selected = row.id">查看调用</button></td></tr><tr v-if="!list.loading.value && !list.error.value && !list.rows.value.length"><td colspan="5">暂无符合条件的调用</td></tr></tbody></table></div>
    <ListPager :page="list.page.value" :count="list.rows.value.length" :loading="list.loading.value" @change="list.page.value = $event" />
    <InvocationDialog v-if="selected" :key="selected" :id="selected" @close="selected = ''" @changed="refresh" />
  </section>
</template>
