<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  dimensions,
  type Dimension,
  type UsageGroup,
  type UsagePoint,
} from '../../../api/modelUsage'
import { money } from '../../../api/models'
import AppSelect from '../../../components/AppSelect.vue'
import { bucketStarts, groupLabel, knownAmount } from '../usageSeries'
const props = defineProps<{
  groups: UsageGroup[]
  total: number
  dimension: Dimension
  since: string
  until: string
  timezone: string
  bucket: string
  units: string[]
  loading: boolean
  exporting: boolean
}>()
const emit = defineEmits<{ drill: [filters: Record<string, string>]; export: [] }>()
// Fixed categorical order (validated for adjacent CVD separation); never cycled.
const palette = [
  '#2a78d6',
  '#eb6834',
  '#1baf7a',
  '#eda100',
  '#e87ba4',
  '#008300',
  '#6250d6',
  '#e34948',
]
const metric = ref('calls')
watch(
  () => props.units,
  (units) => {
    if (metric.value !== 'calls' && !units.includes(metric.value))
      metric.value = units[0] ?? 'calls'
  },
)
const shown = computed(() => props.groups.slice(0, palette.length))
const starts = computed(() => bucketStarts(props.since, props.until, props.bucket))
const byStart = computed(() =>
  shown.value.map(
    (group) =>
      new Map((group.trend ?? []).map((point) => [new Date(point.start).getTime(), point])),
  ),
)
// No point in a bucket means the group had no calls there: zero calls and zero cost.
function value(point: UsagePoint | undefined): number | null {
  if (!point) return 0
  if (metric.value === 'calls') return point.calls
  const amount = knownAmount(point, metric.value)
  return amount === null ? null : Number(amount)
}
const values = computed(() =>
  byStart.value.map((points) => starts.value.map((start) => value(points.get(start)))),
)
const maximum = computed(() =>
  Math.max(0, ...values.value.flat().filter((item): item is number => item !== null)),
)
// The drawing uses the plot's real pixel width so marks keep their size on narrow screens.
const plot = ref<HTMLElement | null>(null),
  width = ref(900)
let observer: ResizeObserver | null = null
onMounted(() => {
  if (typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(([entry]) => {
    width.value = Math.max(240, Math.round(entry?.contentRect.width ?? 900))
  })
  if (plot.value) observer.observe(plot.value)
})
onBeforeUnmount(() => observer?.disconnect())
const left = 12,
  top = 14,
  bottom = 196
const right = computed(() => width.value - 12)
const x = (index: number) =>
  starts.value.length > 1
    ? left + (index / (starts.value.length - 1)) * (right.value - left)
    : (left + right.value) / 2
const y = (item: number) => bottom - (maximum.value ? (item / maximum.value) * (bottom - top) : 0)
// Unknown cost breaks the line instead of drawing a fabricated zero.
function path(series: (number | null)[]) {
  let d = '',
    pen = false
  series.forEach((item, index) => {
    if (item === null) pen = false
    else {
      d += `${pen ? 'L' : 'M'}${x(index).toFixed(1)},${y(item).toFixed(1)}`
      pen = true
    }
  })
  return d
}
const markers = computed(() => starts.value.length <= 62)
const active = ref<number | null>(null)
const stamp = (start: number, withYear = true) =>
  new Date(start).toLocaleString('zh-CN', {
    timeZone: props.timezone,
    ...(withYear || props.bucket === 'month' ? { year: 'numeric' as const } : {}),
    month: '2-digit',
    ...(props.bucket === 'month' ? {} : { day: '2-digit' as const }),
    ...(props.bucket === 'hour' ? { hour: '2-digit' as const, minute: '2-digit' as const } : {}),
    hour12: false,
  })
const ticks = computed(() => {
  const count = starts.value.length
  if (!count) return []
  const picks =
    count < 5
      ? [...Array(count).keys()]
      : [0, 1, 2, 3, 4].map((i) => Math.round(((count - 1) * i) / 4))
  return [...new Set(picks)].map((index) => ({ index, text: stamp(starts.value[index]!, false) }))
})
function reading(index: number, group: number) {
  const point = byStart.value[group]!.get(starts.value[index]!)
  if (metric.value === 'calls') return `${point?.calls ?? 0} 次`
  if (!point) return `0 ${metric.value}`
  const amount = knownAmount(point, metric.value)
  if (amount === null) return '费用未知'
  const suffix = point.unknown_cost_calls ? `（另 ${point.unknown_cost_calls} 次未知）` : ''
  return `${money(amount)} ${metric.value}${suffix}`
}
function groupTotal(group: UsageGroup) {
  if (metric.value === 'calls') return `${group.calls} 次`
  const cost = group.costs.find((item) => item.unit === metric.value)
  return cost
    ? `${money(cost.amount)} ${cost.unit}`
    : group.unknown_cost_calls
      ? '费用未知'
      : `0 ${metric.value}`
}
const magnitude = (item: number) =>
  !item
    ? '0'
    : item < 0.0001
      ? item.toExponential(1)
      : new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 3 }).format(item)
function pointer(event: PointerEvent) {
  const box = (event.currentTarget as SVGElement).getBoundingClientRect()
  const position = event.clientX - box.left
  const count = starts.value.length
  if (!count) return
  active.value = Math.min(
    count - 1,
    Math.max(0, Math.round(((position - left) / (right.value - left)) * (count - 1))),
  )
}
function step(delta: number) {
  const count = starts.value.length
  if (!count) return
  active.value = Math.min(
    count - 1,
    Math.max(0, (active.value ?? (delta > 0 ? -1 : count)) + delta),
  )
}
</script>
<template>
  <section aria-label="分组趋势" class="usage-panel">
    <div class="heading">
      <h2>分组趋势：{{ dimensions[dimension] }}</h2>
      <div class="trend-controls">
        <div class="trend-metric">
          <AppSelect v-model="metric" aria-label="分组趋势指标"
            ><el-option label="调用次数" value="calls" /><el-option
              v-for="unit in units"
              :key="unit"
              :label="`已知费用 · ${unit}`"
              :value="unit"
          /></AppSelect>
        </div>
        <el-button
          native-type="button"
          :loading="exporting"
          :disabled="loading"
          @click="emit('export')"
          >导出 CSV</el-button
        >
      </div>
    </div>
    <p>
      按当前排序绘制前 {{ shown.length }} 组<template v-if="total > shown.length"
        >（共 {{ total }} 组，其余见下方表格或导出）</template
      >。费用仅含该单位已知金额，不同单位分别查看；线段中断表示该时段费用未知。导出包含全部分组和时段。
    </p>
    <template v-if="shown.length"
      ><div class="chart-shell" :class="{ stale: loading }">
        <div class="trend-scale" aria-hidden="true">
          <span>{{ magnitude(maximum) }}</span
          ><span>{{ magnitude(maximum / 2) }}</span
          ><span>0</span>
        </div>
        <div ref="plot" class="plot">
          <svg
            :viewBox="`0 0 ${width} 210`"
            role="img"
            tabindex="0"
            :aria-label="`${dimensions[dimension]}分组趋势图，可用左右方向键逐个时段查看数值`"
            @pointermove="pointer"
            @pointerleave="active = null"
            @blur="active = null"
            @keydown.right.prevent="step(1)"
            @keydown.left.prevent="step(-1)"
          >
            <line
              v-for="level in [top, (top + bottom) / 2]"
              :key="level"
              :x1="left"
              :x2="right"
              :y1="level"
              :y2="level"
              class="grid"
            />
            <line :x1="left" :x2="right" :y1="bottom" :y2="bottom" class="axis" />
            <line
              v-if="active !== null"
              :x1="x(active)"
              :x2="x(active)"
              :y1="top"
              :y2="bottom"
              class="crosshair"
            />
            <g v-for="(series, index) in values" :key="shown[index]!.key ?? '__none__'">
              <path
                :d="path(series)"
                fill="none"
                :stroke="palette[index]"
                stroke-width="2"
                stroke-linejoin="round"
                stroke-linecap="round"
              />
              <template v-for="(item, position) in series" :key="position">
                <circle
                  v-if="item !== null && (markers || active === position)"
                  :cx="x(position)"
                  :cy="y(item)"
                  r="4"
                  :fill="palette[index]"
                  class="marker"
                />
                <circle
                  v-else-if="item === null"
                  :cx="x(position)"
                  :cy="bottom"
                  r="4"
                  fill="#ffffff"
                  :stroke="palette[index]"
                  stroke-width="1.5"
                  stroke-dasharray="2 2"
                />
              </template>
            </g>
          </svg>
          <div
            v-if="active !== null"
            class="trend-tooltip"
            role="status"
            :style="{ left: `${x(active)}px` }"
            :class="{ flip: x(active) > width * 0.6 }"
          >
            <strong>{{ stamp(starts[active]!) }}</strong>
            <p v-for="(group, index) in shown" :key="group.key ?? '__none__'">
              <span class="key" :style="{ background: palette[index] }" /><b>{{
                reading(active, index)
              }}</b
              ><span class="name">{{ groupLabel(dimension, group) }}</span>
            </p>
          </div>
          <div class="trend-axis" aria-hidden="true">
            <span v-for="tick in ticks" :key="tick.index" :style="{ left: `${x(tick.index)}px` }">{{
              tick.text
            }}</span>
          </div>
        </div>
      </div>
      <ul class="legend" aria-label="图例，点击查看该组明细">
        <li v-for="(group, index) in shown" :key="group.key ?? '__none__'">
          <el-button
            link
            native-type="button"
            :title="group.key ?? group.label"
            @click="emit('drill', group.filters)"
            ><span class="key" :style="{ background: palette[index] }" /><span class="name">{{
              groupLabel(dimension, group)
            }}</span
            ><small>{{ groupTotal(group) }}</small></el-button
          >
        </li>
      </ul></template
    >
    <el-empty v-else description="该时间范围内没有调用" :image-size="64" />
  </section>
</template>
<style scoped>
.usage-panel {
  padding: 18px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  background: var(--el-bg-color);
  margin-bottom: 20px;
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
.trend-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}
.trend-metric {
  width: 200px;
  max-width: 100%;
}
.chart-shell {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 6px;
  transition: opacity 0.15s;
}
.chart-shell.stale {
  opacity: 0.55;
}
.plot {
  position: relative;
  min-width: 0;
}
svg {
  display: block;
  width: 100%;
  height: 210px;
  outline: none;
  touch-action: pan-y;
}
svg:focus-visible {
  box-shadow: 0 0 0 2px var(--el-color-primary-light-5);
  border-radius: 4px;
}
.grid,
.axis {
  stroke: #e8ecf1;
  stroke-width: 1;
}
.axis {
  stroke: #dce2e9;
}
.crosshair {
  stroke: #617084;
  stroke-width: 1;
}
.marker {
  stroke: #ffffff;
  stroke-width: 2;
}
.trend-scale {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  text-align: right;
  box-sizing: border-box;
  height: 210px;
  padding: 6px 0;
  color: #617084;
  font-size: 12px;
}
.trend-axis {
  position: relative;
  height: 20px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.trend-axis > span {
  position: absolute;
  top: 2px;
  transform: translateX(-50%);
  white-space: nowrap;
}
.trend-axis > span:first-child {
  transform: none;
}
.trend-axis > span:last-child:not(:first-child) {
  transform: translateX(-100%);
}
.trend-tooltip {
  position: absolute;
  top: 0;
  z-index: 2;
  min-width: 180px;
  max-width: 280px;
  padding: 8px 10px;
  border: 1px solid #e4e8ed;
  border-radius: 6px;
  background: #ffffff;
  box-shadow: 0 4px 12px rgb(29 43 60 / 12%);
  transform: translateX(12px);
  pointer-events: none;
  font-size: 12px;
  color: #526173;
}
.trend-tooltip.flip {
  transform: translateX(calc(-100% - 12px));
}
.trend-tooltip strong {
  display: block;
  margin-bottom: 4px;
  color: #1d2b3c;
}
.trend-tooltip p {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 2px 0;
}
.trend-tooltip b {
  color: #1d2b3c;
  font-variant-numeric: tabular-nums;
}
.trend-tooltip .name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.key {
  flex: none;
  display: inline-block;
  width: 14px;
  height: 2px;
  border-radius: 1px;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}
.legend :deep(.el-button) {
  gap: 6px;
  color: #263445;
  max-width: 100%;
}
.legend .name {
  margin-left: 6px;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.legend small {
  margin-left: 6px;
  color: #617084;
}
@media (max-width: 600px) {
  .trend-axis > span:not(:first-child):not(:last-child) {
    display: none;
  }
}
</style>
