<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth, getReadiness } from '../api'
import type { Health, Readiness } from '../api'
const health = ref<Health | null>(null), ready = ref<Readiness | null>(null), busy = ref(false)
const checked = ref('尚未检查')
const labels: Record<string, string> = { ok: '正常', unconfigured: '未配置', unavailable: '不可用', out_of_date: '待迁移' }
async function refresh() {
  if (busy.value) return
  busy.value = true
  const result = await Promise.allSettled([getHealth(), getReadiness()])
  health.value = result[0].status === 'fulfilled' ? result[0].value : null
  ready.value = result[1].status === 'fulfilled' ? result[1].value : null
  checked.value = new Date().toLocaleTimeString('zh-CN'); busy.value = false
}
onMounted(refresh)
</script>
<template>
  <div class="heading"><div><div class="eyebrow">PLATFORM / OVERVIEW</div><h1>平台状态</h1><p>查看服务连接与运行配置。</p></div><button :disabled="busy" @click="refresh">刷新状态</button></div>
  <section class="status-grid" aria-live="polite">
    <article class="card"><h2>接口服务</h2><div class="value">{{ health ? '在线' : '无法确认' }}</div><p v-if="health">Keystone API · {{ health.version }}</p></article>
    <article v-for="(label, key) in { database: '数据库连接', schema: '数据库版本', storage: '文件存储' }" :key="key" class="card"><h2>{{ label }}</h2><div class="value">{{ labels[ready?.checks[key] ?? ''] ?? '无法确认' }}</div></article>
  </section>
  <p>Flow 配置：{{ ready ? (ready.flow_enabled ? '已启用' : '未启用') : '无法确认' }}。API 就绪不代表 Worker 已在线。</p>
  <p>模型发送模式请在模型接入页查看；真实调用可能产生费用。</p><footer>最近检查 · {{ checked }}</footer>
</template>
