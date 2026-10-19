<script setup lang="ts">
import { ref } from 'vue'
import { can } from './access'
import { request } from './auth'
import { dictionaryApi, type Definition } from './dictionary-api'
import { usePagedList } from './composables/usePagedList'
import { useAction } from './composables/useAction'
import AppDialog from './components/AppDialog.vue'
import ListPager from './components/ListPager.vue'
import DictionaryItems from './components/DictionaryItems.vue'
const { rows, page, loading, error: listError, reload, search } = usePagedList('dictionary:read', dictionaryApi.list)
const { busy, error, notice, run } = useAction()
const selected = ref<Definition | null>(null), editing = ref<Definition | null>(null), open = ref(false)
const key = ref(''), name = ref(''), description = ref(''), active = ref(true)
function edit(row: Definition | null) { editing.value = row; key.value = row?.key ?? ''; name.value = row?.name ?? ''; description.value = row?.description ?? ''; active.value = row?.is_active ?? true; error.value = ''; open.value = true }
async function save() {
  await run(async () => {
    if (!can('dictionary:manage')) return
    const body = { name: name.value, description: description.value || null }
    const row = editing.value
      ? await request<Definition>(`/dictionaries/${editing.value.id}`, 'PATCH', { ...body, is_active: active.value })
      : await request<Definition>('/dictionaries', 'POST', { ...body, key: key.value })
    if (selected.value?.id === row.id || !editing.value) selected.value = row
    open.value = false; notice.value = '字典已保存'; search()
  })
}
function refresh() { selected.value = null; void reload() }
function changePage(value: number) { selected.value = null; page.value = value }
</script>
<template>
  <section class="module-panel" aria-label="字典管理">
    <div class="heading"><div><p class="eyebrow">PLATFORM / DICTIONARY</p><h1>业务字典</h1><p>维护业务选项。只停用、不删除，历史记录仍可显示。</p></div><div class="row-actions"><button v-if="can('dictionary:manage')" :disabled="busy" @click="edit(null)">新建字典</button><button class="secondary" :disabled="busy || loading" @click="refresh">刷新字典</button></div></div>
    <p v-if="listError" role="alert">{{ listError }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="loading" role="status">正在加载字典…</p>
    <div class="table-scroll"><table><thead><tr><th>字典</th><th>标识</th><th>状态</th><th>操作</th></tr></thead><tbody>
      <tr v-for="row in rows" :key="row.id" :aria-selected="selected?.id === row.id"><td>{{ row.name }}<small class="cell-description">{{ row.description }}</small><details><summary>标识与时间</summary><small>ID：{{ row.id }}<br>创建：{{ new Date(row.created_at).toLocaleString() }}<br>修改：{{ new Date(row.updated_at).toLocaleString() }}</small></details></td><td>{{ row.key }}</td><td>{{ row.is_active ? '启用' : '停用' }}</td><td><div class="row-actions"><button :disabled="busy" @click="selected = row">查看选项</button><button v-if="can('dictionary:manage')" :disabled="busy" @click="edit(row)">编辑字典</button></div></td></tr>
      <tr v-if="!loading && !listError && !rows.length"><td colspan="4">暂无字典</td></tr>
    </tbody></table></div>
    <ListPager :page="page" :count="rows.length" :loading="busy || loading" @change="changePage" />
    <DictionaryItems v-if="selected" :key="selected.id" :dictionary="selected" @close="selected = null" />
    <AppDialog v-if="open" :title="editing ? '编辑字典' : '新建字典'" :busy="busy" @close="open = false; error = ''">
      <p v-if="error" role="alert">{{ error }}</p><form class="dialog-form" @submit.prevent="save">
        <label>字典标识<input v-model="key" :disabled="!!editing" required pattern="[a-z][a-z0-9_]{0,63}" placeholder="customer_source"></label><label>字典名称<input v-model="name" required maxlength="128"></label><label>字典说明<textarea v-model="description" maxlength="1000"></textarea></label>
        <label v-if="editing" class="check-option"><input v-model="active" type="checkbox">启用字典</label><p>标识创建后不可修改。停用字典会阻止其所有选项用于新选择，不改动选项自身的启用状态。</p><button :disabled="busy">保存字典</button>
      </form>
    </AppDialog>
  </section>
</template>
