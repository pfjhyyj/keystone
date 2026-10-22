<script setup lang="ts">
import { onMounted, onScopeDispose, ref } from 'vue'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import { downloadFile, uploadFile, type UploadProgress } from '../../api/files'
import { fileApi, fileStatus, uuidPattern, type StoredFile } from '../../api/files'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import ListPager from '../../components/ListPager.vue'
import FileGrantsDialog from './components/FileGrantsDialog.vue'
import FileReferencesDialog from './components/FileReferencesDialog.vue'
const {
  rows,
  page,
  loading,
  error: listError,
  reload,
  search,
} = usePagedList('file:inspect', fileApi.list)
const { busy, error, notice, run } = useAction()
const chosen = ref<File | null>(null),
  maxBytes = ref(0),
  requestKey = ref('')
const storageKeys = ref<string[]>([]),
  storageKey = ref(''),
  optionsLoading = ref(false),
  optionsError = ref('')
let alive = true
let uploadController: AbortController | null = null
const uploadProgress = ref<UploadProgress | null>(null)
function cancelUpload() {
  uploadController?.abort()
}
onScopeDispose(() => {
  alive = false
  uploadController?.abort()
})
const selected = ref<StoredFile | null>(null),
  mode = ref<'edit' | 'delete'>('edit'),
  name = ref(''),
  status = ref('active')
const sharing = ref<{ id: string; name?: string; status?: string } | null>(null),
  references = ref<{ id: string; name?: string } | null>(null)
const knownId = ref(''),
  input = ref<HTMLInputElement | null>(null)
function choose(event: Event) {
  chosen.value = (event.target as HTMLInputElement).files?.[0] ?? null
  requestKey.value = crypto.randomUUID()
}
function changeStorage() {
  requestKey.value = crypto.randomUUID()
}
async function upload() {
  if (optionsLoading.value || optionsError.value || !maxBytes.value || !storageKey.value) return
  await run(async () => {
    if (!chosen.value || !can('file:create')) return
    if (chosen.value.size > maxBytes.value) throw new Error('文件超过上传上限')
    uploadController = new AbortController()
    try {
      const result = await uploadFile(chosen.value, requestKey.value, storageKey.value, {
        signal: uploadController.signal,
        onProgress: (value) => {
          if (alive) uploadProgress.value = value
        },
      })
      if (!alive) return
      notice.value = result.status === 'active' ? '文件已上传' : '上传请求已处理，请查看文件状态'
      chosen.value = null
      if (input.value) input.value.value = ''
      search()
    } finally {
      uploadController = null
      if (alive) uploadProgress.value = null
    }
  })
}
function edit(row: StoredFile, operation: 'edit' | 'delete') {
  selected.value = row
  mode.value = operation
  name.value = row.name
  status.value = row.status
  error.value = ''
}
async function save() {
  await run(async () => {
    if (!selected.value || !can('file:manage')) return
    if (mode.value === 'edit') {
      await request(`/files/${selected.value.id}`, 'PATCH', {
        name: name.value,
        status: status.value,
      })
      notice.value = '文件资料已保存'
    } else {
      await request(`/files/${selected.value.id}`, 'DELETE')
      notice.value = '删除请求已受理，等待 Worker 清理，可刷新查看状态'
    }
    selected.value = null
    await reload()
  })
}
function openKnown(kind: 'grants' | 'references') {
  error.value = ''
  const id = knownId.value.trim()
  if (!new RegExp(`^${uuidPattern}$`).test(id)) {
    error.value = '请填写有效的文件 UUID'
    return
  }
  if (kind === 'grants') sharing.value = { id }
  else references.value = { id }
}
async function loadOptions() {
  if (!can('file:create') || optionsLoading.value || busy.value) return
  optionsLoading.value = true
  optionsError.value = ''
  try {
    const options = await request<{
      max_file_bytes: number
      storage_keys: string[]
      default_storage: string
    }>('/files/options')
    if (!alive) return
    maxBytes.value = options.max_file_bytes
    storageKeys.value = options.storage_keys
    if (!options.storage_keys.includes(storageKey.value)) {
      storageKey.value = options.default_storage
      changeStorage()
    }
  } catch (reason) {
    if (alive) optionsError.value = reason instanceof Error ? reason.message : '上传配置不可用'
  } finally {
    if (alive) optionsLoading.value = false
  }
}
onMounted(() => void loadOptions())
</script>

<template>
  <section aria-label="文件库">
    <div class="heading">
      <div>
        <h1>文件库</h1>
        <p>统一保存文件，供后续业务引用。内容读取与管理授权分别控制。</p>
      </div>
      <el-button
        v-if="can('file:inspect')"
        :disabled="busy || loading"
        @click="reload"
        native-type="button"
        type="default"
      >
        刷新文件
      </el-button>
    </div>
    <el-alert
      v-if="(error && !selected) || listError"
      role="alert"
      type="error"
      show-icon
      :closable="false"
      >{{ (selected ? '' : error) || listError }}</el-alert
    >
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <template v-if="can('file:create')">
      <el-alert v-if="optionsError" role="alert" type="error" show-icon :closable="false"
        >上传配置加载失败：{{ optionsError }}</el-alert
      >
      <p v-if="optionsLoading" role="status">正在加载上传配置…</p>
      <form class="module-form" @submit.prevent="upload">
        <label>选择文件<input ref="input" type="file" :disabled="busy" @change="choose" /></label
        ><span v-if="maxBytes && !optionsError"
          >上限 {{ Math.round((maxBytes / 1024 / 1024) * 10) / 10 }} MB</span
        >
        <div class="form-field">
          <span class="form-label">存储源</span
          ><AppSelect
            v-model="storageKey"
            aria-label="存储源"
            :disabled="busy || optionsLoading || !!optionsError"
            @change="changeStorage"
            ><el-option v-for="key in storageKeys" :key="key" :value="key" :label="key"
          /></AppSelect>
        </div>
        <el-button
          :disabled="
            busy || optionsLoading || !!optionsError || !chosen || !maxBytes || !storageKey
          "
          native-type="submit"
          type="primary"
          >上传文件</el-button
        >
        <template v-if="uploadProgress">
          <span role="status"
            >{{
              uploadProgress.phase === 'hashing'
                ? '正在校验文件'
                : uploadProgress.phase === 'confirming'
                  ? '正在等待服务器确认'
                  : '正在上传文件'
            }}
            {{
              uploadProgress.total
                ? Math.floor((uploadProgress.loaded / uploadProgress.total) * 100)
                : 0
            }}%</span
          >
          <el-button native-type="button" @click="cancelUpload">取消上传</el-button>
        </template>
        <el-button
          :disabled="busy || optionsLoading"
          @click="loadOptions"
          native-type="button"
          type="default"
          >{{ optionsError ? '重试上传配置' : '刷新上传配置' }}</el-button
        >
      </form>
    </template>
    <p v-if="!can('file:inspect')">当前账户没有查看文件库的权限。</p>
    <template v-else>
      <p v-if="loading" role="status">正在加载文件…</p>
      <div class="table-scroll">
        <el-table
          :fit="false"
          native-scrollbar
          table-layout="auto"
          :data="rows"
          row-key="id"
          stripe
          class="data-table"
        >
          <el-table-column label="文件名" min-width="180"
            ><template #default="{ row: row }">{{ row.name }}</template></el-table-column
          >
          <el-table-column label="大小" min-width="100"
            ><template #default="{ row: row }">{{
              row.size_bytes === null ? '—' : `${row.size_bytes} B`
            }}</template></el-table-column
          >
          <el-table-column label="状态" min-width="120"
            ><template #default="{ row: row }"
              ><StatusTag :status="row.status">{{ fileStatus[row.status] }}</StatusTag></template
            ></el-table-column
          >
          <el-table-column label="操作" min-width="220"
            ><template #default="{ row: row }"
              ><div class="row-actions">
                <el-button
                  v-if="can('file:content_read') && row.status === 'active'"
                  :disabled="busy"
                  @click="run(() => downloadFile(row.id, row.name))"
                  native-type="button"
                  type="primary"
                  link
                >
                  下载
                </el-button>
                <template v-if="can('file:manage')"
                  ><el-button
                    v-if="['active', 'disabled'].includes(row.status)"
                    :disabled="busy"
                    @click="edit(row, 'edit')"
                    native-type="button"
                    type="primary"
                    link
                  >
                    编辑文件
                  </el-button>
                  <el-button
                    :disabled="busy"
                    @click="sharing = row"
                    native-type="button"
                    type="primary"
                    link
                    >文件授权</el-button
                  ><el-button
                    :disabled="busy"
                    @click="references = row"
                    native-type="button"
                    type="primary"
                    link
                    >查看引用</el-button
                  >
                  <el-button
                    v-if="!['deleted', 'deleting'].includes(row.status)"
                    :disabled="busy"
                    @click="edit(row, 'delete')"
                    native-type="button"
                    type="danger"
                    link
                  >
                    删除文件
                  </el-button></template
                >
              </div></template
            ></el-table-column
          >
          <template #empty><el-empty :image-size="64" description="暂无可见文件" /></template
        ></el-table>
      </div>
      <ListPager
        :page="page"
        :count="rows.length"
        :loading="busy || loading"
        @change="page = $event"
      />
    </template>
    <details v-if="can('file:manage')" class="known-file">
      <summary>按文件 ID 管理授权或查看引用</summary>
      <form class="module-form" @submit.prevent="openKnown('grants')">
        <label
          >文件 ID<el-input
            v-model="knownId"
            required
            :pattern="uuidPattern"
            placeholder="已知文件 UUID" /></label
        ><el-button :disabled="busy" native-type="submit" type="primary">打开授权</el-button
        ><el-button
          :disabled="busy"
          @click="openKnown('references')"
          native-type="button"
          type="primary"
          >打开引用</el-button
        >
      </form>
      <p>用于已知但未出现在可见列表中的文件，仍需对应文件的管理授权。</p>
    </details>
    <AppDialog
      v-if="selected"
      :title="mode === 'edit' ? '编辑文件' : '删除文件'"
      :busy="busy"
      @close="
        () => {
          selected = null
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="save">
        <template v-if="mode === 'edit'"
          ><label>文件名称<el-input v-model="name" required maxlength="255" /></label>
          <div class="form-field">
            <span class="form-label">文件状态</span
            ><AppSelect v-model="status" aria-label="文件状态"
              ><el-option value="active" :label="'可用'" /><el-option
                value="disabled"
                :label="'停用'"
            /></AppSelect></div
        ></template>
        <p v-else>
          确认删除“{{ selected.name }}”？存在引用时无法删除；受理后由 Worker
          清理内容，清理完成后无法恢复。
        </p>
        <el-button :disabled="busy" native-type="submit" type="primary">{{
          mode === 'edit' ? '保存文件' : '确认删除'
        }}</el-button>
      </form>
    </AppDialog>
    <FileGrantsDialog
      v-if="sharing"
      :key="sharing.id"
      :file="sharing"
      @close="sharing = null"
      @changed="reload"
    />
    <FileReferencesDialog
      v-if="references"
      :key="references.id"
      :file="references"
      @close="references = null"
    />
  </section>
</template>
