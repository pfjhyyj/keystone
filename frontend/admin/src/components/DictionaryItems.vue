<script setup lang="ts">
import { ref } from 'vue'
import { can } from '../access'
import { request } from '../auth'
import { dictionaryApi, type Definition, type DictionaryItem } from '../dictionary-api'
import { usePagedList } from '../composables/usePagedList'
import { useAction } from '../composables/useAction'
import AppDialog from './AppDialog.vue'
import ListPager from './ListPager.vue'
const props = defineProps<{ dictionary: Definition }>()
defineEmits<{ close: [] }>()
const { rows, page, loading, error: listError, reload, search } = usePagedList('dictionary:read', offset => dictionaryApi.items(props.dictionary.id, offset))
const { busy, error, notice, run } = useAction()
const open = ref(false), editing = ref<DictionaryItem | null>(null)
const value = ref(''), label = ref(''), sort = ref(0), active = ref(true)
function edit(row: DictionaryItem | null) { editing.value = row; value.value = row?.value ?? ''; label.value = row?.label ?? ''; sort.value = row?.sort_order ?? 0; active.value = row?.is_active ?? true; error.value = ''; open.value = true }
async function save() {
  await run(async () => {
    if (!can('dictionary:manage')) return
    if (!Number.isInteger(sort.value) || sort.value < -2147483648 || sort.value > 2147483647) throw new Error('排序必须是 32 位整数')
    const path = `/dictionaries/${props.dictionary.id}/items`, body = { label: label.value, sort_order: sort.value }
    if (editing.value) await request(`${path}/${editing.value.id}`, 'PATCH', { ...body, is_active: active.value })
    else await request(path, 'POST', { ...body, value: value.value })
    open.value = false; notice.value = '选项已保存'; search()
  })
}
</script>
<template>
  <section class="dictionary-items" aria-label="字典选项">
    <div class="heading"><div><h2>{{ dictionary.name }} · 选项</h2><p>{{ dictionary.key }} · {{ dictionary.description || '暂无说明' }}</p></div><div class="row-actions"><button v-if="can('dictionary:manage')" :disabled="busy" @click="edit(null)">添加选项</button><button class="secondary" :disabled="busy || loading" @click="reload">刷新选项</button><button class="secondary" :disabled="busy" @click="$emit('close')">收起选项</button></div></div>
    <p v-if="!dictionary.is_active" role="status">此字典已停用，所有选项暂不可用于新选择；选项自身的启用状态保留。</p>
    <p v-if="listError" role="alert">{{ listError }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="loading" role="status">正在加载选项…</p>
    <div class="table-scroll"><table><thead><tr><th>显示名称</th><th>稳定值</th><th>排序</th><th>状态</th><th>操作</th></tr></thead><tbody>
      <tr v-for="item in rows" :key="item.id"><td>{{ item.label }}<details><summary>标识与时间</summary><small>ID：{{ item.id }}<br>创建：{{ new Date(item.created_at).toLocaleString() }}<br>修改：{{ new Date(item.updated_at).toLocaleString() }}</small></details></td><td>{{ item.value }}</td><td>{{ item.sort_order }}</td><td>{{ !item.is_active ? '已停用' : dictionary.is_active ? '可选' : '已启用，字典停用中' }}</td><td><button v-if="can('dictionary:manage')" :disabled="busy" @click="edit(item)">编辑选项</button></td></tr>
      <tr v-if="!loading && !listError && !rows.length"><td colspan="5">暂无选项</td></tr>
    </tbody></table></div>
    <ListPager :page="page" :count="rows.length" :loading="loading || busy" @change="page = $event" />
    <AppDialog v-if="open" :title="editing ? '编辑选项' : '添加选项'" :busy="busy" @close="open = false; error = ''">
      <p>{{ dictionary.name }}（{{ dictionary.key }}）</p><p v-if="error" role="alert">{{ error }}</p>
      <form class="dialog-form" @submit.prevent="save"><label>稳定值<input v-model="value" :disabled="!!editing" required pattern="[a-z][a-z0-9_]{0,63}"></label><label>显示名称<input v-model="label" required maxlength="128"></label><label>排序<input v-model.number="sort" type="number" min="-2147483648" max="2147483647" step="1" required></label>
        <label v-if="editing" class="check-option"><input v-model="active" type="checkbox">启用选项</label><p>稳定值创建后不可修改；选项只停用，不删除。排序相同时按 ID 排列。</p><button :disabled="busy">保存选项</button></form>
    </AppDialog>
  </section>
</template>
