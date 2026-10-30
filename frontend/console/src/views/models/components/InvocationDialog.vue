<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { can } from '../../../stores/session'
import { request } from '../../../api/client'
import { callStates, costCheckStates, money, type Invocation } from '../../../api/models'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import { statisticsTime } from '../../usage/time'
import { feeSourceLabels } from '../../../api/modelUsage'
import { estimateStates, estimateReasons, quantityLabels } from '../../../api/pricing'
const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const row = ref<Invocation | null>(null)
const usageLabels: Record<string, string> = {
  input_tokens: '输入 Token',
  output_tokens: '输出 Token',
  total_tokens: '总 Token',
  prompt_tokens: '输入 Token',
  completion_tokens: '输出 Token',
  cached_tokens: '缓存 Token',
  reasoning_tokens: '推理 Token',
}
const usageFacts = computed(() =>
  Object.entries(row.value?.usage ?? {}).filter(
    ([key, value]) =>
      key in usageLabels && (typeof value === 'number' || typeof value === 'string'),
  ),
)
const { busy, error, run } = useAction()
async function load() {
  row.value = null
  row.value = await request<Invocation>(`/model-invocations/${props.id}`)
}
async function cost() {
  await run(async () => {
    await request(`/model-invocations/${props.id}/cost`, 'POST')
    await load()
    emit('changed')
  })
}
onMounted(() => run(load))
</script>
<template>
  <AppDialog drawer title="调用详情" :busy="busy" @close="$emit('close')"
    ><el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <p v-if="busy" role="status">正在处理…</p>
    <template v-if="row">
      <div class="detail-summary">
        <div class="detail-title">
          <h2>{{ row.model_name ?? row.provider_model }}</h2>
          <StatusTag :status="row.status">{{ callStates[row.status] }}</StatusTag>
        </div>
        <p>
          {{ row.channel_name ?? row.channel_id }} · {{ row.user_name ?? row.user_id ?? '系统' }}
        </p>
        <p v-if="row.error_code" class="detail-error">{{ row.error_code }}</p>
        <div class="fee-focus">
          <span>预估费用</span
          ><strong>{{
            row.estimate_amount == null
              ? '暂无法预估'
              : `${money(row.estimate_amount)} ${row.estimate_currency}`
          }}</strong>
          <StatusTag :status="row.estimate_status ?? 'unavailable'">{{
            estimateStates[row.estimate_status ?? 'unavailable']
          }}</StatusTag>
        </div>
        <p>
          {{
            estimateReasons[row.estimate_reason ?? ''] ??
            row.estimate_reason ??
            '按登记时价格与真实计量计算，不代表上游实扣。'
          }}
        </p>
        <p v-if="row.estimate_source?.label">
          {{ row.estimate_source.label }} · {{ row.estimate_source.version }} · 配置版本
          {{ row.pricing_revision }}
        </p>
        <details v-if="row.estimate_detail?.items?.length">
          <summary>
            预估计算分项<span v-if="row.estimate_detail.leaf">
              · {{ row.estimate_detail.leaf }}</span
            >
          </summary>
          <dl v-for="item in row.estimate_detail.items" :key="item.id">
            <dt>{{ quantityLabels[item.quantity] ?? item.quantity }}</dt>
            <dd>
              {{ item.value ?? '计量缺失' }} × {{ item.rate }} / {{ item.per }} =
              {{ item.amount === null ? '无法计算' : money(item.amount) }}
              {{ row.estimate_currency }}
            </dd>
          </dl>
          <p v-if="row.estimate_detail.factor">
            价格系数：{{ row.estimate_detail.factor }}；总额统一舍入至 18 位小数。
          </p>
          <p v-if="row.estimate_detail.missing?.length">
            缺失：{{
              row.estimate_detail.missing.map((key) => quantityLabels[key] ?? key).join('、')
            }}
          </p>
          <p v-if="row.estimate_detail.time_basis">
            时间依据：{{ row.estimate_detail.time_basis.source }} ·
            {{ row.estimate_detail.time_basis.timezone }}
          </p>
        </details>
        <div class="fee-focus">
          <span>上游报告费用</span
          ><strong>{{
            row.cost_amount === null ? '费用未知' : `${money(row.cost_amount)} ${row.cost_unit}`
          }}</strong
          ><StatusTag v-if="row.cost_check_status" :status="row.cost_check_status">{{
            costCheckStates[row.cost_check_status] ?? row.cost_check_status
          }}</StatusTag>
        </div>
        <p>
          {{ feeSourceLabels[row.cost_source as keyof typeof feeSourceLabels] ?? row.cost_source
          }}{{ row.cost_note ? ` · ${row.cost_note}` : '' }}
        </p>
      </div>
      <div class="detail-actions">
        <el-button :disabled="busy" @click="run(load)" native-type="button" type="default"
          >刷新调用</el-button
        >
        <el-button
          v-if="can('model:manage') && row.status !== 'running'"
          :disabled="busy"
          @click="cost"
          type="primary"
          >补查费用</el-button
        >
      </div>
      <p class="detail-note">费用未知不代表未计费。补查只查询账务，不重新发送模型请求。</p>
      <h3>用量</h3>
      <dl v-if="usageFacts.length" class="token-facts">
        <template v-for="[key, value] in usageFacts" :key="key"
          ><dt>{{ usageLabels[key] }}</dt>
          <dd>{{ value }}</dd></template
        >
      </dl>
      <p v-else>未报告可显示的 Token 用量；未报告不代表零用量。</p>
      <details>
        <summary>用量原始字段</summary>
        <pre class="audit-details">{{ JSON.stringify(row.usage, null, 2) }}</pre>
      </details>
      <details>
        <summary>Flow 关联与技术标识</summary>
        <dl>
          <dt>调用 ID</dt>
          <dd>{{ row.id }}</dd>
          <dt>上游请求 ID</dt>
          <dd>{{ row.upstream_request_id ?? '未提供' }}</dd>
        </dl>
        <pre class="audit-details">{{ JSON.stringify(row.flow_context, null, 2) }}</pre>
      </details>
      <details>
        <summary>时间与费用核对记录</summary>
        <dl>
          <dt>登记 / 完成</dt>
          <dd>
            {{ statisticsTime(row.created_at) }} /
            {{ row.finished_at ? statisticsTime(row.finished_at) : '尚未完成' }}
          </dd>
          <dt>核对原因</dt>
          <dd>{{ row.cost_check_reason ?? '无补充原因' }}</dd>
          <dt>最近 / 下次核对</dt>
          <dd>
            {{ row.cost_checked_at ? statisticsTime(row.cost_checked_at) : '暂无' }} /
            {{ row.cost_next_check_at ? statisticsTime(row.cost_next_check_at) : '暂无' }}
          </dd>
        </dl>
      </details>
    </template>
  </AppDialog>
</template>
