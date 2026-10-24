<script setup lang="ts">
import { ref, watch } from 'vue'
import { request } from '../../../api/client'
const props = defineProps<{ channelId: string; disabled: boolean }>()
const model = defineModel<string>({ required: true })
const items = ref<{ id: string }[]>([]),
  loading = ref(false),
  error = ref(''),
  loaded = ref(false),
  version = ref(0)
watch(
  () => [props.channelId, version.value] as const,
  async ([id], _, onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    items.value = []
    error.value = ''
    loaded.value = false
    loading.value = !!id
    if (!id) return
    try {
      const result = await request<{ items: { id: string }[] }>(
        `/model-channels/${id}/models`,
        'GET',
        undefined,
        true,
        controller.signal,
      )
      if (controller.signal.aborted) return
      items.value = result.items
      loaded.value = true
    } catch (reason) {
      if (!controller.signal.aborted)
        error.value = reason instanceof Error ? reason.message : '模型列表查询失败'
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)
</script>
<template>
  <div class="form-field">
    <span class="form-label">上游模型</span>
    <AppSelect
      v-model="model"
      aria-label="上游模型"
      required
      filterable
      allow-create
      default-first-option
      :disabled="disabled || !channelId"
      :loading="loading"
      placeholder="搜索或输入上游模型 ID"
    >
      <el-option
        v-if="model && !items.some((item) => item.id === model)"
        :value="model"
        :label="model"
      />
      <el-option v-for="item in items" :key="item.id" :value="item.id" :label="item.id" />
    </AppSelect>
    <el-button
      :disabled="disabled || !channelId || loading"
      native-type="button"
      @click="version++"
    >
      刷新上游模型
    </el-button>
    <p v-if="loading" role="status">正在查询渠道模型列表…</p>
    <p v-if="loaded">已获取 {{ items.length }} 个模型；操作能力仍需按模型实际支持范围选择。</p>
    <p v-if="loaded && model && !items.some((item) => item.id === model)" role="alert">
      当前渠道返回的列表不包含此模型，请确认模型 ID 和 Key 权限。
    </p>
    <p v-if="error" role="alert">{{ error }}；可稍后刷新，或手动输入模型 ID。</p>
  </div>
</template>
