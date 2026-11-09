<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref } from 'vue'
import { can } from '../../../stores/session'
import {
  callStates,
  costCheckStates,
  operationLabels,
  type Invocation,
  type ModelOperation,
} from '../../../api/models'
import ListPager from '../../../components/ListPager.vue'
import StatusTag from '../../../components/StatusTag.vue'
import { estimateStates } from '../../../api/pricing'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import { statisticsTime } from '../time'
const props = withDefaults(
  defineProps<{
    rows: Invocation[]
    page: number
    loading: boolean
    costBasis?: 'actual' | 'estimated'
  }>(),
  { costBasis: 'estimated' },
)
const compact = ref(false)
const narrowViewport = window.matchMedia('(max-width: 600px)')
function updateDensity() {
  compact.value = narrowViewport.matches
}
onMounted(() => {
  updateDensity()
  narrowViewport.addEventListener('change', updateDensity)
})
onScopeDispose(() => narrowViewport.removeEventListener('change', updateDensity))
const missingFee = computed(() => (props.costBasis === 'estimated' ? '暂无法预估' : '费用未知'))
const displayRows = computed(() =>
  props.rows.map((row) => {
    const flowRunId = row.flow_context?.flow_run_id
    return {
      ...row,
      modelLabel: row.model_name ?? row.model_key ?? row.provider_model,
      flowRunId: typeof flowRunId === 'string' ? flowRunId : '',
      feeAmount: props.costBasis === 'estimated' ? row.estimate_amount : row.cost_amount,
      feeUnit: props.costBasis === 'estimated' ? row.estimate_currency : row.cost_unit,
      feeState:
        props.costBasis === 'estimated'
          ? estimateStates[row.estimate_status ?? 'unavailable']
          : (costCheckStates[row.cost_check_status ?? ''] ?? row.cost_check_status),
    }
  }),
)
defineEmits<{
  page: [value: number]
  select: [value: string]
  flow: [value: string]
  drill: [filters: Record<string, string>]
}>()
</script>
<template>
  <section aria-label="调用明细">
    <p v-if="loading" role="status">正在加载调用记录…</p>
    <template v-if="compact">
      <ul v-if="displayRows.length" class="compact-calls">
        <li v-for="row in displayRows" :key="row.id">
          <div class="call-title">
            <strong>{{ row.modelLabel }}</strong>
            <StatusTag :status="row.status">{{ callStates[row.status] ?? row.status }}</StatusTag>
          </div>
          <div class="call-fee">
            <MoneyAmount
              v-if="row.feeAmount != null && row.feeUnit"
              :amount="row.feeAmount"
              :unit="row.feeUnit"
            />
            <span v-else>{{ missingFee }}</span>
            <small v-if="row.feeState && row.feeState !== missingFee">{{ row.feeState }}</small>
          </div>
          <div class="call-actions">
            <small>{{ statisticsTime(row.created_at) }}</small>
            <el-button type="primary" link native-type="button" @click="$emit('select', row.id)"
              >查看调用</el-button
            >
          </div>
          <div v-if="row.flowRunId" class="call-flow">
            <el-button
              type="primary"
              link
              native-type="button"
              @click="$emit('drill', { flow_run_id: row.flowRunId })"
            >
              筛选此 Flow
            </el-button>
            <el-button
              v-if="can('flow:read')"
              link
              native-type="button"
              @click="$emit('flow', row.flowRunId)"
            >
              查看流程运行
            </el-button>
          </div>
        </li>
      </ul>
      <el-empty v-else-if="!loading" description="暂无符合条件的调用" :image-size="64" />
    </template>
    <div v-else class="table-scroll">
      <el-table :data="displayRows" :fit="true" native-scrollbar stripe class="data-table">
        <el-table-column label="登记时间" min-width="170"
          ><template #default="{ row }">{{
            statisticsTime(row.created_at)
          }}</template></el-table-column
        >
        <el-table-column label="模型 / 渠道" min-width="200"
          ><template #default="{ row }"
            ><span class="truncate" :title="row.modelLabel">{{ row.modelLabel }}</span
            ><small
              class="truncate"
              :title="`${row.provider_model} · ${row.channel_name ?? row.channel_id}`"
              >{{ row.provider_model }} · {{ row.channel_name ?? row.channel_id }}</small
            ></template
          ></el-table-column
        >
        <el-table-column label="用户 / 操作" min-width="150"
          ><template #default="{ row }"
            ><span class="truncate" :title="row.user_name ?? row.user_id">{{
              row.user_name ?? row.user_id ?? '系统调用'
            }}</span
            ><small>{{
              operationLabels[row.operation as ModelOperation] ?? row.operation
            }}</small></template
          ></el-table-column
        >
        <el-table-column label="调用结果" min-width="110"
          ><template #default="{ row }"
            >{{ callStates[row.status]
            }}<small class="truncate" :title="row.error_code">{{ row.error_code }}</small></template
          ></el-table-column
        >
        <el-table-column
          :label="costBasis === 'estimated' ? '预估费用 / 状态' : '上游实扣 / 核对状态'"
          min-width="170"
          ><template #default="{ row }"
            ><MoneyAmount
              v-if="row.feeAmount != null && row.feeUnit"
              :amount="row.feeAmount"
              :unit="row.feeUnit"
            />
            <span v-else>{{ missingFee }}</span>
            <small>{{ row.feeState }}</small></template
          ></el-table-column
        >
        <el-table-column label="Flow / 步骤" min-width="180"
          ><template #default="{ row }"
            ><template v-if="row.flow_context?.flow_run_id"
              ><el-button
                link
                type="primary"
                native-type="button"
                class="flow-link"
                :title="row.flow_context.node_key ?? 'Flow 运行'"
                @click="$emit('drill', { flow_run_id: row.flow_context.flow_run_id })"
                ><span class="truncate">{{
                  row.flow_context.node_key ?? 'Flow 运行'
                }}</span></el-button
              ><small class="truncate" :title="row.flow_context.flow_run_id">{{
                row.flow_context.flow_run_id
              }}</small
              ><el-button
                v-if="can('flow:read')"
                link
                native-type="button"
                @click="$emit('flow', row.flow_context.flow_run_id)"
                >查看流程运行</el-button
              ></template
            ><span v-else>普通调用</span></template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="110"
          ><template #default="{ row }"
            ><el-button type="primary" link native-type="button" @click="$emit('select', row.id)"
              >查看调用</el-button
            ></template
          ></el-table-column
        >
        <template #empty><el-empty description="暂无符合条件的调用" :image-size="64" /></template>
      </el-table>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading"
      @change="$emit('page', $event)"
    />
  </section>
</template>
<style scoped>
.compact-calls {
  list-style: none;
  margin: 0;
  padding: 0;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.compact-calls li {
  padding: 12px 16px 4px;
}
.compact-calls li + li {
  border-top: 1px solid var(--el-border-color-lighter);
}
.call-title,
.call-fee,
.call-actions,
.call-flow {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.call-title {
  justify-content: space-between;
}
.call-title strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-weight: 600;
}
.call-title .el-tag {
  flex-shrink: 0;
}
.call-fee {
  flex-wrap: wrap;
  margin-top: 4px;
}
.call-fee small,
.call-actions small {
  color: var(--el-text-color-secondary);
}
.call-actions {
  justify-content: space-between;
  align-items: center;
}
.compact-calls .el-button {
  min-height: 40px;
}
.call-flow {
  flex-wrap: wrap;
}
.call-flow .el-button + .el-button {
  margin-left: 0;
}
.truncate {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.flow-link {
  max-width: 100%;
  min-width: 0;
}
.flow-link :deep(> span) {
  min-width: 0;
  max-width: 100%;
}
</style>
