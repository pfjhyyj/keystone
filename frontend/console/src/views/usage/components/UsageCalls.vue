<script setup lang="ts">
import { can } from '../../../stores/session'
import {
  callStates,
  costCheckStates,
  operationLabels,
  type Invocation,
  type ModelOperation,
} from '../../../api/models'
import ListPager from '../../../components/ListPager.vue'
import { estimateStates } from '../../../api/pricing'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import { statisticsTime } from '../time'
withDefaults(
  defineProps<{
    rows: Invocation[]
    page: number
    loading: boolean
    costBasis?: 'actual' | 'estimated'
  }>(),
  { costBasis: 'estimated' },
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
    <h2>调用明细</h2>
    <p v-if="loading" role="status">正在加载调用记录…</p>
    <div class="table-scroll">
      <el-table :data="rows" :fit="true" native-scrollbar stripe class="data-table">
        <el-table-column label="登记时间" min-width="170"
          ><template #default="{ row }">{{
            statisticsTime(row.created_at)
          }}</template></el-table-column
        >
        <el-table-column label="模型 / 渠道" min-width="200"
          ><template #default="{ row }"
            ><span
              class="truncate"
              :title="row.model_name ?? row.model_key ?? row.provider_model"
              >{{ row.model_name ?? row.model_key ?? row.provider_model }}</span
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
            ><template v-if="costBasis === 'estimated'"
              ><span v-if="row.estimate_amount == null || !row.estimate_currency">暂无法预估</span
              ><MoneyAmount
                v-else
                :amount="row.estimate_amount"
                :unit="row.estimate_currency"
              /><small>{{ estimateStates[row.estimate_status ?? 'unavailable'] }}</small></template
            ><template v-else
              ><span v-if="row.cost_amount == null || !row.cost_unit">未知</span
              ><MoneyAmount v-else :amount="row.cost_amount" :unit="row.cost_unit" /><small>{{
                costCheckStates[row.cost_check_status] ?? row.cost_check_status
              }}</small></template
            ></template
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
