<script setup lang="ts">
import { fileApi } from '../file-api'
import { usePagedList } from '../composables/usePagedList'
import AppDialog from './AppDialog.vue'
import ListPager from './ListPager.vue'
const props = defineProps<{ file: { id: string; name?: string } }>()
defineEmits<{ close: [] }>()
const { rows, page, loading, error, reload } = usePagedList('file:manage', offset => fileApi.references(props.file.id, offset))
</script>
<template>
  <AppDialog title="文件引用" @close="$emit('close')">
    <p>{{ file.name || file.id }}</p>
    <p>引用期间不能删除文件。请到所属流程或业务中解除引用；引用本身不授予文件内容权限。</p>
    <button class="secondary" :disabled="loading" @click="reload">刷新引用</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在加载引用…</p>
    <ul v-else class="selection-list reference-list"><li v-for="row in rows" :key="`${row.owner}:${row.slot}`"><span>所属对象：{{ row.owner }}<small>位置：{{ row.slot }}</small></span></li></ul>
    <p v-if="!loading && !error && !rows.length">没有有效引用。</p>
    <ListPager :page="page" :count="rows.length" :loading="loading" @change="page = $event" />
  </AppDialog>
</template>
