<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { adminApi, type Permission, type Role } from '../admin-api'
import { can, refreshSession } from '../access'
import { request } from '../auth'
import { usePagedList } from '../composables/usePagedList'
import { useAction } from '../composables/useAction'
import AppDialog from '../components/AppDialog.vue'
import ListPager from '../components/ListPager.vue'
const { rows, page, loading, error: listError, reload } = usePagedList('role:read', adminApi.roles)
const { busy, error, notice, run } = useAction()
const catalog = ref<Permission[]>([]),
  catalogError = ref(''),
  catalogLoading = ref(false)
const editing = ref(false),
  target = ref<Role | null>(null),
  deleting = ref<Role | null>(null)
const filter = ref('')
const form = reactive({ key: '', name: '', description: '', permissions: [] as string[] })
const groups = computed(() => {
  const result: Record<string, Permission[]> = {}
  for (const permission of catalog.value) {
    if (!`${permission.key} ${permission.name}`.toLowerCase().includes(filter.value.toLowerCase()))
      continue
    const group = permission.key.split(':')[0]!
    ;(result[group] ??= []).push(permission)
  }
  return result
})
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
function open(role: Role | null = null) {
  target.value = role
  error.value = ''
  filter.value = ''
  Object.assign(form, {
    key: role?.key ?? '',
    name: role?.name ?? '',
    description: role?.description ?? '',
    permissions: [...(role?.permissions ?? [])],
  })
  editing.value = true
}
const manageable = (role: Role) => role.key !== 'admin' && role.permissions.every(can)
async function save() {
  await run(async () => {
    const body = { name: form.name, description: form.description, permissions: form.permissions }
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
  <section aria-label="角色与权限">
    <div class="heading">
      <div>
        <h1>角色与权限</h1>
        <p>通过角色授予功能权限，文件内容仍受独立对象授权保护。</p>
      </div>
      <el-button
        v-if="can('role:create')"
        :disabled="busy || catalogLoading || !!catalogError"
        @click="open()"
        native-type="button"
        type="primary"
      >
        新建角色
      </el-button>
    </div>
    <el-alert v-if="listError || catalogError" role="alert" type="error" show-icon :closable="false"
      >{{ listError || catalogError }}
      <el-button v-if="catalogError" @click="loadCatalog" native-type="button" type="primary"
        >重试权限目录</el-button
      ></el-alert
    >
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-button :disabled="loading" @click="reload" native-type="button" type="default"
      >刷新角色</el-button
    >
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
        <el-table-column label="角色" min-width="180"
          ><template #default="{ row: role }"
            ><strong>{{ role.name }}</strong>
            <p>{{ role.key }}</p>
            <small v-if="role.key === 'admin'">内置角色，不可编辑或删除</small></template
          ></el-table-column
        >
        <el-table-column label="说明" min-width="180"
          ><template #default="{ row: role }">{{
            role.description || '—'
          }}</template></el-table-column
        >
        <el-table-column label="权限" min-width="180"
          ><template #default="{ row: role }"
            ><details>
              <summary>{{ role.permissions.length }} 项权限</summary>
              <ul>
                <li v-for="permission in role.permissions" :key="permission">
                  <code>{{ permission }}</code>
                </li>
              </ul>
            </details></template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="220"
          ><template #default="{ row: role }"
            ><div class="row-actions">
              <el-button
                v-if="can('role:update')"
                :disabled="!manageable(role) || catalogLoading || !!catalogError"
                @click="open(role)"
                native-type="button"
                type="primary"
                link
              >
                编辑角色</el-button
              ><el-button
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
              >
                删除角色
              </el-button>
            </div></template
          ></el-table-column
        >
        <template #empty><el-empty :image-size="64" description="暂无角色。" /></template
      ></el-table>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading || busy"
      @change="page = $event"
    />
    <details class="permission-catalog">
      <summary>查看平台权限目录</summary>
      <ul>
        <li v-for="p in catalog" :key="p.key">
          <code>{{ p.key }}</code> · {{ p.name }}
        </li>
      </ul>
    </details>
    <AppDialog
      v-if="editing"
      :title="target ? '编辑角色' : '新建角色'"
      :busy="busy"
      @close="editing = false"
      ><form class="dialog-form" @submit.prevent="save">
        <label
          >角色标识<el-input
            v-model="form.key"
            :disabled="!!target"
            required
            maxlength="64"
            pattern="[a-z][a-z0-9_.-]*" /></label
        ><label>角色名称<el-input v-model="form.name" required maxlength="128" /></label
        ><label>说明<el-input v-model="form.description" maxlength="1000" type="textarea" /></label>
        <label>筛选权限<el-input v-model="filter" placeholder="权限标识或名称" /></label>
        <p>仅能授予自己当前拥有的权限。未勾选表示不授予。</p>
        <fieldset v-for="(items, group) in groups" :key="group">
          <legend>{{ group }}</legend>
          <el-checkbox-group v-model="form.permissions"
            ><el-checkbox
              v-for="permission in items"
              :key="permission.key"
              class="check-option"
              :value="permission.key"
              :disabled="!can(permission.key)"
              ><span
                >{{ permission.name }} <small>{{ permission.key }}</small></span
              ></el-checkbox
            ></el-checkbox-group
          >
        </fieldset>
        <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
          error
        }}</el-alert>
        <el-button
          :disabled="busy || catalogLoading || !!catalogError"
          native-type="submit"
          type="primary"
          >{{ busy ? '正在保存…' : '保存角色' }}</el-button
        >
      </form></AppDialog
    >
    <AppDialog v-if="deleting" title="删除角色" :busy="busy" @close="deleting = null"
      ><p>删除“{{ deleting.name }}”？仍有用户分配或文件授权引用时，服务端会拒绝删除。</p>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <el-button :disabled="busy" @click="remove" native-type="button" type="danger"
        >确认删除</el-button
      ></AppDialog
    >
  </section>
</template>
