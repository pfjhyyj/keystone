<script setup lang="ts">
import { computed, ref } from 'vue'
import { can } from '../access'
import { request } from '../auth'
import { fileApi, grantActions, uuidPattern, type FileGrant } from '../file-api'
import { usePagedList } from '../composables/usePagedList'
import { useAction } from '../composables/useAction'
import AppDialog from './AppDialog.vue'
import ListPager from './ListPager.vue'
import SubjectPicker from './SubjectPicker.vue'
const props = defineProps<{ file: { id: string; name?: string; status?: string } }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const { rows, page, loading, error: listError, reload, search } = usePagedList('file:manage', offset => fileApi.grants(props.file.id, offset))
const { busy, error, notice, run } = useAction()
const subject = ref<FileGrant['subject_type']>('user'), target = ref(''), action = ref<FileGrant['action']>('inspect')
const revoking = ref<FileGrant | null>(null)
const writable = computed(() => can('file:manage') && !['deleting', 'deleted'].includes(props.file.status ?? ''))
const canAdd = computed(() => writable.value && (!props.file.status || ['active', 'disabled'].includes(props.file.status)))
function resetSubject() { target.value = ''; action.value = 'inspect' }
function subjectLabel(row: FileGrant) { return row.subject_type === 'authenticated' ? '所有登录用户' : `${row.subject_type === 'user' ? '用户' : '角色'} ${row.user_id || row.role_id}` }
async function add() {
  if (!canAdd.value || loading.value) return
  await run(async () => {
    if (subject.value !== 'authenticated' && !new RegExp(`^${uuidPattern}$`).test(target.value)) throw new Error('请填写有效的对象 UUID')
    if (subject.value === 'authenticated' && action.value === 'manage') throw new Error('不能向所有登录用户授予管理权')
    await request(`/files/${props.file.id}/grants`, 'POST', { subject_type: subject.value, user_id: subject.value === 'user' ? target.value : null, role_id: subject.value === 'role' ? target.value : null, action: action.value })
    target.value = ''; notice.value = '授权已保存，重复授权不会新增关系'; search(); emit('changed')
  })
}
async function revoke() {
  if (!revoking.value || !writable.value) return
  await run(async () => {
    await request(`/files/${props.file.id}/grants/${revoking.value!.id}`, 'DELETE')
    revoking.value = null; notice.value = '授权已撤销'; emit('changed'); await reload()
  })
}
</script>
<template>
  <AppDialog title="文件授权" :busy="busy" @close="$emit('close')">
    <p>{{ file.name || file.id }}</p>
    <p>授权还需配合对应功能权限。查看信息、读取内容、管理授权分别生效；所属人不自动绕过授权。内置管理员可查看和管理，但读取内容仍需显式授权。</p>
    <p v-if="error || listError" role="alert">{{ error || listError }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <form v-if="canAdd" class="dialog-form" @submit.prevent="add">
      <label>授权主体<select v-model="subject" aria-label="授权主体" :disabled="busy || loading" @change="resetSubject"><option value="user">用户</option><option value="role">角色</option><option value="authenticated">所有登录用户</option></select></label>
      <SubjectPicker v-if="subject !== 'authenticated'" :key="subject" v-model="target" :kind="subject" :disabled="busy || loading" />
      <label>授权动作<select v-model="action" aria-label="授权动作" :disabled="busy || loading"><option value="inspect">查看信息</option><option value="content_read">读取内容</option><option v-if="subject !== 'authenticated'" value="manage">管理与授权</option></select></label>
      <button :disabled="busy || loading || !!listError || (subject !== 'authenticated' && !target)">添加授权</button>
    </form>
    <p v-else>当前文件状态不允许新增授权。</p>
    <button type="button" class="secondary" :disabled="busy || loading" @click="reload">刷新授权</button>
    <p v-if="loading" role="status">正在加载授权…</p>
    <ul class="selection-list grant-list"><li v-for="row in rows" :key="row.id"><span>{{ subjectLabel(row) }}<small>{{ grantActions[row.action] }}</small><small>创建于 {{ new Date(row.created_at).toLocaleString() }} · {{ row.created_by || '系统' }}</small></span><button v-if="writable" type="button" :disabled="busy || loading" @click="revoking = row; error = ''">撤销授权</button></li></ul>
    <p v-if="!loading && !listError && !rows.length">暂无授权。</p>
    <ListPager :page="page" :count="rows.length" :loading="busy || loading" @change="revoking = null; page = $event" />
    <form v-if="revoking" class="confirmation" @submit.prevent="revoke">
      <p>确认撤销 {{ subjectLabel(revoking) }} 的“{{ grantActions[revoking.action] }}”？撤销自己的管理授权后可能无法继续操作。</p>
      <div class="row-actions"><button :disabled="busy">确认撤销</button><button type="button" class="secondary" :disabled="busy" @click="revoking = null">取消撤销</button></div>
    </form>
  </AppDialog>
</template>
