<script setup lang="ts">
import { type UsageMetrics, percent, duration } from '../../../api/modelUsage'
import MoneyAmount from '../../../components/MoneyAmount.vue'
withDefaults(defineProps<{ value: UsageMetrics; costBasis?: 'actual' | 'estimated' }>(), {
  costBasis: 'estimated',
})
defineEmits<{ drill: [filters: Record<string, string>] }>()
const count = (value: number) => value.toLocaleString('zh-CN')
const tokens = (value: string | null | undefined) =>
  value == null ? '未报告' : value.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
const coverage = (reported: number | undefined, total: number) =>
  reported == null ? '未报告' : `${count(reported)}/${count(total)}`
</script>
<template>
  <section aria-label="用量费用总览" class="usage-cards">
    <article class="usage-card">
      <h2>{{ costBasis === 'estimated' ? '预估费用' : '上游实扣' }}</h2>
      <p v-for="cost in value.costs" :key="cost.unit">
        <strong><MoneyAmount :amount="cost.amount" :unit="cost.unit" /></strong>
      </p>
      <p v-if="!value.costs.length">暂无已知费用</p>
      <small>{{
        costBasis === 'estimated'
          ? '币种分别统计，缺失计量不作为零费用。'
          : '单位分别统计，包含已收费的失败调用。'
      }}</small>
    </article>
    <article class="usage-card">
      <h2>调用次数</h2>
      <strong>{{ count(value.calls) }}</strong>
      <p>成功率 {{ percent(value.succeeded, value.calls) }}</p>
      <p>
        成功 {{ count(value.succeeded) }} · 失败 {{ count(value.failed) }} · 结果未知
        {{ count(value.unknown) }} · 进行中 {{ count(value.running) }}
      </p>
    </article>
    <article class="usage-card">
      <h2>费用覆盖率</h2>
      <strong>{{ percent(value.known_calls, value.calls) }}</strong>
      <p>{{ count(value.known_calls) }} / {{ count(value.calls) }} 次已有金额</p>
      <div class="coverage-actions">
        <el-button link native-type="button" @click="$emit('drill', { cost_known: 'false' })"
          >费用未知：{{ count(value.unknown_cost_calls) }} 次</el-button
        ><el-button
          link
          native-type="button"
          @click="
            $emit(
              'drill',
              costBasis === 'estimated'
                ? { estimate_status: 'pending' }
                : { cost_status: 'pending' },
            )
          "
          >{{ costBasis === 'estimated' ? '等待计量' : '等待核对' }}：{{
            count(value.pending_calls)
          }}
          次</el-button
        >
      </div>
    </article>
  </section>
  <slot />
  <details class="usage-details">
    <summary>Token、耗时与覆盖明细</summary>
    <div class="usage-detail-grid">
      <article class="usage-card">
        <h2>输入 / 输出 Token</h2>
        <strong
          class="token-total"
          :title="`${value.input_tokens ?? '未报告'} / ${value.output_tokens ?? '未报告'}`"
          >{{ tokens(value.input_tokens) }} / {{ tokens(value.output_tokens) }}</strong
        >
        <p>
          输入覆盖 {{ count(value.input_reported) }}/{{ count(value.calls) }} · 输出覆盖
          {{ count(value.output_reported) }}/{{ count(value.calls) }}
        </p>
        <p>
          缓存读取 {{ tokens(value.cache_read_tokens) }} · 缓存写入
          {{ tokens(value.cache_write_tokens) }}
        </p>
        <small
          >缓存读取覆盖 {{ coverage(value.cache_read_reported, value.calls) }} · 缓存写入覆盖
          {{ coverage(value.cache_write_reported, value.calls) }}。未报告不代表零用量。</small
        >
      </article>
      <article class="usage-card">
        <h2>平均调用耗时</h2>
        <strong>{{ duration(value.avg_duration_ms) }}</strong>
        <p>平均 · P95 {{ duration(value.p95_duration_ms) }}</p>
        <small>从登记到完成，{{ count(value.duration_reported) }} 次有完整时间</small>
      </article>
      <article v-if="value.estimate_coverage" class="usage-card">
        <h2>预估覆盖明细</h2>
        <p>计量不完整 {{ count(value.estimate_coverage.partial ?? 0) }}</p>
        <p>无法预估 {{ count(value.estimate_coverage.unavailable ?? 0) }}</p>
        <p>异常 {{ count(value.estimate_coverage.invalid ?? 0) }}</p>
      </article>
    </div>
  </details>
</template>
<style scoped>
.usage-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  border-top: 1px solid #e4e8ed;
  border-bottom: 1px solid #e4e8ed;
  margin: 12px 0;
}
.usage-card {
  padding: 12px 16px 12px 0;
  min-width: 0;
}
h2 {
  font-size: 14px;
  font-weight: 500;
  color: var(--el-text-color-secondary);
  margin: 0 0 8px;
}
strong {
  color: var(--el-text-color-primary);
  font-size: 24px;
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
  margin: 6px 0;
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
.usage-details {
  margin: 16px 0;
  padding: 12px 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.usage-details summary {
  cursor: pointer;
}
.usage-detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
  column-gap: 16px;
}
@media (max-width: 760px) {
  .usage-cards {
    grid-template-columns: minmax(0, 1fr);
  }
  .usage-cards .usage-card + .usage-card {
    border-top: 1px solid var(--el-border-color-lighter);
  }
}
</style>
