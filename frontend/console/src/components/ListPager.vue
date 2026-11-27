<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { MAX_LIST_OFFSET, PAGE_SIZES } from '../composables/usePagedList'
import AppSelect from './AppSelect.vue'

const props = withDefaults(
  defineProps<{
    page: number
    count: number
    loading: boolean
    size?: number
    total?: number | null
    sizeOptions?: readonly number[]
  }>(),
  { size: 50, total: null, sizeOptions: () => PAGE_SIZES },
)
const emit = defineEmits<{ change: [page: number]; 'size-change': [size: number] }>()

const input = ref('')
const invalid = ref(false)
const errorId = useId()
const pageCount = computed(() =>
  props.total === null ? null : Math.ceil(props.total / props.size),
)
const accessiblePages = computed(() => Math.floor(MAX_LIST_OFFSET / props.size) + 1)
const maxPage = computed(() =>
  pageCount.value === null
    ? props.count < props.size
      ? props.page + 1
      : accessiblePages.value
    : Math.min(pageCount.value, accessiblePages.value),
)
watch(
  () => [props.page, props.size],
  () => {
    input.value = ''
    invalid.value = false
  },
)

function jump() {
  if (props.loading) return
  const value = input.value.trim()
  const target = Number(value)
  invalid.value = !/^[0-9]+$/.test(value) || target < 1 || target > maxPage.value
  if (invalid.value) return
  if (target === props.page + 1) input.value = ''
  else emit('change', target - 1)
}
</script>

<template>
  <div class="list-pager">
    <div class="page-controls">
      <el-button :disabled="loading || page === 0" @click="emit('change', page - 1)">
        上一页
      </el-button>
      <span class="page-position" aria-live="polite">
        <strong>第 {{ pageCount === 0 ? 0 : page + 1 }} 页</strong>
        <template v-if="pageCount !== null"> / 共 {{ pageCount }} 页</template>
      </span>
      <el-button :disabled="loading || page + 1 >= maxPage" @click="emit('change', page + 1)">
        下一页
      </el-button>
    </div>
    <AppSelect
      :model-value="String(size)"
      :disabled="loading"
      aria-label="每页条数"
      class="page-size"
      @update:model-value="emit('size-change', Number($event))"
    >
      <el-option
        v-for="option in sizeOptions"
        :key="option"
        :value="String(option)"
        :label="`${option} 条 / 页`"
      />
    </AppSelect>
    <form v-if="page > 0 || maxPage > 1" class="jump-controls" @submit.prevent.stop="jump">
      <el-input
        v-model="input"
        inputmode="numeric"
        placeholder="页码"
        aria-label="跳转页码"
        :aria-invalid="invalid || undefined"
        :aria-describedby="invalid ? errorId : undefined"
        :disabled="loading"
        @input="invalid = false"
      />
      <el-button :disabled="loading" native-type="submit">跳转</el-button>
      <span v-if="invalid" :id="errorId" class="jump-error" role="alert">
        请输入 1–{{ maxPage }} 的整数页码
      </span>
    </form>
    <small v-if="pageCount !== null && pageCount > accessiblePages" class="page-limit">
      最多可翻到第 {{ accessiblePages }} 页；查询更早的记录请缩小筛选范围。
    </small>
  </div>
</template>

<style scoped>
.list-pager,
.page-controls,
.jump-controls {
  flex-wrap: wrap;
}
.list-pager {
  display: flex;
  align-items: center;
  gap: 8px 12px;
  font-variant-numeric: tabular-nums;
}
.page-position {
  color: var(--el-text-color-secondary);
}
.page-position strong {
  color: var(--el-text-color-primary);
}
.page-size {
  width: 112px;
}
.page-limit {
  flex-basis: 100%;
  color: var(--el-text-color-secondary);
}
.page-controls,
.jump-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}
.jump-controls .el-input {
  width: 72px;
}
.jump-error {
  color: var(--el-color-danger);
}
@media (max-width: 600px) {
  .list-pager .el-button,
  .list-pager .el-input {
    min-height: 40px;
  }
}
</style>
