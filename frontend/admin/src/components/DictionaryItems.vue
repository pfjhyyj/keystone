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
const {
  rows,
  page,
  loading,
  error: listError,
  reload,
  search,
} = usePagedList('dictionary:read', (offset) => dictionaryApi.items(props.dictionary.id, offset))
const { busy, error, notice, run } = useAction()
const open = ref(false),
  editing = ref<DictionaryItem | null>(null)
const value = ref(''),
  label = ref(''),
  sort = ref(0),
  active = ref(true)
function edit(row: DictionaryItem | null) {
  editing.value = row
  value.value = row?.value ?? ''
  label.value = row?.label ?? ''
  sort.value = row?.sort_order ?? 0
  active.value = row?.is_active ?? true
  error.value = ''
  open.value = true
}
async function save() {
  await run(async () => {
    if (!can('dictionary:manage')) return
    if (!Number.isInteger(sort.value) || sort.value < -2147483648 || sort.value > 2147483647)
      throw new Error('排序必须是 32 位整数')
    const path = `/dictionaries/${props.dictionary.id}/items`,
      body = { label: label.value, sort_order: sort.value }
    if (editing.value)
      await request(`${path}/${editing.value.id}`, 'PATCH', { ...body, is_active: active.value })
    else await request(path, 'POST', { ...body, value: value.value })
    open.value = false
    notice.value = '选项已保存'
    search()
  })
}
</script>
<template>
  <section class="dictionary-items" aria-label="字典选项">
    <div class="heading">
      <div>
        <h2>{{ dictionary.name }} · 选项</h2>
        <p>{{ dictionary.key }} · {{ dictionary.description || '暂无说明' }}</p>
      </div>
      <div class="row-actions">
        <el-button
          v-if="can('dictionary:manage')"
          :disabled="busy"
          @click="edit(null)"
          native-type="button"
          type="primary"
        >
          添加选项</el-button
        ><el-button :disabled="busy || loading" @click="reload" native-type="button" type="default"
          >刷新选项</el-button
        ><el-button :disabled="busy" @click="$emit('close')" native-type="button" type="default"
          >收起选项</el-button
        >
      </div>
    </div>
    <p v-if="!dictionary.is_active" role="status">
      此字典已停用，所有选项暂不可用于新选择；选项自身的启用状态保留。
    </p>
    <el-alert v-if="listError" role="alert" type="error" show-icon :closable="false">{{
      listError
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <p v-if="loading" role="status">正在加载选项…</p>
    <div class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        table-layout="auto"
        :data="rows"
        row-key="id"
        stripe
        class="data-table"
      >
        <el-table-column label="显示名称" min-width="180"
          ><template #default="{ row: item }"
            >{{ item.label }}
            <details>
              <summary>标识与时间</summary>
              <small
                >ID：{{ item.id }}<br />创建：{{ new Date(item.created_at).toLocaleString()
                }}<br />修改：{{ new Date(item.updated_at).toLocaleString() }}</small
              >
            </details></template
          ></el-table-column
        >
        <el-table-column label="稳定值" min-width="180"
          ><template #default="{ row: item }">{{ item.value }}</template></el-table-column
        >
        <el-table-column label="排序" min-width="180"
          ><template #default="{ row: item }">{{ item.sort_order }}</template></el-table-column
        >
        <el-table-column label="状态" min-width="120"
          ><template #default="{ row: item }"
            ><StatusTag :status="item.is_active ? 'active' : 'disabled'">{{
              !item.is_active ? '已停用' : dictionary.is_active ? '可选' : '已启用，字典停用中'
            }}</StatusTag></template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="220"
          ><template #default="{ row: item }"
            ><el-button
              v-if="can('dictionary:manage')"
              :disabled="busy"
              @click="edit(item)"
              native-type="button"
              type="primary"
              link
            >
              编辑选项
            </el-button></template
          ></el-table-column
        >
        <template #empty><el-empty :image-size="64" description="暂无选项" /></template
      ></el-table>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading || busy"
      @change="page = $event"
    />
    <AppDialog
      v-if="open"
      :title="editing ? '编辑选项' : '添加选项'"
      :busy="busy"
      @close="
        () => {
          open = false
          error = ''
        }
      "
    >
      <p>{{ dictionary.name }}（{{ dictionary.key }}）</p>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="save">
        <label
          >稳定值<el-input
            v-model="value"
            :disabled="!!editing"
            required
            pattern="[a-z][a-z0-9_]{0,63}" /></label
        ><label>显示名称<el-input v-model="label" required maxlength="128" /></label
        ><label
          >排序<el-input
            v-model.number="sort"
            type="number"
            min="-2147483648"
            max="2147483647"
            step="1"
            required
        /></label>
        <el-checkbox v-if="editing" class="check-option" v-model="active">启用选项</el-checkbox>
        <p>稳定值创建后不可修改；选项只停用，不删除。排序相同时按 ID 排列。</p>
        <el-button :disabled="busy" native-type="submit" type="primary">保存选项</el-button>
      </form>
    </AppDialog>
  </section>
</template>
