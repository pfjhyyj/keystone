<script setup lang="ts">
// Ether's permission-aware subject picker, with Keystone's paginated directories.
// The parent keys this component by kind to discard selection and pending requests.
import { computed, ref } from 'vue'
import { can } from '../access'
import { adminApi, type ManagedUser, type Role } from '../admin-api'
import { uuidPattern } from '../file-api'
import { usePagedList } from '../composables/usePagedList'
import ListPager from './ListPager.vue'
const props = defineProps<{ kind: 'user' | 'role'; disabled: boolean }>()
const model = defineModel<string>({ required: true })
const query = ref(''),
  applied = ref('')
const allowed = computed(() => can(`${props.kind}:read`))
const { rows, page, loading, error, search } = usePagedList<ManagedUser | Role>(
  `${props.kind}:read`,
  (offset) =>
    props.kind === 'user' ? adminApi.users(offset, applied.value) : adminApi.roles(offset),
)
function find() {
  model.value = ''
  applied.value = query.value
  search()
}
function changePage(value: number) {
  model.value = ''
  page.value = value
}
</script>
<template>
  <div class="subject-picker">
    <template v-if="allowed">
      <div v-if="kind === 'user'" class="row-actions">
        <label
          >搜索授权用户<el-input
            v-model="query"
            maxlength="100"
            :disabled="disabled"
            @keydown.enter.prevent="find"
        /></label>
        <el-button :disabled="disabled || loading" @click="find" native-type="button" type="primary"
          >查找用户</el-button
        >
      </div>
      <div class="form-field">
        <span class="form-label">授权对象</span
        ><AppSelect v-model="model" aria-label="授权对象" required :disabled="disabled || loading"
          ><el-option value="" :label="'请选择' + (kind === 'user' ? '用户' : '角色')" /><el-option
            v-for="row in rows"
            :key="row.id"
            :value="row.id"
            :label="
              'username' in row
                ? `${row.display_name}（${row.username}）${row.is_active ? '' : ' · 已停用'}`
                : `${row.name}（${row.key}）`
            "
        /></AppSelect>
      </div>
      <ListPager
        v-if="page > 0 || rows.length >= 50"
        :page="page"
        :count="rows.length"
        :loading="loading || disabled"
        @change="changePage"
      />
      <p v-if="loading" role="status">正在加载授权对象…</p>
      <p v-else-if="!rows.length && !error">没有可选择的对象。</p>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
    </template>
    <template v-else>
      <p>无权读取{{ kind === 'user' ? '用户' : '角色' }}目录，可填写已知对象 ID。</p>
      <label
        >授权对象 ID<el-input
          v-model="model"
          required
          :pattern="uuidPattern"
          placeholder="对象 UUID"
          :disabled="disabled"
      /></label>
    </template>
  </div>
</template>
