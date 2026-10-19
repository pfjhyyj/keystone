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
const catalog = ref<Permission[]>([]), catalogError = ref(''), catalogLoading = ref(false)
const editing = ref(false), target = ref<Role | null>(null), deleting = ref<Role | null>(null)
const filter = ref('')
const form = reactive({ key: '', name: '', description: '', permissions: [] as string[] })
const groups = computed(() => {
  const result: Record<string, Permission[]> = {}
  for (const permission of catalog.value) {
    if (!`${permission.key} ${permission.name}`.toLowerCase().includes(filter.value.toLowerCase())) continue
    const group = permission.key.split(':')[0]!
    ;(result[group] ??= []).push(permission)
  }
  return result
})
async function loadCatalog() {
  catalogLoading.value = true; catalogError.value = ''
  try { catalog.value = await adminApi.permissions() }
  catch (reason) { catalogError.value = reason instanceof Error ? reason.message : '权限目录不可用' }
  finally { catalogLoading.value = false }
}
function open(role: Role | null = null) {
  target.value = role; error.value = ''; filter.value = ''
  Object.assign(form, { key: role?.key ?? '', name: role?.name ?? '', description: role?.description ?? '', permissions: [...(role?.permissions ?? [])] })
  editing.value = true
}
const manageable = (role: Role) => role.key !== 'admin' && role.permissions.every(can)
async function save() {
  await run(async () => {
    const body = { name: form.name, description: form.description, permissions: form.permissions }
    if (target.value) await request(`/roles/${target.value.id}`, 'PUT', body)
    else await request('/roles', 'POST', { ...body, key: form.key })
    editing.value = false; notice.value = '角色已保存。'
    await refreshSession(); await reload()
  })
}
async function remove() {
  await run(async () => {
    if (!deleting.value) return
    await request(`/roles/${deleting.value.id}`, 'DELETE')
    deleting.value = null; notice.value = '角色已删除。'; await reload()
  })
}
onMounted(loadCatalog)
</script>
<template>
  <section aria-label="角色与权限"><div class="heading"><div><div class="eyebrow">ACCESS / ROLES</div><h1>角色与权限</h1><p>通过角色授予功能权限，文件内容仍受独立对象授权保护。</p></div><button v-if="can('role:create')" :disabled="busy || catalogLoading || !!catalogError" @click="open()">新建角色</button></div>
    <p v-if="listError || catalogError" role="alert">{{ listError || catalogError }} <button v-if="catalogError" @click="loadCatalog">重试权限目录</button></p><p v-if="notice" role="status">{{ notice }}</p><button class="secondary" :disabled="loading" @click="reload">刷新角色</button>
    <p v-if="loading" role="status">正在加载角色…</p><div v-else class="table-scroll"><table><thead><tr><th>角色</th><th>说明</th><th>权限</th><th>操作</th></tr></thead><tbody><tr v-for="role in rows" :key="role.id"><td><strong>{{ role.name }}</strong><p>{{ role.key }}</p><small v-if="role.key === 'admin'">内置角色，不可编辑或删除</small></td><td>{{ role.description || '—' }}</td><td><details><summary>{{ role.permissions.length }} 项权限</summary><ul><li v-for="permission in role.permissions" :key="permission"><code>{{ permission }}</code></li></ul></details></td><td><div class="row-actions"><button v-if="can('role:update')" :disabled="!manageable(role) || catalogLoading || !!catalogError" @click="open(role)">编辑角色</button><button v-if="can('role:delete')" :disabled="!manageable(role)" @click="deleting = role; error = ''">删除角色</button></div></td></tr><tr v-if="!rows.length"><td colspan="4">暂无角色。</td></tr></tbody></table></div>
    <ListPager :page="page" :count="rows.length" :loading="loading || busy" @change="page = $event" />
    <details class="permission-catalog"><summary>查看平台权限目录</summary><ul><li v-for="p in catalog" :key="p.key"><code>{{ p.key }}</code> · {{ p.name }}</li></ul></details>
    <AppDialog v-if="editing" :title="target ? '编辑角色' : '新建角色'" :busy="busy" @close="editing = false"><form class="dialog-form" @submit.prevent="save">
      <label>角色标识<input v-model="form.key" :disabled="!!target" required maxlength="64" pattern="[a-z][a-z0-9_.-]*"></label><label>角色名称<input v-model="form.name" required maxlength="128"></label><label>说明<textarea v-model="form.description" maxlength="1000" /></label>
      <label>筛选权限<input v-model="filter" placeholder="权限标识或名称"></label><p>仅能授予自己当前拥有的权限。未勾选表示不授予。</p>
      <fieldset v-for="(items, group) in groups" :key="group"><legend>{{ group }}</legend><label v-for="permission in items" :key="permission.key" class="check-option"><input v-model="form.permissions" type="checkbox" :value="permission.key" :disabled="!can(permission.key)"><span>{{ permission.name }} <small>{{ permission.key }}</small></span></label></fieldset>
      <p v-if="error" role="alert">{{ error }}</p><button :disabled="busy || catalogLoading || !!catalogError">{{ busy ? '正在保存…' : '保存角色' }}</button>
    </form></AppDialog>
    <AppDialog v-if="deleting" title="删除角色" :busy="busy" @close="deleting = null"><p>删除“{{ deleting.name }}”？仍有用户分配或文件授权引用时，服务端会拒绝删除。</p><p v-if="error" role="alert">{{ error }}</p><button :disabled="busy" @click="remove">确认删除</button></AppDialog>
  </section>
</template>
