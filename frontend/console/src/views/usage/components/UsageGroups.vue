<script setup lang="ts">
import { computed } from 'vue'
import {
  dimensions,
  duration,
  percent,
  sourceLabels,
  feeSourceLabels,
  type Dimension,
  type UsageGroup,
} from '../../../api/modelUsage'
import {
  callStates,
  costCheckStates,
  operationLabels,
  type ModelOperation,
} from '../../../api/models'
import AppSelect from '../../../components/AppSelect.vue'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import ListPager from '../../../components/ListPager.vue'
import { estimateStates } from '../../../api/pricing'
const props = defineProps<{
  rows: UsageGroup[]
  dimension: Dimension
  page: number
  total: number
  loading: boolean
  order: string
  unit: string
  units: string[]
  costBasis?: 'actual' | 'estimated'
}>()
const emit = defineEmits<{
  dimension: [value: Dimension]
  page: [value: number]
  order: [value: { order: string; unit: string }]
  drill: [filters: Record<string, string>]
}>()
const grouping = computed({ get: () => props.dimension, set: (value) => emit('dimension', value) })
const availableDimensions = computed(() =>
  Object.fromEntries(
    Object.entries(dimensions).filter(([key]) =>
      props.costBasis === 'actual'
        ? key !== 'estimate_status'
        : !['cost_status', 'cost_source'].includes(key),
    ),
  ),
)
const ordering = computed({
  get: () => (props.order === 'calls' ? 'calls' : props.unit),
  set: (value) =>
    emit('order', {
      order: value === 'calls' ? 'calls' : 'cost',
      unit: value === 'calls' ? props.unit : value,
    }),
})
function label(row: UsageGroup) {
  if (row.key === null) return row.label
  if (props.dimension === 'estimate_status') return estimateStates[row.key] ?? row.label
  return props.dimension === 'source'
    ? (sourceLabels[row.key] ?? row.label)
    : props.dimension === 'cost_source'
      ? (feeSourceLabels[row.key] ?? row.label)
      : props.dimension === 'operation'
        ? (operationLabels[row.key as ModelOperation] ?? row.label)
        : props.dimension === 'status'
          ? (callStates[row.key] ?? row.label)
          : props.dimension === 'cost_status'
            ? (costCheckStates[row.key] ?? row.label)
            : row.label
}
</script>
<template>
  <section aria-label="维度分析" class="usage-panel">
    <div class="heading">
      <h2>维度分析</h2>
      <div class="row-actions group-controls">
        <AppSelect v-model="grouping" aria-label="分析维度"
          ><el-option
            v-for="(text, key) in availableDimensions"
            :key="key"
            :value="key"
            :label="text"
        /></AppSelect>
        <AppSelect v-model="ordering" aria-label="分组排序"
          ><el-option value="calls" label="调用次数从高到低" /><el-option
            v-for="value in [...new Set([unit, ...units])]"
            :key="value"
            :value="value"
            :label="`已知费用从高到低 · ${value}`"
        /></AppSelect>
      </div>
    </div>
    <p>
      共
      {{ total }} 组。点击分组进入对应调用明细；单次平均费用以该单位已有金额的调用为分母。
    </p>
    <div class="table-scroll">
      <el-table :data="rows" :fit="true" native-scrollbar stripe class="data-table">
        <el-table-column :label="dimensions[dimension]" min-width="200"
          ><template #default="{ row }"
            ><el-button
              link
              type="primary"
              native-type="button"
              class="group-link"
              :title="label(row)"
              @click="emit('drill', row.filters)"
              ><span class="truncate">{{ label(row) }}</span></el-button
            ><small v-if="row.key && row.label !== row.key" class="truncate" :title="row.key">{{
              row.key
            }}</small></template
          ></el-table-column
        >
        <el-table-column label="调用 / 成功率" min-width="140" align="right"
          ><template #default="{ row }"
            >{{ row.calls }} 次 · {{ percent(row.succeeded, row.calls) }}</template
          ></el-table-column
        >
        <el-table-column label="已知费用 / 单次平均" min-width="220" align="right"
          ><template #default="{ row }"
            ><p v-for="cost in row.costs" :key="cost.unit" class="group-cost">
              <MoneyAmount :amount="cost.amount" :unit="cost.unit" />
              <small class="average-cost"
                >平均 <MoneyAmount :amount="cost.avg_cost" :unit="cost.unit"
              /></small>
              <small>{{ cost.known_calls }} 次已知费用</small>
            </p>
            <span v-if="!row.costs.length">未知</span></template
          ></el-table-column
        >
        <el-table-column label="费用覆盖率" min-width="150" align="right"
          ><template #default="{ row }"
            >{{ percent(row.known_calls, row.calls)
            }}<small
              >未知 {{ row.unknown_cost_calls }} · 待核对 {{ row.pending_calls }}</small
            ></template
          ></el-table-column
        >
        <el-table-column label="输入 / 输出 Token" min-width="180" align="right"
          ><template #default="{ row }"
            ><span
              class="truncate"
              :title="`${row.input_tokens ?? '未报告'} / ${row.output_tokens ?? '未报告'}`"
              >{{ row.input_tokens ?? '未报告' }} / {{ row.output_tokens ?? '未报告' }}</span
            ><small
              >覆盖 {{ row.input_reported }}/{{ row.calls }} · {{ row.output_reported }}/{{
                row.calls
              }}</small
            ></template
          ></el-table-column
        >
        <el-table-column label="缓存读 / 写 Token" min-width="180" align="right"
          ><template #default="{ row }"
            ><span
              class="truncate"
              :title="`读 ${row.cache_read_tokens ?? '未报告'} / 写 ${row.cache_write_tokens ?? '未报告'}`"
              >{{ row.cache_read_tokens ?? '未报告' }} /
              {{ row.cache_write_tokens ?? '未报告' }}</span
            ><small
              >覆盖 {{ row.cache_read_reported }}/{{ row.calls }} ·
              {{ row.cache_write_reported }}/{{ row.calls }}</small
            ></template
          ></el-table-column
        >
        <el-table-column label="平均 / P95 耗时" min-width="160" align="right"
          ><template #default="{ row }"
            >{{ duration(row.avg_duration_ms) }} / {{ duration(row.p95_duration_ms) }}</template
          ></el-table-column
        >
        <template #empty><el-empty description="暂无符合条件的分组" :image-size="64" /></template>
      </el-table>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading"
      @change="emit('page', $event)"
    />
  </section>
</template>

<style scoped>
.usage-panel {
  padding: 18px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  background: var(--el-bg-color);
  margin-top: 20px;
}
.usage-panel > p {
  margin: 0 0 14px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.heading {
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 12px;
}
.group-controls {
  max-width: 100%;
}
.group-controls .app-select {
  width: 240px;
  max-width: 100%;
}
.group-link {
  max-width: 100%;
  min-width: 0;
}
.group-link :deep(> span) {
  min-width: 0;
  max-width: 100%;
}
.truncate {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-cost {
  margin: 0;
}
.group-cost + .group-cost {
  margin-top: 8px;
}
.average-cost {
  display: flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 0.3em;
  white-space: nowrap;
}
</style>
