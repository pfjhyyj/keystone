<script setup lang="ts">
import { computed } from 'vue'
import { can } from '../../stores/session'
import { fileApi, uuidPattern } from '../../api/files'
import { usePagedList } from '../../composables/usePagedList'
import ListPager from '../../components/ListPager.vue'
import ModelPicker from '../../components/models/ModelPicker.vue'

defineProps<{ disabled: boolean }>()
const input = defineModel<Record<string, unknown>>({ required: true })
const fileId = computed({
  get: () => String(input.value.source_file_id ?? ''),
  set: (value) => {
    input.value = { ...input.value, source_file_id: value }
  },
})
const modelKey = computed({
  get: () => String(input.value.model_key ?? ''),
  set: (value) => {
    input.value = { ...input.value, model_key: value }
  },
})
const files = usePagedList('file:inspect', fileApi.list)
</script>

<template>
  <template v-if="can('file:inspect')">
    <div class="form-field">
      <span class="form-label">可见源文件 </span
      ><AppSelect
        v-model="fileId"
        aria-label="可见源文件"
        :disabled="disabled || files.loading.value"
        ><el-option value="" :label="'选择文件或填写下方 ID'" /><el-option
          v-if="fileId && !files.rows.value.some((row) => row.id === fileId)"
          :value="fileId"
          :label="fileId + '（已选）'" /><el-option
          v-for="row in files.rows.value.filter((row) => row.status === 'active')"
          :key="row.id"
          :value="row.id"
          :label="row.name"
      /></AppSelect>
    </div>
    <ListPager
      v-if="files.page.value || files.rows.value.length >= 50"
      :page="files.page.value"
      :count="files.rows.value.length"
      :loading="disabled || files.loading.value"
      @change="files.page.value = $event"
    />
    <el-button
      :disabled="disabled || files.loading.value"
      @click="files.reload"
      native-type="button"
      type="default"
    >
      刷新源文件
    </el-button>
    <el-alert v-if="files.error.value" role="alert" type="error" show-icon :closable="false">{{
      files.error.value
    }}</el-alert>
  </template>
  <label
    >源文件 ID<el-input
      v-model="fileId"
      :pattern="uuidPattern"
      required
      :disabled="disabled"
      placeholder="已知文件 UUID"
  /></label>
  <p>文件须可读取且为不超过 16 KiB 的 UTF-8 文本；可见文件不自动代表有内容读取权。</p>
  <ModelPicker v-model="modelKey" :disabled="disabled" />
  <p>本流程将调用真实模型，可能产生费用；取消不撤销已发生的调用。</p>
</template>
