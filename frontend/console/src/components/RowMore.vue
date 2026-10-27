<script setup lang="ts">
import { ref } from 'vue'
import { ElPopover } from 'element-plus'
import { MoreFilled } from '@element-plus/icons-vue'
withDefaults(defineProps<{ label?: string }>(), { label: '更多操作' })
const opened = ref(false)
</script>
<template>
  <ElPopover
    v-model:visible="opened"
    trigger="click"
    placement="bottom-end"
    :width="190"
    popper-class="row-more-popover"
  >
    <template #reference
      ><el-button text class="row-more-trigger" :aria-label="label" :aria-expanded="opened"
        ><el-icon><MoreFilled /></el-icon><span>更多</span></el-button
      ></template
    >
    <div
      class="row-more-actions"
      :aria-label="label"
      @click="opened = false"
      @keydown.esc="opened = false"
    >
      <slot />
    </div>
  </ElPopover>
</template>
