<script setup lang="ts">
// Adapted from Ether's native dialog: focus containment, Escape and busy protection.
import { onBeforeUnmount, ref, useId, watch } from 'vue'
const props = defineProps<{ title: string; busy?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const element = ref<HTMLDialogElement>()
const titleId = useId()
watch(element, value => value?.showModal(), { flush: 'post' })
onBeforeUnmount(() => element.value?.close())
function cancel(event: Event) { event.preventDefault(); if (!props.busy) emit('close') }
</script>
<template>
  <dialog ref="element" class="app-dialog" :aria-labelledby="titleId" @cancel="cancel">
    <header class="dialog-heading"><h2 :id="titleId">{{ title }}</h2><button type="button" class="secondary" :disabled="busy" aria-label="关闭对话框" @click="emit('close')">关闭</button></header>
    <slot />
  </dialog>
</template>
