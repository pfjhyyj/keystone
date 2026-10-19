<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ status?: string }>()
const type = computed(() => {
  if (['active', 'succeeded', 'SUCCESS', 'ok', 'success'].includes(props.status ?? ''))
    return 'success'
  if (['failed', 'ERROR', 'error', 'denied'].includes(props.status ?? '')) return 'danger'
  if (['pending', 'running', 'uploading', 'deleting', 'unknown'].includes(props.status ?? ''))
    return 'warning'
  return 'info'
})
</script>
<template>
  <el-tag disable-transitions :type="type" effect="light" round size="small"><slot /></el-tag>
</template>
