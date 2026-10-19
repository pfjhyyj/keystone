<script setup lang="ts">
import { watch } from 'vue'
import { modelApi } from '../model-api'
import { usePagedList } from '../composables/usePagedList'
import ListPager from './ListPager.vue'
const props = defineProps<{ disabled?: boolean }>()
const model = defineModel<string>({ required: true })
const emit = defineEmits<{ mode: [value: string] }>()
let mode = ''
const { rows, page, loading, error, reload } = usePagedList('model:invoke', async offset => { const data = await modelApi.catalog(offset); mode = data.mode; return data.items })
watch(rows, values => { if (!loading.value && values.length) emit('mode', mode) })
watch(loading, value => { if (!value && !error.value) emit('mode', mode) })
</script>
<template>
  <div class="subject-picker"><label>模型<select v-model="model" aria-label="模型" :disabled="props.disabled || loading" required><option value="">请选择模型</option><option v-if="model && !rows.some(row => row.key === model)" :value="model">{{ model }}（已选，提交时复查）</option><option v-for="row in rows" :key="row.key" :value="row.key">{{ row.key }}</option></select></label>
    <div class="row-actions"><button type="button" class="secondary" :disabled="props.disabled || loading" @click="reload">刷新可用模型</button></div>
    <ListPager v-if="page || rows.length >= 50" :page="page" :count="rows.length" :loading="loading || !!props.disabled" @change="page = $event" />
    <p v-if="error" role="alert">{{ error }}</p><p v-else-if="!loading && !rows.length">本页没有可用模型。</p>
  </div>
</template>
