<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'

const props = withDefaults(
  defineProps<{ page: number; count: number; loading: boolean; size?: number }>(),
  { size: 50 },
)
const emit = defineEmits<{ change: [page: number] }>()

const input = ref('')
const invalid = ref(false)
const errorId = useId()
const maxPage = computed(() =>
  props.count < props.size ? props.page + 1 : Math.floor(100000 / props.size) + 1,
)
watch(
  () => props.page,
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
      <span>第 {{ page + 1 }} 页 · 本页 {{ count }} 条</span>
      <el-button :disabled="loading || page + 1 >= maxPage" @click="emit('change', page + 1)">
        下一页
      </el-button>
    </div>
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
  </div>
</template>

<style scoped>
.list-pager,
.page-controls,
.jump-controls {
  flex-wrap: wrap;
}
.list-pager {
  font-variant-numeric: tabular-nums;
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
