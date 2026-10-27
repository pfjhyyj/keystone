<script setup lang="ts">
import { onScopeDispose, ref } from 'vue'
import { adminApi, type ManagedUser, type Role } from '../../../api/admin'
import { can, identity, refreshSession } from '../../../stores/session'
import { request } from '../../../api/client'
import { usePagedList } from '../../../composables/usePagedList'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import ListPager from '../../../components/ListPager.vue'
const props = defineProps<{ user: ManagedUser }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const assigned = ref([...props.user.role_ids])
const { rows, page, loading, error: listError, reload } = usePagedList('role:read', adminApi.roles)
const { busy, error, run } = useAction()
let alive = true
onScopeDispose(() => {
  alive = false
})
function blockedReason(role: Role) {
  if (!can('role:read') || !can('role:assign')) return '当前账号没有角色分配权限'
  if (role.key === 'admin' && !identity.value?.is_administrator)
    return '只有管理员可分配或解除管理员角色'
  if (role.permissions.some((permission) => !can(permission))) return '超出当前账号可授予的权限范围'
  return ''
}
async function toggle(role: Role) {
  if (busy.value || loading.value || blockedReason(role)) return
  await run(async () => {
    const id = role.id
    const user = props.user
    const value = !assigned.value.includes(id)
    await request(`/users/${user.id}/roles/${id}`, 'PUT', { assigned: value })
    if (identity.value?.user.id === user.id) await refreshSession()
    if (!alive) return
    assigned.value = value ? [...assigned.value, id] : assigned.value.filter((item) => item !== id)
    emit('changed')
  })
}
</script>
<template>
  <AppDialog drawer :title="`分配角色 · ${user.display_name}`" :busy="busy" @close="emit('close')">
    <p>分配或解除后立即保存，用户的可用功能会随角色权限更新。</p>
    <p>管理员角色不会自动获得所有文件的内容读取权限。</p>
    <div class="assignment-toolbar">
      <el-button
        :disabled="busy || loading || !can('role:read')"
        @click="reload"
        native-type="button"
      >
        {{ listError ? '重试角色目录' : '刷新角色目录' }}</el-button
      >
    </div>
    <el-alert v-if="!can('role:read')" role="alert" type="error" show-icon :closable="false"
      >需要角色目录读取权限才能选择角色。</el-alert
    >
    <el-alert v-if="listError || error" role="alert" type="error" show-icon :closable="false">{{
      listError || error
    }}</el-alert>
    <p v-if="loading" role="status">正在加载角色…</p>
    <el-empty
      v-if="!loading && !listError && can('role:read') && !rows.length"
      :image-size="64"
      description="暂无角色，可先在角色管理中新建。"
    />
    <ul v-if="!loading && rows.length" class="selection-list">
      <li v-for="role in rows" :key="role.id">
        <span class="assignment-summary"
          ><strong>{{ role.name }}</strong
          ><small>{{ role.key }} · {{ role.permissions.length }} 项权限</small>
          <el-tag
            disable-transitions
            :type="assigned.includes(role.id) ? 'success' : 'info'"
            size="small"
            effect="plain"
            >{{ assigned.includes(role.id) ? '已分配' : '未分配' }}</el-tag
          >
          <small v-if="blockedReason(role)">{{ blockedReason(role) }}</small></span
        ><el-button
          :disabled="busy || loading || !!blockedReason(role)"
          @click="toggle(role)"
          native-type="button"
          type="primary"
          >{{ assigned.includes(role.id) ? '解除角色' : '分配角色' }}</el-button
        >
      </li>
    </ul>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading || busy"
      @change="page = $event"
    />
  </AppDialog>
</template>

<style scoped>
.assignment-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.assignment-summary {
  min-width: 0;
}
.assignment-summary .el-tag {
  margin-top: 6px;
}
</style>
