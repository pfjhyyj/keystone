<script setup lang="ts">
import { onScopeDispose, reactive, ref } from 'vue'
import { adminApi, type ManagedUser } from '../admin-api'
import { can, identity, refreshSession } from '../access'
import { request } from '../auth'
import { usePagedList } from '../composables/usePagedList'
import { useAction } from '../composables/useAction'
import AppDialog from '../components/AppDialog.vue'
import ListPager from '../components/ListPager.vue'
import UserRolesDialog from '../components/UserRolesDialog.vue'
const query = ref(''), applied = ref('')
const { rows, page, loading, error: listError, reload, search } = usePagedList('user:read', offset => adminApi.users(offset, applied.value))
const { busy, error, notice, run } = useAction()
const mode = ref<'create' | 'edit' | 'password' | 'active' | 'revoke' | null>(null)
const target = ref<ManagedUser | null>(null), roleTarget = ref<ManagedUser | null>(null)
const form = reactive({ username: '', display_name: '', password: '', confirmation: '' })
function close() { mode.value = null; target.value = null; form.password = ''; form.confirmation = ''; error.value = '' }
onScopeDispose(close)
function open(value: NonNullable<typeof mode.value>, user: ManagedUser | null = null) {
  close(); mode.value = value; target.value = user
  form.username = user?.username ?? ''; form.display_name = user?.display_name ?? ''
}
const titles = { create: '新建用户', edit: '修改展示名', password: '重置密码', active: '变更账户状态', revoke: '撤销全部凭据' }
function submit() {
  return run(async () => {
    const current = mode.value, user = target.value
    if (!current) return
    try {
      if (current === 'create' || current === 'password') {
        if (form.password !== form.confirmation) throw new Error('两次密码不一致')
      }
      if (current === 'create') await request('/users', 'POST', { username: form.username, display_name: form.display_name, password: form.password })
      else if (user) {
        if (current === 'edit') await request(`/users/${user.id}`, 'PATCH', { display_name: form.display_name })
        if (current === 'password') await request(`/users/${user.id}/password`, 'POST', { password: form.password })
        if (current === 'active') await request(`/users/${user.id}/active`, 'PUT', { is_active: !user.is_active })
        if (current === 'revoke') await request(`/users/${user.id}/credentials/revoke`, 'POST')
      }
      close(); notice.value = '用户操作已保存。'
      if (user?.id === identity.value?.user.id) await refreshSession()
      await reload()
    } finally { form.password = ''; form.confirmation = '' }
  })
}
</script>
<template>
  <section aria-label="用户管理">
    <div class="heading"><div><div class="eyebrow">ACCESS / USERS</div><h1>用户管理</h1><p>管理账户、角色与凭据。用户停用后保留历史记录。</p></div><button v-if="can('user:create')" :disabled="busy" @click="open('create')">新建用户</button></div>
    <form class="module-form" @submit.prevent="applied = query.trim(); search()"><label>搜索用户<input v-model="query" maxlength="128" placeholder="登录名或展示名"></label><button :disabled="loading">查询</button><button type="button" class="secondary" :disabled="loading" @click="reload">刷新</button></form>
    <p v-if="listError" role="alert">{{ listError }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="loading" role="status">正在加载用户…</p>
    <div v-else class="table-scroll"><table><thead><tr><th>用户</th><th>状态</th><th>角色</th><th>操作</th></tr></thead><tbody>
      <tr v-for="user in rows" :key="user.id"><td><strong>{{ user.display_name }}</strong><p>{{ user.username }} <small v-if="user.id === identity?.user.id">（本人）</small></p><details><summary>标识与时间</summary><p>{{ user.id }}</p><p>创建 {{ new Date(user.created_at).toLocaleString() }}</p><p>修改 {{ new Date(user.updated_at).toLocaleString() }}</p></details></td><td>{{ user.is_active ? '启用' : '停用' }}<p v-if="user.password_change_required">需首次改密</p></td><td>{{ user.role_ids.length }} 个角色</td><td><div class="row-actions">
        <button v-if="can('user:update')" @click="open('edit', user)">修改</button>
        <button v-if="can('user:update')" @click="open('active', user)">{{ user.is_active ? '停用' : '启用' }}</button>
        <button v-if="can('role:assign')" :disabled="!can('role:read')" @click="roleTarget = user">分配角色</button>
        <button v-if="can('user:reset_password')" @click="open('password', user)">重置密码</button>
        <button v-if="can('user:credentials')" @click="open('revoke', user)">撤销凭据</button>
      </div></td></tr><tr v-if="!rows.length"><td colspan="4">没有符合条件的用户。</td></tr>
    </tbody></table></div>
    <ListPager :page="page" :count="rows.length" :loading="loading || busy" @change="page = $event" />
    <AppDialog v-if="mode" :title="titles[mode]" :busy="busy" @close="close"><form class="dialog-form" @submit.prevent="submit">
      <p v-if="target">{{ target.display_name }} · {{ target.username }}</p>
      <label v-if="mode === 'create'">登录名<input v-model="form.username" autocomplete="off" required minlength="3" maxlength="64" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]*"></label>
      <label v-if="mode === 'create' || mode === 'edit'">展示名<input v-model="form.display_name" required maxlength="128"></label>
      <template v-if="mode === 'create' || mode === 'password'"><p>用户首次登录必须修改密码。重置密码会撤销登录会话，个人令牌保持独立。</p><label>初始密码<input v-model="form.password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label><label>确认密码<input v-model="form.confirmation" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label></template>
      <p v-if="mode === 'active'">{{ target?.is_active ? '停用会撤销该用户全部会话和个人令牌。' : '启用不会恢复此前撤销的凭据。' }}</p>
      <p v-if="mode === 'revoke'">撤销该用户全部会话和个人令牌，用户需要重新登录；账户本身保持不变。</p>
      <p v-if="error" role="alert">{{ error }}</p><button :disabled="busy">{{ busy ? '正在保存…' : '确认保存' }}</button>
    </form></AppDialog>
    <UserRolesDialog v-if="roleTarget" :user="roleTarget" @close="roleTarget = null" @changed="reload" />
  </section>
</template>
