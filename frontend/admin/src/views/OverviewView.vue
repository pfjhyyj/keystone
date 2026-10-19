<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Connection, Cpu, Files, Monitor, Refresh } from '@element-plus/icons-vue'
import { getHealth, getReadiness } from '../api'
import type { Health, Readiness } from '../api'
const health = ref<Health | null>(null),
  ready = ref<Readiness | null>(null),
  busy = ref(false)
const checked = ref('尚未检查')
const labels: Record<string, string> = {
  ok: '正常',
  unconfigured: '未配置',
  unavailable: '不可用',
  out_of_date: '待迁移',
}
const cards = computed(() => [
  {
    label: '接口服务',
    icon: Monitor,
    value: health.value ? '在线' : '无法确认',
    good: !!health.value,
    note: health.value ? `Keystone API · ${health.value.version}` : '等待接口响应',
  },
  ...(
    [
      { key: 'database', label: '数据库连接', icon: Connection },
      { key: 'schema', label: '数据库版本', icon: Cpu },
      { key: 'storage', label: '文件存储', icon: Files },
    ] as const
  ).map((item) => ({
    ...item,
    value: labels[ready.value?.checks[item.key] ?? ''] ?? '无法确认',
    good: ready.value?.checks[item.key] === 'ok',
    note:
      item.key === 'schema'
        ? '业务迁移版本检查'
        : item.key === 'storage'
          ? '默认存储源与协调卷检查'
          : 'PostgreSQL 连接检查',
  })),
])
async function refresh() {
  if (busy.value) return
  busy.value = true
  const result = await Promise.allSettled([getHealth(), getReadiness()])
  health.value = result[0].status === 'fulfilled' ? result[0].value : null
  ready.value = result[1].status === 'fulfilled' ? result[1].value : null
  checked.value = new Date().toLocaleString('zh-CN')
  busy.value = false
}
onMounted(refresh)
</script>
<template>
  <section class="overview-page" aria-label="平台状态">
    <div class="heading">
      <div>
        <h1>平台状态</h1>
        <p>查看服务连接、存储依赖与运行配置。</p>
      </div>
      <el-button :icon="Refresh" :loading="busy" @click="refresh">刷新状态</el-button>
    </div>
    <div class="status-grid" aria-live="polite">
      <el-card v-for="card in cards" :key="card.label" shadow="never" class="status-card"
        ><div class="status-card-header">
          <span>{{ card.label }}</span
          ><el-icon><component :is="card.icon" /></el-icon>
        </div>
        <div class="status-value" :class="{ 'is-warning': !card.good }">{{ card.value }}</div>
        <p>{{ card.note }}</p></el-card
      >
    </div>
    <el-card shadow="never" class="overview-detail"
      ><template #header><h2>运行配置</h2></template
      ><el-descriptions :column="1" border
        ><el-descriptions-item label="Flow 执行能力"
          ><StatusTag :status="ready?.flow_enabled ? 'active' : 'disabled'">{{
            ready ? (ready.flow_enabled ? '已启用' : '未启用') : '无法确认'
          }}</StatusTag></el-descriptions-item
        ><el-descriptions-item label="Worker 在线状态">需通过独立健康检查确认</el-descriptions-item
        ><el-descriptions-item label="最近检查">{{
          checked
        }}</el-descriptions-item></el-descriptions
      >
      <div class="overview-note">
        <el-tag disable-transitions type="info" effect="plain">就绪检查</el-tag>
        <p>API 就绪不代表 Worker 已在线；模型渠道配置可在模型接入页查看。</p>
      </div></el-card
    >
  </section>
</template>
