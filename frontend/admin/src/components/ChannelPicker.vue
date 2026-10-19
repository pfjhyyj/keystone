<script setup lang="ts">
import { modelApi } from '../model-api'
import { usePagedList } from '../composables/usePagedList'
import ListPager from './ListPager.vue'
defineProps<{ disabled: boolean }>()
const model = defineModel<string>({ required: true })
const { rows, page, loading, error } = usePagedList('model:manage', modelApi.channels)
</script>
<template>
  <div class="subject-picker"><label>模型渠道<select v-model="model" aria-label="模型渠道" :disabled="disabled || loading" required><option value="">请选择渠道</option><option v-if="model && !rows.some(row => row.id === model)" :value="model">{{ model }}（当前渠道）</option><option v-for="row in rows" :key="row.id" :value="row.id">{{ row.name }}{{ row.is_active ? '' : ' · 停用' }}</option></select></label>
    <ListPager v-if="page || rows.length >= 50" :page="page" :count="rows.length" :loading="loading || disabled" @change="page = $event" /><p v-if="error" role="alert">{{ error }}</p></div>
</template>
