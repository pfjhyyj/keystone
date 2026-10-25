<script setup lang="ts">
import { ElDialog, ElDrawer } from 'element-plus'
const props = withDefaults(
  defineProps<{
    title: string
    busy?: boolean
    drawer?: boolean
    width?: string
    appendToBody?: boolean
  }>(),
  { width: '620px', appendToBody: false },
)
const emit = defineEmits<{ close: [] }>()
function close() {
  if (!props.busy) emit('close')
}
</script>
<template>
  <component
    :is="drawer ? ElDrawer : ElDialog"
    :model-value="true"
    :title="title"
    :width="drawer ? undefined : width"
    :size="drawer ? 'min(760px, 100vw)' : undefined"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="false"
    :append-to-body="props.appendToBody"
    class="app-dialog"
    destroy-on-close
    @update:model-value="close"
  >
    <template #header="{ titleId }">
      <div class="dialog-heading">
        <h2 :id="titleId">{{ title }}</h2>
        <el-button text :disabled="busy" aria-label="关闭对话框" @click="close">关闭</el-button>
      </div>
    </template>
    <slot />
  </component>
</template>
