<script setup lang="ts">
import { ref } from 'vue'
import { adminApi, type ManagedUser } from '../../../api/admin'
import { can, identity, refreshSession } from '../../../stores/session'
import { request } from '../../../api/client'
import { usePagedList } from '../../../composables/usePagedList'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import ListPager from '../../../components/ListPager.vue'
const props = defineProps<{ user: ManagedUser }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const assigned = ref([...props.user.role_ids])
const { rows, page, loading, error: listError } = usePagedList('role:read', adminApi.roles)
const { busy, error, run } = useAction()
async function toggle(id: string) {
  await run(async () => {
    const value = !assigned.value.includes(id)
    await request(`/users/${props.user.id}/roles/${id}`, 'PUT', { assigned: value })
    assigned.value = value ? [...assigned.value, id] : assigned.value.filter((item) => item !== id)
    emit('changed')
    if (identity.value?.user.id === props.user.id) await refreshSession()
  })
}
</script>
<template>
  <AppDialog :title="`分配角色 · ${user.display_name}`" :busy="busy" @close="emit('close')">
    <p>逐项保存。角色授予范围、目标用户及最后管理员保护由服务端校验。</p>
    <el-alert v-if="!can('role:read')" role="alert" type="error" show-icon :closable="false"
      >需要角色目录读取权限才能选择角色。</el-alert
    >
    <el-alert v-if="listError || error" role="alert" type="error" show-icon :closable="false">{{
      listError || error
    }}</el-alert>
    <p v-if="loading" role="status">正在加载角色…</p>
    <ul class="selection-list">
      <li v-for="role in rows" :key="role.id">
        <span
          >{{ role.name }} <small>{{ role.key }}</small></span
        ><el-button
          :disabled="
            busy ||
            !can('role:assign') ||
            (role.key === 'admin' && !identity?.is_administrator) ||
            role.permissions.some((p) => !can(p))
          "
          @click="toggle(role.id)"
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
