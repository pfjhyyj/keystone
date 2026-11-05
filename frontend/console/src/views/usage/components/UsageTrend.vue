<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { type UsagePoint } from '../../../api/modelUsage'
import { money } from '../../../api/models'
import AppSelect from '../../../components/AppSelect.vue'
const props = defineProps<{
  points: UsagePoint[]
  units: string[]
  timezone: string
  since: string
  until: string
}>()
const emit = defineEmits<{ drill: [filters: Record<string, string>] }>()
const metric = ref('calls')
watch(
  () => props.units,
  (units) => {
    if (metric.value !== 'calls' && !units.includes(metric.value))
      metric.value = units[0] ?? 'calls'
  },
)
// A bucket without this unit is zero only when every call in it has a known cost.
function value(point: UsagePoint) {
  if (metric.value === 'calls') return point.calls
  const cost = point.costs.find((cost) => cost.unit === metric.value)
  if (cost) return Number(cost.amount)
  return point.unknown_cost_calls ? null : 0
}
const known = (point: UsagePoint) => value(point) ?? 0
function label(point: UsagePoint) {
  if (metric.value === 'calls') return `${point.calls} 次调用`
  const cost = point.costs.find((cost) => cost.unit === metric.value)
  if (cost) return `${money(cost.amount)} ${cost.unit}`
  return point.unknown_cost_calls ? '费用未知' : `0 ${metric.value}`
}
const maximum = computed(() => Math.max(...props.points.map(known), 1e-12))
const scaleMaximum = computed(() => Math.max(...props.points.map(known), 0))
const magnitude = (value: number) =>
  !value
    ? '0'
    : value < 0.0001
      ? value.toExponential(1)
      : new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 3 }).format(value)
const first = computed(() => new Date(props.since).getTime())
const last = computed(() => new Date(props.until).getTime())
const timeTicks = computed(() =>
  [0.25, 0.5, 0.75].map((fraction) =>
    new Date(first.value + (last.value - first.value) * fraction).toLocaleString('zh-CN', {
      timeZone: props.timezone,
      month: '2-digit',
      day: '2-digit',
      ...(last.value - first.value < 2 * 86400000 ? { hour: '2-digit' as const } : {}),
    }),
  ),
)
const axis = (stamp: string) =>
  new Date(stamp).toLocaleString('zh-CN', {
    timeZone: props.timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
const x = (point: UsagePoint) =>
  ((new Date(point.filters.since!).getTime() - first.value) /
    Math.max(1, last.value - first.value)) *
    850 +
  25
const width = (point: UsagePoint) =>
  Math.max(
    1,
    Math.min(
      40,
      ((new Date(point.filters.until!).getTime() - new Date(point.filters.since!).getTime()) /
        Math.max(1, last.value - first.value)) *
        850 -
        2,
    ),
  )
</script>
<template>
  <section aria-label="费用与调用趋势" class="usage-panel">
    <div class="heading">
      <h2>费用与调用趋势</h2>
      <div class="trend-control">
        <AppSelect v-model="metric" aria-label="趋势指标"
          ><el-option label="调用次数" value="calls" /><el-option
            v-for="unit in units"
            :key="unit"
            :label="`已知费用 · ${unit}`"
            :value="unit"
        /></AppSelect>
      </div>
    </div>
    <p>点击柱形查看该时段明细。费用仅含已知金额，虚线表示该时段费用未知。</p>
    <template v-if="points.length"
      ><div class="chart-shell">
        <div class="trend-scale" aria-hidden="true">
          <span>{{ magnitude(scaleMaximum) }}</span
          ><span>{{ magnitude(scaleMaximum / 2) }}</span
          ><span>0</span>
        </div>
        <svg
          viewBox="0 0 900 190"
          preserveAspectRatio="none"
          role="group"
          aria-label="可点击的时间趋势"
        >
          <line x1="25" y1="170" x2="875" y2="170" stroke="currentColor" opacity=".2" />
          <line
            v-for="y in [30, 100]"
            :key="y"
            x1="25"
            :y1="y"
            x2="875"
            :y2="y"
            stroke="currentColor"
            opacity=".1"
          />
          <g
            v-for="point in points"
            :key="point.start"
            role="button"
            tabindex="0"
            :aria-label="`${point.label} ${label(point)}，查看明细`"
            class="trend-bar"
            @click="emit('drill', point.filters)"
            @keydown.enter.prevent="emit('drill', point.filters)"
            @keydown.space.prevent="emit('drill', point.filters)"
          >
            <title>
              {{ point.label }}：{{ label(point) }}；{{ point.unknown_cost_calls }} 次费用未知
            </title>
            <rect
              v-if="value(point) === null"
              :x="x(point)"
              y="30"
              :width="width(point)"
              height="140"
              rx="2"
              fill="none"
              stroke="currentColor"
              stroke-dasharray="4 3"
              opacity=".5"
            />
            <rect
              v-else
              :x="x(point)"
              :y="170 - Math.max(3, (known(point) / maximum) * 140)"
              :width="width(point)"
              :height="Math.max(3, (known(point) / maximum) * 140)"
              rx="2"
              fill="currentColor"
            />
          </g>
        </svg>
      </div>
      <div class="trend-axis">
        <span>{{ axis(since) }}</span
        ><span v-for="(tick, index) in timeTicks" :key="index" class="intermediate-tick">{{
          tick
        }}</span
        ><span>{{ axis(until) }}</span>
      </div>
    </template>
    <el-empty v-else description="该时间范围内没有调用" :image-size="64" />
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
svg {
  display: block;
  width: 100%;
  height: 200px;
  color: var(--el-color-primary);
}
.trend-control {
  width: 230px;
  max-width: 100%;
}
.chart-shell {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 6px;
}
.trend-scale {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  text-align: right;
  padding: 22px 0 20px;
  color: #617084;
  font-size: 12px;
}
.trend-axis {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  padding-left: 44px;
}
.trend-axis > span {
  min-width: 0;
  max-width: 27%;
}
.intermediate-tick {
  align-self: center;
}
@media (max-width: 600px) {
  .intermediate-tick {
    display: none;
  }
}
.trend-axis span:last-child {
  text-align: right;
}
.trend-bar {
  cursor: pointer;
}
.trend-bar:hover,
.trend-bar:focus {
  color: var(--el-color-success);
  outline: none;
}
</style>
