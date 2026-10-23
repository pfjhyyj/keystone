<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref } from 'vue'
import { Connection, Cpu, Files, Monitor, Refresh } from '@element-plus/icons-vue'
import { getHealth, getReadiness, getWorkerStatus } from '../../api/system'
import type { Health, Readiness, WorkerStatus } from '../../api/system'
const health = ref<Health | null>(null),
  ready = ref<Readiness | null>(null),
  worker = ref<WorkerStatus | null>(null),
  busy = ref(false)
let alive = true
let timer: ReturnType<typeof setInterval> | undefined
const checked = ref('尚未检查')
const workerLabels = { ready: '就绪', unready: '未就绪', offline: '离线', unknown: '无法确认' }
const processLabels: Record<string, string> = {
  starting: '启动中',
  ready: '就绪',
  unready: '未就绪',
  draining: '正在排空',
  stopped: '已停止',
  missing: '尚未启动',
  retired: '已退役',
}
const taskLabels: Record<string, string> = {
  files: '文件清理',
  flow: '流程协调',
  model_costs: '费用扫描',
}
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
  {
    label: 'Worker 服务',
    icon: Cpu,
    value: workerLabels[worker.value?.status ?? 'unknown'],
    good: worker.value?.status === 'ready',
    note:
      worker.value?.expected_workers !== undefined
        ? `${worker.value.ready_workers ?? 0} / ${worker.value.expected_workers} 个进程就绪；共享维护另行检查`
        : '独立心跳检查，超过 30 秒未上报视为离线',
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
  const result = await Promise.allSettled([getHealth(), getReadiness(), getWorkerStatus()])
  if (!alive) return
  health.value = result[0].status === 'fulfilled' ? result[0].value : null
  ready.value = result[1].status === 'fulfilled' ? result[1].value : null
  worker.value = result[2].status === 'fulfilled' ? result[2].value : null
  checked.value = new Date().toLocaleString('zh-CN')
  busy.value = false
}
onMounted(() => {
  void refresh()
  timer = setInterval(refresh, 5000)
})
onScopeDispose(() => {
  alive = false
  clearInterval(timer)
})
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
    <el-card v-if="worker?.instances?.length" shadow="never" class="overview-detail">
      <template #header><h2>Worker 实例</h2></template>
      <el-table :data="worker.instances" row-key="worker_id">
        <el-table-column prop="worker_id" label="固定身份" />
        <el-table-column label="进程状态"
          ><template #default="{ row }"
            >{{ workerLabels[row.status as keyof typeof workerLabels] }} ·
            {{ processLabels[row.state] ?? row.state }}</template
          ></el-table-column
        >
        <el-table-column label="当前实例"
          ><template #default="{ row }">{{
            row.instance_id?.slice(0, 8) ?? '—'
          }}</template></el-table-column
        >
        <el-table-column label="执行版本"
          ><template #default="{ row }">{{
            row.execution_version?.slice(0, 12) ?? '—'
          }}</template></el-table-column
        >
        <el-table-column label="最近心跳"
          ><template #default="{ row }">{{
            row.last_heartbeat_at
              ? new Date(row.last_heartbeat_at).toLocaleString('zh-CN')
              : '暂无记录'
          }}</template></el-table-column
        >
        <el-table-column label="身份登记"
          ><template #default="{ row }">{{
            row.expected ? '已登记' : row.state === 'retired' ? '已退役' : '未登记'
          }}</template></el-table-column
        >
      </el-table>
      <div v-if="worker.maintenance" class="overview-note">
        <el-tag
          v-for="(task, name) in worker.maintenance"
          :key="name"
          :type="task.status === 'ready' ? 'success' : 'warning'"
          effect="plain"
          disable-transitions
          >{{ taskLabels[String(name)] ?? name }}：{{ workerLabels[task.status] }}</el-tag
        >
      </div>
    </el-card>
    <el-card shadow="never" class="overview-detail"
      ><template #header><h2>运行配置</h2></template
      ><el-descriptions :column="1" border
        ><el-descriptions-item label="Worker 最近心跳"
          ><time v-if="worker?.last_heartbeat_at" :datetime="worker.last_heartbeat_at">{{
            new Date(worker.last_heartbeat_at).toLocaleString('zh-CN')
          }}</time
          ><span v-else>{{
            worker?.status === 'offline' ? '暂无记录' : '无法读取'
          }}</span></el-descriptions-item
        ><el-descriptions-item label="最近检查">{{
          checked
        }}</el-descriptions-item></el-descriptions
      >
      <div class="overview-note">
        <el-tag disable-transitions type="info" effect="plain">就绪检查</el-tag>
        <p>状态每 5 秒刷新；Worker 心跳独立于 API 就绪检查。模型渠道配置可在模型接入页查看。</p>
      </div></el-card
    >
  </section>
</template>
