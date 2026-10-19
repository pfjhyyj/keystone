<script setup lang="ts">
withDefaults(defineProps<{ page: number; count: number; loading: boolean; size?: number }>(), {
  size: 50,
})
defineEmits<{ change: [page: number] }>()
</script>
<template>
  <div class="list-pager">
    <span>第 {{ page + 1 }} 页 · 本页 {{ count }} 条</span>
    <div class="row-actions">
      <el-button
        :disabled="loading || page === 0"
        @click="$emit('change', page - 1)"
        native-type="button"
        type="default"
      >
        上一页
      </el-button>
      <el-button
        :disabled="loading || count < size || (page + 1) * size > 100000"
        @click="$emit('change', page + 1)"
        native-type="button"
        type="default"
      >
        下一页
      </el-button>
    </div>
  </div>
</template>
