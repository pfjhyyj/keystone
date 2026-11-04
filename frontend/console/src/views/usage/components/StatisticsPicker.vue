<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { can, identity } from '../../../stores/session'
import AppSelect from '../../../components/AppSelect.vue'
import { uuidPattern } from '../../../api/client'
import {
  dimensionFilters,
  usageApi,
  type Dimension,
  type UsageOption,
} from '../../../api/modelUsage'
const props = defineProps<{
  dimension: Dimension
  label: string
  filters: Record<string, string>
}>()
const model = defineModel<string>({ required: true })
const rows = ref<UsageOption[]>([]),
  selected = ref<UsageOption | null>(null)
const loading = ref(false),
  resolving = ref(false),
  more = ref(false),
  search = ref('')
const queryError = ref(''),
  labelError = ref('')
const scope = computed(() =>
  JSON.stringify([
    props.dimension,
    identity.value?.user.id,
    can('model_invocation:read'),
    can('model_invocation:read_all'),
  ]),
)
const context = computed(() =>
  JSON.stringify(
    Object.entries(props.filters)
      .filter(([key]) => key !== dimensionFilters[props.dimension])
      .map(([key, value]) => [
        key,
        key === 'missing'
          ? value
              .split(',')
              .filter((item) => item !== props.dimension)
              .sort()
              .join(',')
          : value,
      ])
      .filter(([, value]) => value)
      .sort(([a], [b]) => a!.localeCompare(b!)),
  ),
)
const requestedKey = computed(() => `${context.value}\n${search.value}`)
const loadedKey = ref('')
const currentResults = computed(() => loadedKey.value === requestedKey.value)
const selectionLabel = computed(() => {
  const known =
    selected.value?.id === model.value
      ? selected.value
      : rows.value.find((row) => row.id === model.value)
  if (known) return known.label
  if (!new RegExp(`^${uuidPattern}$`).test(model.value)) return model.value
  return resolving.value ? '正在读取名称…' : `名称不可用（${model.value}）`
})
const pinned = computed(() =>
  model.value && !rows.value.some((row) => row.id === model.value)
    ? {
        id: model.value,
        label: selectionLabel.value,
      }
    : null,
)
const manual = computed(() => {
  const value = search.value.trim()
  const needsUUID = ['channel', 'model', 'user', 'flow_run'].includes(props.dimension)
  if (
    !value ||
    value === model.value ||
    rows.value.some((row) => row.id === value || row.label === value)
  )
    return ''
  if (
    needsUUID &&
    !new RegExp(`^${uuidPattern}$`).test(value) &&
    !(props.dimension === 'user' && value === 'system')
  )
    return ''
  return value
})
let alive = true,
  opened = false,
  generation = 0,
  labelGeneration = 0,
  nextOffset = 0
let activeKey = '',
  timer: ReturnType<typeof setTimeout> | undefined
function remember(row: UsageOption) {
  selected.value = { ...row }
  labelGeneration++
  resolving.value = false
  labelError.value = ''
}
async function resolveLabel() {
  const value = model.value
  if (!value || selected.value?.id === value || !can('model_invocation:read')) return
  const known = rows.value.find((row) => row.id === value)
  if (known) {
    remember(known)
    return
  }
  const token = ++labelGeneration
  resolving.value = true
  labelError.value = ''
  try {
    // Label resolution is independent of the dropdown's current search and pagination.
    const result = await usageApi.options(props.dimension, props.filters, value, 0)
    if (
      !alive ||
      token !== labelGeneration ||
      model.value !== value ||
      !can('model_invocation:read')
    )
      return
    const row = result.items.find((item) => item.id === value)
    if (row) remember(row)
  } catch (reason) {
    if (alive && token === labelGeneration)
      labelError.value = reason instanceof Error ? reason.message : '名称查询失败'
  } finally {
    if (alive && token === labelGeneration) resolving.value = false
  }
}
function request(q = '', append = false, delay = 0) {
  if (!can('model_invocation:read')) return
  const key = `${context.value}\n${q}`
  if (
    !append &&
    ((activeKey === key && loading.value) || (loadedKey.value === key && !queryError.value))
  ) {
    if (search.value !== q) {
      generation++
      clearTimeout(timer)
      loading.value = false
      activeKey = ''
      search.value = q
    }
    return
  }
  clearTimeout(timer)
  const token = ++generation,
    offset = append ? nextOffset : 0
  search.value = q
  activeKey = key
  loading.value = true
  queryError.value = ''
  async function fetchOptions() {
    try {
      const result = await usageApi.options(props.dimension, props.filters, q, offset)
      if (!alive || token !== generation || !can('model_invocation:read')) return
      nextOffset = offset + result.items.length
      // Replace the menu atomically; keep the selected name and old choices while loading.
      rows.value = [
        ...new Map(
          (append ? [...rows.value, ...result.items] : result.items).map((row) => [row.id, row]),
        ).values(),
      ]
      loadedKey.value = key
      more.value = result.has_more && nextOffset <= 100000
      const row = result.items.find((item) => item.id === model.value)
      if (row) remember(row)
    } catch (reason) {
      if (alive && token === generation)
        queryError.value = reason instanceof Error ? reason.message : '选项查询失败'
    } finally {
      if (alive && token === generation) {
        loading.value = false
        activeKey = ''
      }
    }
  }
  if (delay) timer = setTimeout(() => void fetchOptions(), delay)
  else void fetchOptions()
}
function choose(value: string) {
  const row =
    rows.value.find((item) => item.id === value) ??
    (selected.value?.id === value ? selected.value : null)
  selected.value = row ? { ...row } : null
  model.value = value
}
function visible(value: boolean) {
  opened = value
  if (value) request('')
}
watch(
  [scope, context, () => model.value],
  ([nowScope, nowContext, value], previous) => {
    const [oldScope, oldContext, oldValue] = previous ?? []
    if (nowScope !== oldScope) {
      rows.value = []
      selected.value = null
      loadedKey.value = ''
    }
    if (nowScope !== oldScope || nowContext !== oldContext) {
      loadedKey.value = ''
      generation++
      labelGeneration++
      clearTimeout(timer)
      loading.value = resolving.value = more.value = false
      queryError.value = labelError.value = activeKey = ''
      nextOffset = 0
      if (opened && can('model_invocation:read')) request(search.value)
    }
    if (value !== oldValue && selected.value?.id !== value) {
      selected.value = null
      labelGeneration++
      resolving.value = false
      labelError.value = ''
    }
    if (!value) {
      selected.value = null
      labelGeneration++
      resolving.value = false
    } else void resolveLabel()
  },
  { immediate: true, flush: 'sync' },
)
onScopeDispose(() => {
  alive = false
  generation++
  labelGeneration++
  clearTimeout(timer)
})
</script>
<template>
  <div class="statistics-picker">
    <AppSelect
      :model-value="model"
      :aria-label="label"
      filterable
      remote
      clearable
      default-first-option
      :remote-method="(q: string) => request(q.trim(), false, 250)"
      :loading="loading && !rows.length && !selected"
      placeholder="搜索名称或输入完整 ID"
      @update:model-value="choose"
      @visible-change="visible"
    >
      <!-- Remote ElSelect caches option labels. Render the resolved name reactively. -->
      <template #label
        ><span :title="selectionLabel">{{ selectionLabel }}</span></template
      >
      <el-option
        v-for="row in rows"
        :key="row.id"
        :value="row.id"
        :label="row.label"
        :title="row.label"
        :disabled="!currentResults"
      />
      <el-option v-if="manual" :value="manual" :label="`使用 ID：${manual}`" />
      <el-option
        v-if="pinned"
        :value="pinned.id"
        :label="pinned.label"
        :title="pinned.label"
        :disabled="resolving"
      >
        <span>{{ pinned.label }}</span
        ><span class="selection-hint">当前已选</span>
      </el-option>
    </AppSelect>
    <el-button
      v-if="more && currentResults"
      :disabled="loading"
      @click="request(search, true)"
      link
      native-type="button"
      >加载更多{{ label }}</el-button
    >
    <small v-if="queryError || labelError" role="alert">{{ queryError || labelError }}</small>
  </div>
</template>
<style scoped>
.selection-hint {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
