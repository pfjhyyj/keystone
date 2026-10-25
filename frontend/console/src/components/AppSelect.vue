<script setup lang="ts">
// Keep native form validation while using Element Plus's accessible combobox.
import { ref, useAttrs } from 'vue'
import { ElSelect, type SelectInstance } from 'element-plus'
defineOptions({ inheritAttrs: false })
const props = defineProps<{ required?: boolean; disabled?: boolean }>()
const model = defineModel<string>({ required: true })
const attrs = useAttrs()
const select = ref<SelectInstance>()
const invalid = ref(false)
function update(value: unknown) {
  model.value = value == null ? '' : String(value)
  invalid.value = false
}
function validate() {
  invalid.value = true
  select.value?.focus()
}
</script>
<template>
  <div class="app-select">
    <ElSelect
      ref="select"
      v-bind="attrs"
      :model-value="model"
      :disabled="disabled"
      :teleported="false"
      :aria-invalid="invalid"
      @update:model-value="update"
    >
      <slot />
      <template v-if="$slots.label" #label="value"><slot name="label" v-bind="value" /></template>
    </ElSelect>
    <input
      v-if="props.required"
      class="select-validation"
      :value="model"
      required
      :disabled="disabled"
      tabindex="-1"
      aria-hidden="true"
      @invalid.prevent="validate"
      @focus="select?.focus()"
    />
    <span v-if="invalid" class="field-error" role="alert"
      >请选择{{ attrs['aria-label'] || '选项' }}</span
    >
  </div>
</template>
