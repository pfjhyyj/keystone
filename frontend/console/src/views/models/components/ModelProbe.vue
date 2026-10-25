<script setup lang="ts">
import { onScopeDispose, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { can } from '../../../stores/session'
import { invokeModel } from '../../../api/client'
import ModelPicker from '../../../components/models/ModelPicker.vue'
const emit = defineEmits<{ completed: [] }>()
const key = ref(''),
  prompt = ref(''),
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
  if (calling.value || !key.value || !prompt.value || !can('model:invoke')) return
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
        stream: false,
      },
      (event) => {
        if (!alive || !can('model:invoke')) return
        if (typeof event.invocation_id === 'string') invocation.value = event.invocation_id
        if (event.type === 'error') throw new Error(String(event.detail))
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
  <section aria-label="真实调用">
    <h2>真实调用</h2>
    <p>请求将发送到所选模型对应的上游服务，可能产生真实费用。停止接收不代表取消上游计费。</p>
    <p>消息与输出只留在当前页面，离开后清空。</p>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <form class="dialog-form probe-form" @submit.prevent="invoke">
      <ModelPicker v-model="key" :disabled="calling" /><label
        >测试消息<el-input
          v-model="prompt"
          required
          maxlength="32000"
          :rows="3"
          :disabled="calling"
          type="textarea"
      /></label>
      <div class="row-actions">
        <el-button :disabled="calling || !key || !prompt" native-type="submit" type="primary"
          >发送真实请求</el-button
        ><el-button v-if="calling" @click="stop" native-type="button" type="default"
          >停止接收</el-button
        >
      </div>
    </form>
    <pre v-if="output" class="model-output" aria-live="polite">{{ output }}</pre>
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
