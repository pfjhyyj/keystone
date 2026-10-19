<script setup lang="ts">
import { onScopeDispose, ref } from 'vue'
import { can } from '../access'
import { invokeModel } from '../auth'
import ModelPicker from './ModelPicker.vue'
const emit = defineEmits<{ completed: [] }>()
const key = ref(''),
  mode = ref(''),
  prompt = ref(''),
  streaming = ref(true),
  output = ref(''),
  invocation = ref(''),
  error = ref(''),
  calling = ref(false)
let controller: AbortController | null = null,
  alive = true
function stop() {
  controller?.abort()
}
onScopeDispose(() => {
  alive = false
  controller?.abort()
  prompt.value = ''
  output.value = ''
})
async function invoke() {
  if (calling.value || !['mock', 'live'].includes(mode.value) || !can('model:invoke')) return
  calling.value = true
  output.value = ''
  invocation.value = ''
  error.value = ''
  const current = new AbortController()
  controller = current
  try {
    await invokeModel(
      {
        model_key: key.value,
        messages: [{ role: 'user', content: prompt.value }],
        stream: streaming.value,
      },
      (event) => {
        if (!alive || !can('model:invoke')) return
        if (typeof event.invocation_id === 'string') invocation.value = event.invocation_id
        if (event.type === 'error') throw new Error(String(event.detail))
        if (event.type === 'delta') output.value += String(event.text)
        if (event.type === 'done' && typeof event.content === 'string') output.value = event.content
      },
      current.signal,
    )
  } catch (reason) {
    if (alive)
      error.value = current.signal.aborted
        ? '已停止接收；上游可能已经执行，请查看调用记录，不要盲目重试。'
        : reason instanceof Error
          ? reason.message
          : '调用失败'
  } finally {
    if (alive) {
      calling.value = false
      controller = null
      emit('completed')
    }
  }
}
</script>
<template>
  <section :aria-label="mode === 'live' ? '真实调用' : '模拟调用'">
    <h2>{{ mode === 'live' ? '真实调用' : '模拟调用' }}</h2>
    <p v-if="mode === 'mock'">仅验证链路，不代表模型质量或真实账务。</p>
    <p v-if="mode === 'live'">
      请求将发送到 OpenRouter，可能产生真实费用。停止接收不代表取消上游计费。
    </p>
    <p>消息与输出只留在当前页面，离开后清空。</p>
    <p v-if="!['mock', 'live'].includes(mode)">
      {{ mode === 'disabled' ? '模型发送已停用。' : '尚未确认发送模式。' }}
    </p>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <form class="dialog-form probe-form" @submit.prevent="invoke">
      <ModelPicker v-model="key" :disabled="calling" @mode="mode = $event" /><label
        >测试消息<el-input
          v-model="prompt"
          required
          maxlength="32000"
          :rows="3"
          :disabled="calling"
          type="textarea" /></label
      ><el-checkbox class="check-option" v-model="streaming" :disabled="calling"
        >流式响应</el-checkbox
      >
      <div class="row-actions">
        <el-button
          :disabled="calling || !['mock', 'live'].includes(mode) || !key"
          native-type="submit"
          type="primary"
          >{{ mode === 'live' ? '发送真实请求' : '开始模拟调用' }}</el-button
        ><el-button v-if="calling" @click="stop" native-type="button" type="default"
          >停止接收</el-button
        >
      </div>
    </form>
    <pre v-if="output" class="model-output" aria-live="polite">{{ output }}</pre>
    <p v-if="invocation">本次调用：{{ invocation }}</p>
  </section>
</template>
