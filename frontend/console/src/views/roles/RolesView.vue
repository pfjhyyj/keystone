<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { adminApi, type Permission, type Role } from '../../api/admin'
import { can, refreshSession } from '../../stores/session'
import { request } from '../../api/client'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import ListPager from '../../components/ListPager.vue'
import PermissionPicker from './components/PermissionPicker.vue'

const { rows, page, loading, error: listError, reload } = usePagedList('role:read', adminApi.roles)
const { busy, error, notice, run } = useAction()
const catalog = ref<Permission[]>([])
const catalogError = ref('')
const catalogLoading = ref(false)
const editing = ref(false)
const target = ref<Role | null>(null)
const deleting = ref<Role | null>(null)
const readOnly = ref(false)
const form = reactive({ key: '', name: '', description: '', permissions: [] as string[] })
const initialForm = ref('')
const fingerprint = computed(() =>
  JSON.stringify({ ...form, permissions: [...form.permissions].sort() }),
)
const formDirty = computed(() => !readOnly.value && fingerprint.value !== initialForm.value)
const manageable = (role: Role) => role.key !== 'admin' && role.permissions.every(can)
const canSave = computed(
  () =>
    !readOnly.value &&
    !busy.value &&
    !catalogLoading.value &&
    !catalogError.value &&
    can(target.value ? 'role:update' : 'role:create') &&
    (!target.value || manageable(target.value)) &&
    form.permissions.every(can),
)

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    catalog.value = await adminApi.permissions()
  } catch (reason) {
    catalogError.value = reason instanceof Error ? reason.message : '权限目录不可用'
  } finally {
    catalogLoading.value = false
  }
}
function open(role: Role | null = null, view = false) {
  target.value = role
  readOnly.value = view || !!(role && !manageable(role))
  error.value = ''
  Object.assign(form, {
    key: role?.key ?? '',
    name: role?.name ?? '',
    description: role?.description ?? '',
    permissions: [...(role?.permissions ?? [])],
  })
  initialForm.value = fingerprint.value
  editing.value = true
}
async function save() {
  if (!canSave.value) return
  await run(async () => {
    const body = {
      name: form.name,
      description: form.description,
      permissions: [...form.permissions],
    }
    if (target.value) await request(`/roles/${target.value.id}`, 'PUT', body)
    else await request('/roles', 'POST', { ...body, key: form.key })
    editing.value = false
    notice.value = '角色已保存。'
    await refreshSession()
    await reload()
  })
}
async function remove() {
  await run(async () => {
    if (!deleting.value) return
    await request(`/roles/${deleting.value.id}`, 'DELETE')
    deleting.value = null
    notice.value = '角色已删除。'
    await reload()
  })
}
onMounted(loadCatalog)
</script>
<template>
  <section aria-label="角色管理">
    <div class="heading">
      <h1>角色管理</h1>
      <el-button
        v-if="can('role:create')"
        :disabled="busy || catalogLoading || !!catalogError"
        @click="open()"
        native-type="button"
        type="primary"
        >新建角色</el-button
      >
    </div>
    <el-alert
      v-if="listError || catalogError"
      role="alert"
      type="error"
      show-icon
      :closable="false"
    >
      {{ listError || catalogError }}
      <el-button v-if="catalogError" @click="loadCatalog" native-type="button" type="primary"
        >重试权限目录</el-button
      >
    </el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-button :disabled="loading" @click="reload" native-type="button">刷新角色</el-button>
    <p v-if="loading" role="status">正在加载角色…</p>
    <div v-else class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        table-layout="auto"
        :data="rows"
        row-key="id"
        stripe
        class="data-table"
      >
        <el-table-column label="角色" min-width="180">
          <template #default="{ row: role }"
            ><strong>{{ role.name }}</strong>
            <p>{{ role.key }}</p>
            <small v-if="role.key === 'admin'">内置角色，不可编辑或删除</small></template
          >
        </el-table-column>
        <el-table-column label="说明" min-width="180">
          <template #default="{ row: role }">{{ role.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="权限" min-width="150">
          <template #default="{ row: role }">
            <el-button
              link
              type="primary"
              native-type="button"
              :disabled="catalogLoading || !!catalogError"
              :aria-label="`查看${role.name}的权限`"
              @click="open(role, true)"
              >{{ role.permissions.length }} 项权限</el-button
            >
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="220">
          <template #default="{ row: role }"
            ><div class="row-actions">
              <el-button
                v-if="can('role:update')"
                :disabled="!manageable(role) || catalogLoading || !!catalogError"
                @click="open(role)"
                native-type="button"
                type="primary"
                link
                >配置权限</el-button
              >
              <el-button
                v-if="can('role:delete')"
                :disabled="!manageable(role)"
                @click="
                  () => {
                    deleting = role
                    error = ''
                  }
                "
                native-type="button"
                type="danger"
                link
                >删除角色</el-button
              >
            </div></template
          >
        </el-table-column>
        <template #empty><el-empty :image-size="64" description="暂无角色。" /></template>
      </el-table>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading || busy"
      @change="page = $event"
    />
    <AppDialog
      protect-changes
      :dirty="formDirty"
      v-if="editing"
      :title="readOnly ? '查看角色权限' : target ? '配置角色权限' : '新建角色'"
      :busy="busy"
      width="960px"
      class="role-dialog"
      @close="editing = false"
    >
      <form class="dialog-form role-form" @submit.prevent="save">
        <label
          >角色标识<el-input
            v-model="form.key"
            :disabled="!!target || busy || readOnly"
            required
            maxlength="64"
            pattern="[a-z][a-z0-9_.-]*"
        /></label>
        <label
          >角色名称<el-input
            v-model="form.name"
            :readonly="readOnly"
            :disabled="busy"
            required
            maxlength="128"
        /></label>
        <label
          >说明<el-input
            v-model="form.description"
            :readonly="readOnly"
            :disabled="busy"
            maxlength="1000"
            type="textarea"
        /></label>
        <div class="role-permissions-field">
          <PermissionPicker
            v-model="form.permissions"
            :catalog="catalog"
            :disabled="busy || catalogLoading"
            :readonly="readOnly"
          />
        </div>
        <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
          error
        }}</el-alert>
        <div class="role-dialog-footer">
          <el-button :disabled="busy" native-type="button" @click="editing = false">{{
            readOnly ? '关闭' : '取消'
          }}</el-button>
          <el-button v-if="!readOnly" :disabled="!canSave" native-type="submit" type="primary">{{
            busy ? '正在保存…' : '保存角色'
          }}</el-button>
        </div>
      </form>
    </AppDialog>
    <AppDialog v-if="deleting" title="删除角色" :busy="busy" @close="deleting = null">
      <p>删除“{{ deleting.name }}”？仍有用户分配或文件授权引用时，服务端会拒绝删除。</p>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <el-button :disabled="busy" @click="remove" native-type="button" type="danger"
        >确认删除</el-button
      >
    </AppDialog>
  </section>
</template>

<style scoped>
.role-permissions-field,
.role-dialog-footer {
  grid-column: 1 / -1;
  min-width: 0;
}
.role-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  border-top: 1px solid #edf0f4;
  padding-top: 16px;
  position: sticky;
  bottom: -4px;
  background: #fff;
  z-index: 2;
}
.role-dialog-footer .el-button + .el-button {
  margin-left: 0;
}
</style>
