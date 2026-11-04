<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { can, identity } from '../../stores/session'
import {
  modelApi,
  callStates,
  costCheckStates,
  operationLabels,
  type Invocation,
} from '../../api/models'
import {
  dimensions,
  dimensionFilters,
  filterKeys,
  sourceLabels,
  feeSourceLabels,
  usageApi,
  type Dimension,
  type UsageStatistics,
} from '../../api/modelUsage'
import { uuidPattern } from '../../api/client'
import { shanghaiInput, shanghaiDate, statisticsRange } from './time'
import AppSelect from '../../components/AppSelect.vue'
import InvocationDialog from '../models/components/InvocationDialog.vue'
import { estimateStates } from '../../api/pricing'
import FlowRunDialog from '../flows/components/FlowRunDialog.vue'
import StatisticsPicker from './components/StatisticsPicker.vue'
import UsageSummary from './components/UsageSummary.vue'
import UsageTrend from './components/UsageTrend.vue'
import UsageGroups from './components/UsageGroups.vue'
import UsageCalls from './components/UsageCalls.vue'
const route = useRoute(),
  router = useRouter()
const allowed = computed(() => can('model_invocation:read'))
const scope = computed(
  () => `${identity.value?.user.id}:${identity.value?.permissions.slice().sort().join(',')}`,
)
const text = (key: string) =>
  typeof route.query[key] === 'string' ? (route.query[key] as string) : ''
const current = computed<Record<string, string>>(() => ({
  ...Object.fromEntries(filterKeys.filter((key) => text(key)).map((key) => [key, text(key)])),
  cost_basis:
    text('cost_basis') === 'actual' ||
    (!text('cost_basis') && (text('cost_status') || text('cost_source')))
      ? 'actual'
      : 'estimated',
}))
const costBasis = computed(() => (current.value.cost_basis === 'actual' ? 'actual' : 'estimated'))
const tab = computed({
  get: () => (['overview', 'analysis', 'details'].includes(text('tab')) ? text('tab') : 'overview'),
  set: (value) => void navigate({ tab: value, invocation: null, run: null }),
})
const grouping = computed<Dimension>(() =>
  costBasis.value === 'estimated' && ['cost_status', 'cost_source'].includes(text('group_by'))
    ? 'estimate_status'
    : Object.hasOwn(dimensions, text('group_by'))
      ? (text('group_by') as Dimension)
      : 'channel',
)
const ordering = computed(() => (text('order_by') === 'cost' ? 'cost' : 'calls'))
const sortUnit = computed(() => text('sort_unit') || 'USD')
const bucket = computed(() =>
  ['hour', 'day', 'month'].includes(text('bucket')) ? text('bucket') : 'day',
)
const safePage = (value: string) => (/^\d+$/.test(value) ? Math.min(2000, Number(value)) : 0)
const page = computed(() => safePage(text('page'))),
  groupPage = computed(() => safePage(text('group_page')))
const selected = computed(() =>
  new RegExp(`^${uuidPattern}$`).test(text('invocation')) ? text('invocation') : '',
)
const selectedRun = computed(() =>
  new RegExp(`^${uuidPattern}$`).test(text('run')) ? text('run') : '',
)
const draft = reactive(
  Object.fromEntries(filterKeys.map((key) => [key, ''])) as Record<
    (typeof filterKeys)[number],
    string
  >,
)
function resetCostFilters() {
  draft.cost_status = ''
  draft.cost_source = ''
  draft.estimate_status = ''
}
const validation = ref(''),
  statistics = ref<UsageStatistics | null>(null),
  statsError = ref(''),
  statsLoading = ref(false)
const calls = ref<Invocation[]>([]),
  callsError = ref(''),
  callsLoading = ref(false)
const preset = ref('custom')
const advancedOpen = ref(false),
  filtersExpanded = ref(false)
const rollingPresets = ['today', '7', '30', 'month']
const fields: { dimension: Dimension; key: (typeof filterKeys)[number]; label: string }[] = [
  { dimension: 'upstream_model', key: 'provider_model', label: '上游模型' },
  { dimension: 'flow_run', key: 'flow_run_id', label: 'Flow 运行' },
  { dimension: 'node', key: 'node_key', label: '流程步骤' },
  { dimension: 'model_role', key: 'model_role', label: '模型角色' },
  { dimension: 'agent', key: 'agent_key', label: 'Agent' },
  { dimension: 'error', key: 'error_code', label: '错误码' },
]
let alive = true,
  statsGeneration = 0,
  callsGeneration = 0
let detailOrigin: { field: string; id: string; path: string } | null = null
function openDetail(field: 'invocation' | 'run', id: string) {
  detailOrigin = { field, id, path: route.fullPath }
  void navigate({ [field]: id })
}
function closeDetail(field: 'invocation' | 'run') {
  const query = { ...route.query }
  delete query[field]
  const origin = detailOrigin
  detailOrigin = null
  if (
    origin?.field === field &&
    origin.id === text(field) &&
    router.resolve({ path: route.path, query }).fullPath === origin.path
  )
    router.back()
  else void navigate({ [field]: null }, true)
}
async function navigate(patch: Record<string, string | null>, replace = false) {
  const query = { ...route.query, ...patch }
  delete query.timezone
  for (const key of Object.keys(query))
    if (query[key] === null || query[key] === '') delete query[key]
  await router[replace ? 'replace' : 'push']({ path: '/model-usage', query })
}
const localInput = shanghaiInput
watch(
  () => JSON.stringify({ filters: current.value, range: text('range') }),
  () => {
    for (const key of filterKeys) draft[key] = current.value[key] ?? ''
    draft.since = draft.since ? localInput(draft.since) : ''
    draft.until = draft.until ? localInput(draft.until) : ''
    validation.value = ''
    preset.value = rollingPresets.includes(text('range')) ? text('range') : 'custom'
  },
  { immediate: true },
)
async function ensureRange() {
  if (current.value.since && current.value.until) return true
  const until = current.value.until ? new Date(current.value.until) : new Date()
  if (!Number.isFinite(until.getTime())) return true // Server returns a safe validation error.
  await navigate(
    {
      since: current.value.since || new Date(until.getTime() - 30 * 86400000).toISOString(),
      until: until.toISOString(),
      range: text('range') || (!current.value.since && !current.value.until ? '30' : 'custom'),
    },
    true,
  )
  return false
}
const statsKey = computed(() =>
  JSON.stringify({
    ...current.value,
    group_by: grouping.value,
    order_by: ordering.value,
    sort_unit: sortUnit.value,
    bucket: bucket.value,
    offset: String(groupPage.value * 50),
    scope: scope.value,
  }),
)
async function loadStatistics() {
  const generation = ++statsGeneration
  statistics.value = null
  statsError.value = ''
  statsLoading.value = false
  if (!allowed.value || !(await ensureRange())) return
  statsLoading.value = true
  try {
    const result = await usageApi.statistics({
      ...current.value,
      group_by: grouping.value,
      order_by: ordering.value,
      sort_unit: sortUnit.value,
      bucket: bucket.value,
      offset: String(groupPage.value * 50),
      limit: '50',
    })
    if (alive && generation === statsGeneration && allowed.value) statistics.value = result
  } catch (reason) {
    if (alive && generation === statsGeneration && allowed.value)
      statsError.value = reason instanceof Error ? reason.message : '统计查询失败'
  } finally {
    if (alive && generation === statsGeneration) statsLoading.value = false
  }
}
watch([statsKey, allowed], loadStatistics, { immediate: true })
const callsKey = computed(() =>
  JSON.stringify({
    ...current.value,
    page: page.value,
    active: tab.value === 'details',
    scope: scope.value,
  }),
)
async function loadCalls() {
  const generation = ++callsGeneration
  calls.value = []
  callsError.value = ''
  callsLoading.value = false
  if (!allowed.value || tab.value !== 'details' || !(await ensureRange())) return
  callsLoading.value = true
  try {
    const result = await modelApi.calls(page.value * 50, current.value)
    if (alive && generation === callsGeneration && allowed.value) calls.value = result
  } catch (reason) {
    if (alive && generation === callsGeneration && allowed.value)
      callsError.value = reason instanceof Error ? reason.message : '调用记录查询失败'
  } finally {
    if (alive && generation === callsGeneration) callsLoading.value = false
  }
}
watch([callsKey, allowed], loadCalls, { immediate: true })
const rangeFor = statisticsRange
function setPreset(value: string) {
  preset.value = value
  if (value === 'custom') {
    advancedOpen.value = true
    return
  }
  const range = rangeFor(value)
  draft.since = localInput(range.since)
  draft.until = localInput(range.until)
}
async function apply() {
  validation.value = ''
  const until = draft.until ? shanghaiDate(draft.until) : new Date()
  const since = draft.since ? shanghaiDate(draft.since) : new Date(until.getTime() - 30 * 86400000)
  if (
    !Number.isFinite(since.getTime()) ||
    !Number.isFinite(until.getTime()) ||
    since > until ||
    until.getTime() - since.getTime() > 366 * 86400000
  ) {
    validation.value = '时间范围须有效且不超过 366 天'
    return
  }
  for (const key of [
    'channel_id',
    'model_entry_id',
    'user_id',
    'flow_run_id',
    'agent_call_id',
  ] as const) {
    if (
      draft[key].trim() &&
      !(key === 'user_id' && draft[key] === 'system') &&
      !new RegExp(`^${uuidPattern}$`).test(draft[key].trim())
    ) {
      validation.value = '渠道、模型、用户和运行 ID 须为完整 UUID，或从搜索选项中选择'
      return
    }
  }
  const next = Object.fromEntries(filterKeys.map((key) => [key, draft[key].trim() || null]))
  filtersExpanded.value = false
  await navigate({
    ...next,
    since:
      current.value.since && draft.since === localInput(current.value.since)
        ? current.value.since
        : since.toISOString(),
    until:
      current.value.until && draft.until === localInput(current.value.until)
        ? current.value.until
        : until.toISOString(),
    range: preset.value,
    page: null,
    group_page: null,
    invocation: null,
    run: null,
  })
}
async function reset() {
  for (const key of filterKeys) draft[key] = ''
  draft.cost_basis = costBasis.value
  setPreset('30')
  await apply()
}
async function refresh(advance = true) {
  if (advance && rollingPresets.includes(text('range'))) {
    const dirty = Object.fromEntries(
      filterKeys
        .filter(
          (key) =>
            draft[key] !==
            (['since', 'until'].includes(key) && current.value[key]
              ? localInput(current.value[key]!)
              : (current.value[key] ?? '')),
        )
        .map((key) => [key, draft[key]]),
    )
    const draftPreset = preset.value
    const range = rangeFor(text('range'))
    if (range.until !== current.value.until || range.since !== current.value.since) {
      await navigate({ ...range, page: null, invocation: null, run: null }, true)
      Object.assign(draft, dirty)
      if ('since' in dirty || 'until' in dirty) preset.value = draftPreset
      return
    }
  }
  await Promise.all([loadStatistics(), loadCalls()])
}
function drill(filters: Record<string, string>) {
  const next = { ...current.value, ...filters }
  const missing = new Set((current.value.missing ?? '').split(',').filter(Boolean))
  for (const dimension of Object.keys(dimensions) as Dimension[]) {
    if (dimensionFilters[dimension] in filters) missing.delete(dimension)
  }
  for (const dimension of (filters.missing ?? '').split(',').filter(Boolean)) {
    missing.add(dimension)
    delete next[dimensionFilters[dimension as Dimension]]
  }
  next.missing = [...missing].join(',')
  const cleared = Object.fromEntries(
    filterKeys.filter((key) => key in current.value && !(key in next)).map((key) => [key, null]),
  )
  void navigate({
    ...cleared,
    ...next,
    tab: 'details',
    range: filters.since || filters.until ? 'custom' : text('range') || 'custom',
    page: null,
    group_page: null,
    invocation: null,
    run: null,
  })
}
const missingLabels = computed(() =>
  (current.value.missing ?? '')
    .split(',')
    .filter(Boolean)
    .map((key) => dimensions[key as Dimension])
    .join('、'),
)
const filterNames: Record<string, string> = {
  cost_basis: '费用口径',
  estimate_status: '预估状态',
  channel_id: '渠道',
  model_entry_id: '模型',
  user_id: '用户',
  source: '来源',
  operation: '操作',
  status: '结果',
  cost_status: '查费',
  cost_source: '费用来源',
  cost_known: '金额',
  cost_unit: '单位',
  provider_model: '上游模型',
  flow_run_id: '运行',
  node_key: '步骤',
  model_role: '模型角色',
  agent_key: 'Agent',
  error_code: '错误码',
  agent_call_id: 'Agent 调用',
  missing: '未关联',
}
const activeFilters = computed(() =>
  Object.entries(current.value)
    .filter(([key]) => !['since', 'until', 'cost_basis'].includes(key))
    .map(([key, value]) => ({
      key,
      value,
      label: filterNames[key] ?? key,
      display:
        (key === 'estimate_status' ? estimateStates[value] : undefined) ??
        sourceLabels[value as keyof typeof sourceLabels] ??
        operationLabels[value as keyof typeof operationLabels] ??
        callStates[value as keyof typeof callStates] ??
        costCheckStates[value as keyof typeof costCheckStates] ??
        (key === 'cost_known'
          ? value === 'false'
            ? '费用未知'
            : '已有金额'
          : value.length > 24
            ? `${value.slice(0, 8)}…`
            : value),
    })),
)
onScopeDispose(() => {
  alive = false
  statsGeneration++
  callsGeneration++
})
</script>
<template>
  <section aria-label="用量与费用">
    <div class="heading">
      <h1>用量与费用</h1>
    </div>
    <template v-if="allowed">
      <p class="usage-scope">
        {{ can('model_invocation:read_all') ? '可见全部用户及系统调用' : '仅显示本人调用' }} ·
        默认近 30 天，最长 366 天。统一按上海时间（UTC+8）。
      </p>
      <el-button
        class="usage-filter-toggle"
        :aria-expanded="filtersExpanded"
        aria-controls="usage-filter-form"
        @click="filtersExpanded = !filtersExpanded"
        >{{ filtersExpanded ? '收起筛选' : '调整筛选'
        }}<span v-if="activeFilters.length"> · {{ activeFilters.length }} 项限定</span></el-button
      >
      <form
        id="usage-filter-form"
        class="usage-filters"
        :class="{ 'is-expanded': filtersExpanded }"
        aria-label="用量费用筛选"
        @submit.prevent="apply"
      >
        <div class="filter-grid">
          <label
            >时间范围<AppSelect
              :model-value="preset"
              aria-label="时间快捷范围"
              @update:model-value="setPreset"
              ><el-option value="custom" label="自定义" /><el-option
                value="today"
                label="今天" /><el-option value="7" label="近 7 天" /><el-option
                value="30"
                label="近 30 天" /><el-option value="month" label="本月" /></AppSelect
          ></label>
          <label
            >渠道 / 站点<StatisticsPicker
              v-model="draft.channel_id"
              dimension="channel"
              label="渠道筛选"
              :filters="current"
          /></label>
          <label
            >业务模型<StatisticsPicker
              v-model="draft.model_entry_id"
              dimension="model"
              label="模型筛选"
              :filters="current"
          /></label>
        </div>
        <details
          :open="advancedOpen"
          @toggle="advancedOpen = ($event.target as HTMLDetailsElement).open"
        >
          <summary>更多筛选与自定义时间</summary>
          <div class="filter-grid">
            <label
              >开始时间（上海）<el-input
                v-model="draft.since"
                type="datetime-local"
                step="1"
                @input="preset = 'custom'"
            /></label>
            <label
              >结束时间（上海）<el-input
                v-model="draft.until"
                type="datetime-local"
                step="1"
                @input="preset = 'custom'"
            /></label>
            <label v-if="can('model_invocation:read_all')"
              >用户 / 系统<StatisticsPicker
                v-model="draft.user_id"
                dimension="user"
                label="用户筛选"
                :filters="current"
            /></label>
            <label
              >调用来源<AppSelect v-model="draft.source" aria-label="调用来源" clearable
                ><el-option
                  v-for="(label, key) in sourceLabels"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>
            <label
              >操作类型<AppSelect v-model="draft.operation" aria-label="操作类型" clearable
                ><el-option
                  v-for="(label, key) in operationLabels"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>

            <label v-for="field in fields" :key="field.key"
              >{{ field.label
              }}<StatisticsPicker
                v-model="draft[field.key]"
                :dimension="field.dimension"
                :label="field.label"
                :filters="current"
            /></label>
            <label
              >调用结果<AppSelect v-model="draft.status" aria-label="调用结果" clearable
                ><el-option
                  v-for="(label, key) in callStates"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>
            <label
              >费用口径<AppSelect
                v-model="draft.cost_basis"
                aria-label="费用口径"
                @change="resetCostFilters"
                ><el-option value="estimated" label="预估费用" /><el-option
                  value="actual"
                  label="上游实扣" /></AppSelect
            ></label>
            <label v-if="draft.cost_basis !== 'actual'"
              >预估状态<AppSelect v-model="draft.estimate_status" aria-label="预估状态" clearable
                ><el-option
                  v-for="(label, key) in estimateStates"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>
            <label v-if="draft.cost_basis === 'actual'"
              >查费状态<AppSelect v-model="draft.cost_status" aria-label="查费状态" clearable
                ><el-option
                  v-for="(label, key) in costCheckStates"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>
            <label v-if="draft.cost_basis === 'actual'"
              >费用来源<AppSelect v-model="draft.cost_source" aria-label="费用来源" clearable
                ><el-option
                  v-for="(label, key) in feeSourceLabels"
                  :key="key"
                  :value="key"
                  :label="label" /></AppSelect
            ></label>
            <label
              >费用是否已知<AppSelect v-model="draft.cost_known" aria-label="费用是否已知" clearable
                ><el-option value="true" label="已有金额" /><el-option
                  value="false"
                  label="费用未知" /></AppSelect
            ></label>
            <label
              >费用单位<el-input
                v-model="draft.cost_unit"
                maxlength="32"
                placeholder="如 USD、credits"
            /></label>
            <label>Agent 调用 ID<el-input v-model="draft.agent_call_id" maxlength="36" /></label>
          </div>
          <div class="row-actions bucket-controls">
            <label
              >时间粒度<AppSelect
                :model-value="bucket"
                aria-label="时间粒度"
                @update:model-value="navigate({ bucket: $event, group_page: null })"
                ><el-option value="hour" label="小时" /><el-option
                  value="day"
                  label="天" /><el-option value="month" label="月" /></AppSelect
            ></label>
            <span>按小时统计最长 31 天。</span>
          </div>
        </details>
        <p v-if="missingLabels">
          当前还限定：未关联{{ missingLabels }}。<el-button
            link
            native-type="button"
            @click="navigate({ missing: null, page: null, group_page: null })"
            >清除此限定</el-button
          >
        </p>
        <div class="row-actions">
          <el-button native-type="submit" type="primary">应用筛选</el-button
          ><el-button native-type="button" @click="reset">重置筛选</el-button
          ><el-button
            native-type="button"
            :disabled="statsLoading || callsLoading"
            @click="refresh()"
            >刷新统计</el-button
          ><span>筛选条件保存在当前链接中。</span>
        </div>
      </form>
      <div class="applied-filters" aria-label="已应用筛选">
        <span
          >当前范围：{{ current.since ? localInput(current.since).replace('T', ' ') : '—' }} 至
          {{ current.until ? localInput(current.until).replace('T', ' ') : '—' }} · 上海时间</span
        ><el-tag
          v-for="filter in activeFilters"
          :key="filter.key"
          :title="filter.value"
          closable
          effect="plain"
          @close="
            navigate({
              [filter.key]: null,
              page: null,
              group_page: null,
              invocation: null,
              run: null,
            })
          "
          >{{ filter.label }}：{{ filter.display }}</el-tag
        >
      </div>
      <el-alert v-if="validation" role="alert" type="error" :closable="false">{{
        validation
      }}</el-alert>
      <el-alert v-if="statsError" role="alert" type="error" :closable="false"
        >统计暂不可用：{{ statsError }}</el-alert
      >
      <el-alert v-if="callsError" role="alert" type="error" :closable="false"
        >调用明细暂不可用：{{ callsError }}</el-alert
      >
      <p v-if="statsLoading" role="status">正在计算用量与费用…</p>
      <el-tabs v-model="tab">
        <el-tab-pane label="总览" name="overview"
          ><template v-if="statistics"
            ><UsageSummary
              :value="statistics.summary"
              :cost-basis="costBasis"
              @drill="drill" /><UsageTrend
              :points="statistics.trend"
              :since="statistics.since"
              :until="statistics.until"
              :units="statistics.summary.costs.map((cost) => cost.unit)"
              :timezone="statistics.timezone"
              @drill="drill" /><UsageGroups
              :rows="statistics.groups"
              :dimension="grouping"
              :cost-basis="costBasis"
              :page="groupPage"
              :total="statistics.total_groups"
              :order="ordering"
              :unit="sortUnit"
              :units="statistics.summary.costs.map((cost) => cost.unit)"
              @order="
                navigate({ order_by: $event.order, sort_unit: $event.unit, group_page: null })
              "
              :loading="statsLoading"
              @dimension="navigate({ group_by: $event, group_page: null })"
              @page="navigate({ group_page: String($event) })"
              @drill="drill" /></template
        ></el-tab-pane>
        <el-tab-pane label="维度分析" name="analysis"
          ><UsageGroups
            v-if="statistics"
            :rows="statistics.groups"
            :dimension="grouping"
            :cost-basis="costBasis"
            :page="groupPage"
            :total="statistics.total_groups"
            :order="ordering"
            :unit="sortUnit"
            :units="statistics.summary.costs.map((cost) => cost.unit)"
            @order="navigate({ order_by: $event.order, sort_unit: $event.unit, group_page: null })"
            :loading="statsLoading"
            @dimension="navigate({ group_by: $event, group_page: null })"
            @page="navigate({ group_page: String($event) })"
            @drill="drill"
        /></el-tab-pane>
        <el-tab-pane label="调用明细" name="details"
          ><UsageCalls
            :cost-basis="costBasis"
            v-if="!callsError"
            :rows="calls"
            :page="page"
            :loading="callsLoading"
            @page="navigate({ page: String($event), invocation: null, run: null })"
            @select="openDetail('invocation', $event)"
            @flow="openDetail('run', $event)"
            @drill="drill"
        /></el-tab-pane>
      </el-tabs>
      <InvocationDialog
        v-if="selected"
        :key="`${scope}:${selected}`"
        :id="selected"
        @close="closeDetail('invocation')"
        @changed="refresh(false)"
      />
      <FlowRunDialog
        v-if="selectedRun && can('flow:read')"
        :key="`${scope}:${selectedRun}`"
        :id="selectedRun"
        @close="closeDetail('run')"
        @changed="refresh(false)"
      />
    </template>
    <el-empty v-else description="没有调用记录与费用查看权限" />
  </section>
</template>
<style scoped>
.usage-filter-toggle {
  display: none;
}
.usage-scope {
  margin-bottom: 8px;
  font-size: 12px;
}
@media (max-width: 600px) {
  .usage-filter-toggle {
    display: inline-flex;
    margin: 12px 0;
  }
  .usage-filters:not(.is-expanded) {
    display: none;
  }
  .usage-scope {
    font-size: 12px;
  }
}
.usage-filters {
  padding: 18px 0;
  border-bottom: 1px solid #e4e8ed;
}
.filter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}
label {
  display: grid;
  gap: 6px;
  min-width: 0;
}
summary {
  cursor: pointer;
  padding: 0 0 8px;
}
.bucket-controls {
  margin: 18px 0;
}
.bucket-controls label {
  min-width: 130px;
}
</style>
