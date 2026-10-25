<script setup lang="ts">
import { type UsageMetrics, percent, duration } from '../../../api/modelUsage'
import MoneyAmount from '../../../components/MoneyAmount.vue'
defineProps<{ value: UsageMetrics }>()
defineEmits<{ drill: [filters: Record<string, string>] }>()
</script>
<template>
  <section aria-label="用量费用总览" class="usage-cards">
    <article class="usage-card">
      <h2>已知费用</h2>
      <p v-for="cost in value.costs" :key="cost.unit">
        <strong><MoneyAmount :amount="cost.amount" :unit="cost.unit" /></strong>
      </p>
      <p v-if="!value.costs.length">暂无已知费用</p>
      <small>单位分别统计，包含已收费的失败调用。</small>
    </article>
    <article class="usage-card">
      <h2>调用次数</h2>
      <strong>{{ value.calls }}</strong>
      <p>
        成功 {{ value.succeeded }} · 失败 {{ value.failed }} · 结果未知 {{ value.unknown }} · 进行中
        {{ value.running }}
      </p>
    </article>
    <article class="usage-card">
      <h2>成功率</h2>
      <strong>{{ percent(value.succeeded, value.calls) }}</strong>
      <p>成功调用 / 全部调用</p>
    </article>
    <article class="usage-card">
      <h2>费用覆盖率</h2>
      <strong>{{ percent(value.known_calls, value.calls) }}</strong>
      <p>{{ value.known_calls }} / {{ value.calls }} 次已有金额</p>
      <div class="coverage-actions">
        <el-button link native-type="button" @click="$emit('drill', { cost_known: 'false' })"
          >费用未知：{{ value.unknown_cost_calls }} 次</el-button
        ><el-button link native-type="button" @click="$emit('drill', { cost_status: 'pending' })"
          >等待核对：{{ value.pending_calls }} 次</el-button
        >
      </div>
    </article>
    <article class="usage-card">
      <h2>输入 / 输出 Token</h2>
      <strong
        class="token-total"
        :title="`${value.input_tokens ?? '未报告'} / ${value.output_tokens ?? '未报告'}`"
        >{{ value.input_tokens ?? '未报告' }} / {{ value.output_tokens ?? '未报告' }}</strong
      >
      <p>
        输入覆盖 {{ value.input_reported }}/{{ value.calls }} · 输出覆盖
        {{ value.output_reported }}/{{ value.calls }}
      </p>
    </article>
    <article class="usage-card">
      <h2>平均调用耗时</h2>
      <strong>{{ duration(value.avg_duration_ms) }}</strong>
      <p>平均 · P95 {{ duration(value.p95_duration_ms) }}</p>
      <small>从登记到完成；{{ value.duration_reported }} 次有完整时间。</small>
    </article>
  </section>
</template>
<style scoped>
.usage-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin: 20px 0;
}
.usage-card {
  padding: 18px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
}
h2 {
  font-size: 14px;
  font-weight: 500;
  color: var(--el-text-color-secondary);
  margin: 0 0 12px;
}
strong {
  color: var(--el-text-color-primary);
  font-size: 23px;
  font-variant-numeric: tabular-nums;
  max-width: 100%;
}
.token-total {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
p {
  margin: 10px 0;
}
.coverage-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.coverage-actions .el-button + .el-button {
  margin-left: 0;
}
small {
  color: var(--el-text-color-secondary);
}
@media (max-width: 1100px) {
  .usage-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 600px) {
  .usage-cards {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
