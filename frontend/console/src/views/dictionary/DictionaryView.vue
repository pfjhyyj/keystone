<script setup lang="ts">
import { ref } from 'vue'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import { dictionaryApi, type Definition } from '../../api/dictionary'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import ListPager from '../../components/ListPager.vue'
import DictionaryItems from './components/DictionaryItems.vue'
const {
  rows,
  page,
  loading,
  error: listError,
  reload,
  search,
} = usePagedList('dictionary:read', dictionaryApi.list)
const { busy, error, notice, run } = useAction()
const selected = ref<Definition | null>(null),
  editing = ref<Definition | null>(null),
  open = ref(false)
const key = ref(''),
  name = ref(''),
  description = ref(''),
  active = ref(true)
function edit(row: Definition | null) {
  editing.value = row
  key.value = row?.key ?? ''
  name.value = row?.name ?? ''
  description.value = row?.description ?? ''
  active.value = row?.is_active ?? true
  error.value = ''
  open.value = true
}
async function save() {
  await run(async () => {
    if (!can('dictionary:manage')) return
    const body = { name: name.value, description: description.value || null }
    const row = editing.value
      ? await request<Definition>(`/dictionaries/${editing.value.id}`, 'PATCH', {
          ...body,
          is_active: active.value,
        })
      : await request<Definition>('/dictionaries', 'POST', { ...body, key: key.value })
    if (selected.value?.id === row.id || !editing.value) selected.value = row
    open.value = false
    notice.value = '字典已保存'
    search()
  })
}
function refresh() {
  selected.value = null
  void reload()
}
function changePage(value: number) {
  selected.value = null
  page.value = value
}
</script>
<template>
  <section aria-label="字典管理">
    <div class="heading">
      <div>
        <h1>业务字典</h1>
        <p>维护业务选项。只停用、不删除，历史记录仍可显示。</p>
      </div>
      <div class="row-actions">
        <el-button
          v-if="can('dictionary:manage')"
          :disabled="busy"
          @click="edit(null)"
          native-type="button"
          type="primary"
        >
          新建字典</el-button
        ><el-button :disabled="busy || loading" @click="refresh" native-type="button" type="default"
          >刷新字典</el-button
        >
      </div>
    </div>
    <el-alert v-if="listError" role="alert" type="error" show-icon :closable="false">{{
      listError
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <p v-if="loading" role="status">正在加载字典…</p>
    <div :class="{ 'dictionary-layout': selected }">
      <div>
        <div v-if="!listError" class="table-scroll">
          <el-table
            :fit="false"
            :data="rows"
            row-key="id"
            stripe
            class="data-table"
            highlight-current-row
            :current-row-key="selected?.id"
          >
            <el-table-column label="字典" min-width="160"
              ><template #default="{ row: row }"
                >{{ row.name }}<small>{{ row.description }}</small>
              </template></el-table-column
            >
            <el-table-column label="标识" min-width="130"
              ><template #default="{ row: row }">{{ row.key }}</template></el-table-column
            >
            <el-table-column label="状态" min-width="120"
              ><template #default="{ row: row }"
                ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
                  row.is_active ? '启用' : '停用'
                }}</StatusTag></template
              ></el-table-column
            >
            <el-table-column label="操作" min-width="160"
              ><template #default="{ row: row }"
                ><div class="row-actions">
                  <el-button
                    :disabled="busy"
                    @click="selected = row"
                    native-type="button"
                    type="primary"
                    link
                    >查看选项</el-button
                  ><el-button
                    v-if="can('dictionary:manage')"
                    :disabled="busy"
                    @click="edit(row)"
                    native-type="button"
                    type="primary"
                    link
                  >
                    编辑字典
                  </el-button>
                </div></template
              ></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无字典" /></template
          ></el-table>
        </div>
        <ListPager
          :page="page"
          :count="rows.length"
          :loading="busy || loading"
          @change="changePage"
        />
      </div>
      <DictionaryItems
        v-if="selected"
        :key="selected.id"
        :dictionary="selected"
        @close="selected = null"
      />
    </div>
    <AppDialog
      v-if="open"
      protect-changes
      :title="editing ? '编辑字典' : '新建字典'"
      :busy="busy"
      @close="
        () => {
          open = false
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="save">
        <label
          >字典标识<el-input
            v-model="key"
            :disabled="!!editing"
            required
            pattern="[a-z][a-z0-9_]{0,63}"
            placeholder="customer_source" /></label
        ><label>字典名称<el-input v-model="name" required maxlength="128" /></label
        ><label>字典说明<el-input v-model="description" maxlength="1000" type="textarea" /></label>
        <el-checkbox v-if="editing" class="check-option" v-model="active">启用字典</el-checkbox>
        <p>标识创建后不可修改。停用字典会阻止其所有选项用于新选择，不改动选项自身的启用状态。</p>
        <el-button :disabled="busy" native-type="submit" type="primary">保存字典</el-button>
      </form>
    </AppDialog>
  </section>
</template>
