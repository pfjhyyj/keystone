<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElDialog, ElDrawer } from 'element-plus'
import { useUnsavedChanges } from '../composables/useUnsavedChanges'
const props = withDefaults(
  defineProps<{
    title: string
    busy?: boolean
    drawer?: boolean
    width?: string
    appendToBody?: boolean
    protectChanges?: boolean
    dirty?: boolean
    page?: boolean
  }>(),
  { width: '620px', appendToBody: false },
)
const emit = defineEmits<{ close: [] }>()
const changed = ref(false)
const dirty = computed(() => props.dirty ?? changed.value)
const { confirmLeave } = useUnsavedChanges(computed(() => !!props.protectChanges && !!dirty.value))
async function close() {
  if (props.busy) return
  if ((await confirmLeave()) && !props.busy) emit('close')
}
</script>
<template>
  <component
    :is="page ? 'section' : drawer ? ElDrawer : ElDialog"
    :model-value="true"
    :title="title"
    :width="drawer ? undefined : width"
    :size="drawer ? 'min(620px, 100vw)' : undefined"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="false"
    :append-to-body="props.appendToBody"
    class="app-dialog"
    :class="{ 'page-editor': page }"
    :aria-label="page ? title : undefined"
    @input="changed = true"
    @change="changed = true"
    destroy-on-close
    @update:model-value="close"
  >
    <template #header="{ titleId }">
      <div class="dialog-heading">
        <h2 :id="titleId">{{ title }}</h2>
        <el-button text :disabled="busy" aria-label="关闭对话框" @click="close">关闭</el-button>
      </div>
    </template>
    <div v-if="page" class="editor-heading">
      <div>
        <el-button text @click="close">返回模型配置</el-button>
        <h1>{{ title }}</h1>
      </div>
      <el-button v-if="!$slots.footer" @click="close">取消</el-button>
    </div>
    <slot />
    <footer v-if="page && $slots.footer" class="page-editor-footer">
      <slot name="footer" :close="close" />
    </footer>
  </component>
</template>
