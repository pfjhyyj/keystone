<script setup lang="ts">
import { modelApi } from '../../../api/models'
import { usePagedList } from '../../../composables/usePagedList'
import ListPager from '../../../components/ListPager.vue'
defineProps<{ disabled: boolean }>()
const model = defineModel<string>({ required: true })
const { rows, page, size, total, loading, error, setSize } = usePagedList(
  'models.channel-picker',
  'model:manage',
  modelApi.channelsPage,
)
</script>
<template>
  <div class="subject-picker">
    <div class="form-field">
      <span class="form-label">模型渠道</span
      ><AppSelect v-model="model" aria-label="模型渠道" :disabled="disabled || loading" required
        ><el-option value="" :label="'请选择渠道'" /><el-option
          v-if="model && !rows.some((row) => row.id === model)"
          :value="model"
          :label="model + '（当前渠道）'" /><el-option
          v-for="row in rows"
          :key="row.id"
          :value="row.id"
          :label="row.name + (row.is_active ? '' : ' · 停用')"
      /></AppSelect>
    </div>
    <ListPager
      :page="page"
      :count="rows.length"
      :loading="loading || disabled"
      :size="size"
      :total="total"
      @change="page = $event"
      @size-change="setSize"
    />
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
  </div>
</template>
