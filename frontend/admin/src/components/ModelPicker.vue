<script setup lang="ts">
import { modelApi } from '../model-api'
import { usePagedList } from '../composables/usePagedList'
import ListPager from './ListPager.vue'
const props = defineProps<{ disabled?: boolean }>()
const model = defineModel<string>({ required: true })
const { rows, page, loading, error, reload } = usePagedList('model:invoke', async (offset) => {
  const data = await modelApi.catalog(offset)
  return data.items
})
</script>
<template>
  <div class="subject-picker">
    <div class="form-field">
      <span class="form-label">模型</span
      ><AppSelect v-model="model" aria-label="模型" :disabled="props.disabled || loading" required
        ><el-option value="" :label="'请选择模型'" /><el-option
          v-if="model && !rows.some((row) => row.key === model)"
          :value="model"
          :label="model + '（已选，提交时复查）'" /><el-option
          v-for="row in rows"
          :key="row.key"
          :value="row.key"
          :label="row.key"
      /></AppSelect>
    </div>
    <div class="row-actions">
      <el-button
        :disabled="props.disabled || loading"
        @click="reload"
        native-type="button"
        type="default"
      >
        刷新可用模型
      </el-button>
    </div>
    <ListPager
      v-if="page || rows.length >= 50"
      :page="page"
      :count="rows.length"
      :loading="loading || !!props.disabled"
      @change="page = $event"
    />
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <p v-else-if="!loading && !rows.length">本页没有可用模型。</p>
  </div>
</template>
