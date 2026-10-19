<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { can } from './access'
import { downloadFile, request, uploadFile } from './auth'
import { fileApi, fileStatus, uuidPattern, type StoredFile } from './file-api'
import { usePagedList } from './composables/usePagedList'
import { useAction } from './composables/useAction'
import AppDialog from './components/AppDialog.vue'
import ListPager from './components/ListPager.vue'
import FileGrantsDialog from './components/FileGrantsDialog.vue'
import FileReferencesDialog from './components/FileReferencesDialog.vue'
const { rows, page, loading, error: listError, reload, search } = usePagedList('file:inspect', fileApi.list)
const { busy, error, notice, run } = useAction()
const chosen = ref<File | null>(null), maxBytes = ref(0), requestKey = ref('')
const storageKeys = ref<string[]>([]), storageKey = ref('')
const selected = ref<StoredFile | null>(null), mode = ref<'edit' | 'delete'>('edit'), name = ref(''), status = ref('active')
const sharing = ref<{ id: string; name?: string; status?: string } | null>(null), references = ref<{ id: string; name?: string } | null>(null)
const knownId = ref(''), input = ref<HTMLInputElement | null>(null)
function choose(event: Event) { chosen.value = (event.target as HTMLInputElement).files?.[0] ?? null; requestKey.value = crypto.randomUUID() }
function changeStorage() { requestKey.value = crypto.randomUUID() }
async function upload() {
  await run(async () => {
    if (!chosen.value || !can('file:create')) return
    if (chosen.value.size > maxBytes.value) throw new Error('文件超过上传上限')
    const result = await uploadFile(chosen.value, requestKey.value, storageKey.value)
    notice.value = result.status === 'active' ? '文件已上传' : '上传请求已处理，请查看文件状态'
    chosen.value = null; if (input.value) input.value.value = ''; search()
  })
}
function edit(row: StoredFile, operation: 'edit' | 'delete') { selected.value = row; mode.value = operation; name.value = row.name; status.value = row.status; error.value = '' }
async function save() {
  await run(async () => {
    if (!selected.value || !can('file:manage')) return
    if (mode.value === 'edit') {
      await request(`/files/${selected.value.id}`, 'PATCH', { name: name.value, status: status.value })
      notice.value = '文件资料已保存'
    } else {
      await request(`/files/${selected.value.id}`, 'DELETE'); notice.value = '删除请求已受理，等待 Worker 清理，可刷新查看状态'
    }
    selected.value = null; await reload()
  })
}
function openKnown(kind: 'grants' | 'references') {
  error.value = ''
  const id = knownId.value.trim()
  if (!new RegExp(`^${uuidPattern}$`).test(id)) { error.value = '请填写有效的文件 UUID'; return }
  if (kind === 'grants') sharing.value = { id }; else references.value = { id }
}
onMounted(() => { if (can('file:create')) void run(async () => {
  const options = await request<{ max_file_bytes: number; storage_keys: string[]; default_storage: string }>('/files/options')
  maxBytes.value = options.max_file_bytes; storageKeys.value = options.storage_keys; storageKey.value = options.default_storage
}) })
</script>

<template>
  <section class="module-panel" aria-label="文件库">
    <div class="heading"><div><p class="eyebrow">PLATFORM / FILES</p><h1>文件库</h1><p>统一保存文件，供后续业务引用。内容读取与管理授权分别控制。</p></div><button v-if="can('file:inspect')" :disabled="busy || loading" @click="reload">刷新文件</button></div>
    <p v-if="(error && !selected) || listError" role="alert">{{ (selected ? '' : error) || listError }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <form v-if="can('file:create')" class="module-form" @submit.prevent="upload">
      <label>选择文件<input ref="input" type="file" :disabled="busy" @change="choose"></label><span>上限 {{ Math.round(maxBytes / 1024 / 1024 * 10) / 10 }} MB</span>
      <label>存储源<select v-model="storageKey" aria-label="存储源" :disabled="busy" @change="changeStorage"><option v-for="key in storageKeys" :key="key" :value="key">{{ key }}</option></select></label>
      <button :disabled="busy || !chosen || !maxBytes || !storageKey">上传文件</button>
    </form>
    <p v-if="!can('file:inspect')">当前账户没有查看文件库的权限。</p>
    <template v-else>
      <p v-if="loading" role="status">正在加载文件…</p>
      <div class="table-scroll"><table><thead><tr><th>文件名</th><th>大小</th><th>状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="row in rows" :key="row.id"><td>{{ row.name }}<details><summary>标识与时间</summary><small>ID：{{ row.id }}<br>所属人：{{ row.owner_id }}<br>存储源：{{ row.storage_key }}<br>创建：{{ new Date(row.created_at).toLocaleString() }}<br>修改：{{ new Date(row.updated_at).toLocaleString() }}</small></details></td><td>{{ row.size_bytes === null ? '—' : `${row.size_bytes} B` }}</td><td>{{ fileStatus[row.status] }}</td>
          <td><div class="row-actions"><button v-if="can('file:content_read') && row.status === 'active'" :disabled="busy" @click="run(() => downloadFile(row.id, row.name))">下载</button>
            <template v-if="can('file:manage')"><button v-if="['active', 'disabled'].includes(row.status)" :disabled="busy" @click="edit(row, 'edit')">编辑文件</button>
              <button :disabled="busy" @click="sharing = row">文件授权</button><button :disabled="busy" @click="references = row">查看引用</button>
              <button v-if="!['deleted','deleting'].includes(row.status)" :disabled="busy" @click="edit(row, 'delete')">删除文件</button></template></div></td></tr>
        <tr v-if="!loading && !listError && !rows.length"><td colspan="4">暂无可见文件</td></tr>
      </tbody></table></div>
      <ListPager :page="page" :count="rows.length" :loading="busy || loading" @change="page = $event" />
    </template>
    <details v-if="can('file:manage')" class="known-file"><summary>按文件 ID 管理授权或查看引用</summary>
      <form class="module-form" @submit.prevent="openKnown('grants')"><label>文件 ID<input v-model="knownId" required :pattern="uuidPattern" placeholder="已知文件 UUID"></label><button :disabled="busy">打开授权</button><button type="button" :disabled="busy" @click="openKnown('references')">打开引用</button></form>
      <p>用于已知但未出现在可见列表中的文件，仍需对应文件的管理授权。</p>
    </details>
    <AppDialog v-if="selected" :title="mode === 'edit' ? '编辑文件' : '删除文件'" :busy="busy" @close="selected = null; error = ''">
      <p v-if="error" role="alert">{{ error }}</p>
      <form class="dialog-form" @submit.prevent="save">
        <template v-if="mode === 'edit'"><label>文件名称<input v-model="name" required maxlength="255"></label><label>文件状态<select v-model="status"><option value="active">可用</option><option value="disabled">停用</option></select></label></template>
        <p v-else>确认删除“{{ selected.name }}”？存在引用时无法删除；受理后由 Worker 清理内容，清理完成后无法恢复。</p>
        <button :disabled="busy">{{ mode === 'edit' ? '保存文件' : '确认删除' }}</button>
      </form>
    </AppDialog>
    <FileGrantsDialog v-if="sharing" :key="sharing.id" :file="sharing" @close="sharing = null" @changed="reload" />
    <FileReferencesDialog v-if="references" :key="references.id" :file="references" @close="references = null" />
  </section>
</template>
