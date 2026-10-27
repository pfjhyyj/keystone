<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { UploadUserFile } from 'element-plus'
import { can } from '../../../stores/session'
import { invokeModel } from '../../../api/client'
import {
  modelApi,
  operationLabels,
  type CatalogModel,
  type ModelOperation,
} from '../../../api/models'
import { uploadFile, downloadFile } from '../../../api/files'
import ModelPicker from '../../../components/models/ModelPicker.vue'
import { useUnsavedChanges } from '../../../composables/useUnsavedChanges'
const emit = defineEmits<{ completed: [] }>()
const operation = ref<ModelOperation>('text.generate')
const operations: ModelOperation[] = [
  'text.generate',
  'image.generate',
  'image.edit',
  'image.understand',
]
const key = ref(''),
  prompt = ref(''),
  output = ref(''),
  invocation = ref(''),
  error = ref(''),
  calling = ref(false)
const selected = ref<CatalogModel | null>(null)
useUnsavedChanges(computed(() => !!prompt.value.trim() || calling.value))
const attachments = ref<UploadUserFile[]>([])
const images = ref<{ file_id: string }[]>([])
const uploads = new Map<File, { requestKey: string; id?: string }>()
const uploading = ref('')
const maxTokens = ref(1024),
  count = ref(1),
  resolution = ref('1K'),
  format = ref('png'),
  ratio = ref('1:1'),
  quality = ref('medium')
const generates = computed(() => ['image.generate', 'image.edit'].includes(operation.value))
const needsImages = computed(() => ['image.edit', 'image.understand'].includes(operation.value))
const limit = computed(() => selected.value?.max_reference_images ?? 4)
const resultFormat = ref('png')
const permitted = computed(
  () =>
    can('model:invoke') &&
    (!generates.value || can('file:create')) &&
    (!needsImages.value || (can('file:create') && can('file:content_read'))),
)
const ready = computed(
  () =>
    permitted.value &&
    !!key.value &&
    !!prompt.value.trim() &&
    (!needsImages.value ||
      (attachments.value.length > 0 && attachments.value.length <= limit.value)),
)
let controller: AbortController | null = null,
  alive = true
function clearResult() {
  output.value = ''
  invocation.value = ''
  images.value = []
  error.value = ''
}
function showText(content: string) {
  output.value = content
  if (!content.trim())
    error.value =
      '本次调用未返回可显示的文本。输出上限包含推理 Token，可能已用尽；请查看调用与费用后，再决定是否增大上限重新发送。'
}
function clearAttachments() {
  for (const file of attachments.value)
    if (file.url?.startsWith('blob:')) URL.revokeObjectURL(file.url)
  attachments.value = []
  uploads.clear()
}
watch(operation, () => {
  key.value = ''
  selected.value = null
  clearResult()
  clearAttachments()
})
watch(key, clearResult)
function stop() {
  controller?.abort()
}
function checkFiles() {
  const invalid = attachments.value.filter(
    (file) =>
      !file.raw ||
      !['image/png', 'image/jpeg', 'image/webp'].includes(file.raw.type) ||
      file.raw.size > 4 * 1024 * 1024,
  )
  for (const file of invalid) if (file.url?.startsWith('blob:')) URL.revokeObjectURL(file.url)
  attachments.value = attachments.value.filter((file) => !invalid.includes(file))
  error.value = invalid.length ? '请选择不超过 4 MiB 的 PNG、JPEG 或 WebP 图片。' : ''
}
function removeAttachment(file: UploadUserFile) {
  if (file.url?.startsWith('blob:')) URL.revokeObjectURL(file.url)
  if (file.raw) uploads.delete(file.raw)
}
async function inputs(signal: AbortSignal) {
  const files = []
  for (const item of attachments.value) {
    signal.throwIfAborted()
    const raw = item.raw!
    let upload = uploads.get(raw)
    if (!upload) {
      upload = { requestKey: crypto.randomUUID() }
      uploads.set(raw, upload)
    }
    if (!upload.id) {
      uploading.value = `正在上传：${item.name}`
      const result = await uploadFile(raw, upload.requestKey, undefined, { signal })
      signal.throwIfAborted()
      upload.id = result.id
    }
    files.push({ file_id: upload.id })
  }
  uploading.value = ''
  return files
}
async function invoke() {
  if (calling.value || !ready.value) return
  calling.value = true
  clearResult()
  const current = new AbortController()
  controller = current
  try {
    if (operation.value === 'text.generate') {
      await invokeModel(
        {
          model_key: key.value,
          messages: [{ role: 'user', content: prompt.value }],
          max_tokens: maxTokens.value,
          stream: false,
        },
        (event) => {
          if (!alive || !can('model:invoke')) return
          if (typeof event.invocation_id === 'string') invocation.value = event.invocation_id
          if (event.type === 'error') throw new Error(String(event.detail))
          if (event.type === 'done' && typeof event.content === 'string') showText(event.content)
        },
        current.signal,
      )
    } else {
      const maximum = selected.value?.max_input_bytes ?? 8 * 1024 * 1024
      if (
        needsImages.value &&
        attachments.value.reduce((total, file) => total + (file.raw?.size ?? 0), 0) > maximum
      )
        throw new Error(`参考图总大小不能超过 ${maximum / 1024 / 1024} MiB`)
      const references = needsImages.value ? await inputs(current.signal) : undefined
      const controls = selected.value?.image_parameters ?? []
      const body = generates.value
        ? {
            model_key: key.value,
            prompt: prompt.value,
            n: count.value,
            resolution: resolution.value,
            output_format: format.value,
            ...(controls.includes('aspect_ratio') ? { aspect_ratio: ratio.value } : {}),
            ...(controls.includes('quality') ? { quality: quality.value } : {}),
            ...(references ? { images: references } : {}),
          }
        : {
            model_key: key.value,
            messages: [{ role: 'user', content: prompt.value }],
            images: references,
            max_tokens: maxTokens.value,
          }
      const result = await modelApi.media(operation.value, body, current.signal)
      if (!alive || !can('model:invoke')) return
      resultFormat.value = format.value
      invocation.value = result.invocation_id
      if (!generates.value) showText(result.content ?? '')
      images.value = result.files ?? []
    }
  } catch (reason) {
    if (alive)
      error.value = current.signal.aborted
        ? '已停止接收；上游可能已经执行，请查看调用记录，不要盲目重试。'
        : `${reason instanceof Error ? reason.message : '调用失败'}。请求不会自动重试；生成结果不明确时请先查看调用记录。`
  } finally {
    if (alive) {
      calling.value = false
      uploading.value = ''
      controller = null
      emit('completed')
    }
  }
}
async function download(id: string, index: number) {
  try {
    await downloadFile(id, `model-result-${index + 1}.${resultFormat.value}`)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '下载失败'
  }
}
onScopeDispose(() => {
  alive = false
  controller?.abort()
  clearAttachments()
  prompt.value = ''
  output.value = ''
  images.value = []
})
</script>
<template>
  <section aria-label="真实调用">
    <h2>真实调用</h2>
    <p>请求将发送到所选模型对应的上游服务，可能产生真实费用。停止接收不代表取消上游计费。</p>
    <p>
      消息与文本输出只留在当前页面，离开后清空。上传素材和生成图片保存在文件库，按文件权限访问。
    </p>
    <el-tabs v-model="operation" aria-label="测试操作">
      <el-tab-pane
        v-for="op in operations"
        :key="op"
        :name="op"
        :label="operationLabels[op]"
        :disabled="calling"
      />
    </el-tabs>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="!permitted" type="warning" :closable="false"
      >当前操作需要模型调用权限；生成图片需要文件创建权限，上传与读取参考图还需要文件内容读取权限。</el-alert
    >
    <form class="dialog-form probe-form" @submit.prevent="invoke">
      <ModelPicker
        v-model="key"
        :operation="operation"
        :disabled="calling"
        @selected="selected = $event"
      />
      <label
        >测试消息<el-input
          v-model="prompt"
          required
          :maxlength="generates ? 16000 : 32000"
          :rows="4"
          :disabled="calling"
          type="textarea"
      /></label>
      <div v-if="generates" class="probe-parameters">
        <label
          >图片数量<el-input-number
            v-model="count"
            aria-label="图片数量"
            :min="1"
            :max="2"
            :step="1"
            step-strictly
            :disabled="calling"
        /></label>
        <label
          >分辨率<AppSelect v-model="resolution" aria-label="分辨率" :disabled="calling"
            ><el-option
              v-for="value in ['1K', '2K', '4K']"
              :key="value"
              :value="value"
              :label="value" /></AppSelect
        ></label>
        <label
          >图片格式<AppSelect v-model="format" aria-label="图片格式" :disabled="calling"
            ><el-option
              v-for="value in ['png', 'jpeg', 'webp']"
              :key="value"
              :value="value"
              :label="value.toUpperCase()" /></AppSelect
        ></label>
        <label v-if="selected?.image_parameters?.includes('aspect_ratio')"
          >图片比例<AppSelect v-model="ratio" aria-label="图片比例" :disabled="calling"
            ><el-option
              v-for="value in ['1:1', '16:9', '9:16', '4:3', '3:4']"
              :key="value"
              :value="value"
              :label="value" /></AppSelect
        ></label>
        <label v-if="selected?.image_parameters?.includes('quality')"
          >画质<AppSelect v-model="quality" aria-label="画质" :disabled="calling"
            ><el-option value="low" label="低" /><el-option value="medium" label="标准" /><el-option
              value="high"
              label="高" /></AppSelect
        ></label>
      </div>
      <label v-else
        >最大输出 Token<el-input-number
          v-model="maxTokens"
          aria-label="最大输出 Token"
          :min="1"
          :max="4096"
          :step="1"
          step-strictly
          :disabled="calling"
      /></label>
      <div v-if="needsImages" class="form-field">
        <span class="form-label">{{
          operation === 'image.edit' ? '原图 / 参考图（按上传顺序发送）' : '待理解图片'
        }}</span>
        <el-upload
          v-model:file-list="attachments"
          action="#"
          accept="image/png,image/jpeg,image/webp"
          :auto-upload="false"
          list-type="picture"
          multiple
          :limit="limit"
          :disabled="calling || !permitted"
          :on-change="checkFiles"
          :on-remove="removeAttachment"
          :on-exceed="() => (error = `最多选择 ${limit} 张图片`)"
        >
          <el-button native-type="button" :disabled="calling || !permitted">选择图片</el-button>
          <template #tip
            ><p>每张不超过 4 MiB；最多 {{ limit }} 张。总大小由所选协议校验。</p></template
          >
        </el-upload>
      </div>
      <div class="row-actions">
        <el-button :disabled="calling || !ready" native-type="submit" type="primary"
          >发送真实请求</el-button
        >
        <el-button v-if="calling" @click="stop" native-type="button">停止接收</el-button>
      </div>
      <p v-if="calling" role="status">{{ uploading || '正在调用模型，请等待…' }}</p>
    </form>
    <pre v-if="output" class="model-output" aria-live="polite">{{ output }}</pre>
    <div v-if="images.length" class="probe-images" aria-label="生成图片">
      <p v-if="!can('file:content_read')">
        图片已保存到文件库；需要文件内容读取权限才能预览或下载。
      </p>
      <figure v-for="(image, index) in images" :key="image.file_id">
        <el-image
          v-if="can('file:content_read')"
          :src="`/api/v1/files/${image.file_id}/content`"
          :preview-src-list="images.map((item) => `/api/v1/files/${item.file_id}/content`)"
          :initial-index="index"
          fit="contain"
          preview-teleported
          ><template #error>图片暂时无法读取，请检查文件权限。</template></el-image
        >
        <figcaption>
          图片 {{ index + 1 }}
          <el-button
            v-if="can('file:content_read')"
            native-type="button"
            @click="download(image.file_id, index)"
            >下载图片</el-button
          >
        </figcaption>
      </figure>
    </div>
    <p v-if="invocation">
      本次调用：{{ invocation }}
      <RouterLink
        v-if="can('model_invocation:read')"
        :to="{ path: '/model-usage', query: { tab: 'details', invocation } }"
        >查看调用与费用</RouterLink
      >
    </p>
  </section>
</template>
<style scoped>
.probe-form {
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
}
.model-output {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  min-width: 0;
}
.probe-parameters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 16px;
}
.probe-parameters label {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.probe-images {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 16px;
  margin-top: 20px;
}
.probe-images figure {
  margin: 0;
  min-width: 0;
}
.probe-images .el-image {
  width: 100%;
  height: 320px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
}
.probe-images figcaption {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
}
</style>
