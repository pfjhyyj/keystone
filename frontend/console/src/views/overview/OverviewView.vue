<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { getHealth, getReadiness, getWorkerStatus } from '../../api/system'
import type { Health, Readiness, WorkerStatus } from '../../api/system'
const props = defineProps<{ section?: string; workerId?: string }>()
const health = ref<Health | null>(null),
  ready = ref<Readiness | null>(null),
  worker = ref<WorkerStatus | null>(null),
  busy = ref(false)
let alive = true
let timer: ReturnType<typeof setInterval> | undefined
const checked = ref('尚未检查')
const checkedOnce = ref(false)
const dependenciesTarget = ref<HTMLElement | null>(null)
const workersTarget = ref<HTMLElement | null>(null)
const workerTarget = ref<HTMLElement | null>(null)
const maintenanceTarget = ref<HTMLElement | null>(null)
const selectedWorker = computed(() =>
  props.section === '#worker-instances'
    ? worker.value?.instances?.find((instance) => instance.worker_id === props.workerId)?.worker_id
    : undefined,
)
watch(
  () => {
    if (props.section === '#dependencies') return dependenciesTarget.value
    if (!checkedOnce.value) return null
    if (props.section === '#shared-maintenance') return maintenanceTarget.value
    if (props.section === '#worker-instances')
      return selectedWorker.value ? workerTarget.value : workersTarget.value
    return null
  },
  (target) => {
    target?.focus({ preventScroll: true })
    target?.scrollIntoView?.({ block: 'nearest' })
  },
  { flush: 'post' },
)
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
    value: health.value ? '在线' : '无法确认',
    good: !!health.value,
    note: health.value ? `Keystone API · ${health.value.version}` : '等待接口响应',
  },
  {
    label: 'Worker 服务',
    value: workerLabels[worker.value?.status ?? 'unknown'],
    good: worker.value?.status === 'ready',
    note:
      worker.value?.expected_workers !== undefined
        ? `${worker.value.ready_workers ?? 0} / ${worker.value.expected_workers} 个进程就绪`
        : '30 秒无心跳视为离线',
  },
  ...(
    [
      { key: 'database', label: '数据库连接' },
      { key: 'schema', label: '数据库版本' },
      { key: 'storage', label: '文件存储' },
    ] as const
  ).map((item) => ({
    ...item,
    value: labels[ready.value?.checks[item.key] ?? ''] ?? '无法确认',
    good: ready.value?.checks[item.key] === 'ok',
    note: item.key === 'schema' ? '迁移版本' : item.key === 'storage' ? '默认存储源' : 'PostgreSQL',
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
  checkedOnce.value = true
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
      <h1>平台状态</h1>
      <el-button :icon="Refresh" :loading="busy" @click="refresh">刷新</el-button>
    </div>
    <ul
      id="dependencies"
      ref="dependenciesTarget"
      class="status-list"
      tabindex="-1"
      aria-live="polite"
    >
      <li v-for="card in cards" :key="card.label" class="status-row">
        <span class="state-dot" :class="{ 'is-warning': !card.good }" aria-hidden="true" />
        <span class="status-name">{{ card.label }}</span>
        <strong class="status-value" :class="{ 'is-warning': !card.good }">{{ card.value }}</strong>
        <small>{{ card.note }}</small>
      </li>
    </ul>
    <el-card
      v-if="worker?.instances?.length || section === '#worker-instances'"
      shadow="never"
      class="overview-detail"
    >
      <template #header>
        <h2 id="worker-instances" ref="workersTarget" tabindex="-1">Worker 实例</h2>
      </template>
      <p v-if="workerId && !selectedWorker && checkedOnce" class="muted">
        {{ worker ? `当前快照中没有实例 ${workerId}` : '无法确认实例，Worker 状态暂不可用' }}
      </p>
      <el-table
        v-if="worker?.instances?.length"
        :data="worker.instances"
        row-key="worker_id"
        :row-class-name="
          ({ row }: { row: { worker_id: string } }) =>
            row.worker_id === selectedWorker ? 'located-worker' : ''
        "
      >
        <el-table-column label="固定身份">
          <template #default="{ row }">
            <span
              v-if="row.worker_id === selectedWorker"
              :id="`worker-${row.worker_id}`"
              ref="workerTarget"
              tabindex="-1"
              >{{ row.worker_id }}</span
            >
            <span v-else>{{ row.worker_id }}</span>
          </template>
        </el-table-column>
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
      <p v-else-if="!workerId || !checkedOnce" class="muted">
        {{ !checkedOnce ? '正在检查实例…' : worker ? '暂无登记实例' : '运行状态暂不可用' }}
      </p>
    </el-card>
    <el-card
      v-if="Object.keys(worker?.maintenance ?? {}).length || section === '#shared-maintenance'"
      shadow="never"
      class="overview-detail"
    >
      <template #header>
        <h2 id="shared-maintenance" ref="maintenanceTarget" tabindex="-1">共享维护</h2>
      </template>
      <div v-if="Object.keys(worker?.maintenance ?? {}).length" class="maintenance-status">
        <el-tag
          v-for="(task, name) in worker?.maintenance"
          :key="name"
          :type="task.status === 'ready' ? 'success' : 'warning'"
          effect="plain"
          disable-transitions
          >{{ taskLabels[String(name)] ?? name }}：{{ workerLabels[task.status] }}</el-tag
        >
      </div>
      <p v-else class="muted">{{ checkedOnce ? '暂无可确认的维护状态' : '正在检查维护状态…' }}</p>
    </el-card>
    <el-card v-if="worker?.backlog" shadow="never" class="overview-detail">
      <template #header><h2>待处理任务</h2></template>
      <el-descriptions :column="1" border>
        <el-descriptions-item
          v-for="(item, name) in worker.backlog"
          :key="name"
          :label="taskLabels[String(name)] ?? String(name)"
        >
          <span v-if="item.status === 'unknown'">无法确认</span>
          <span v-else
            >{{ item.pending }} 项；最早待办：{{
              item.oldest_pending_at
                ? new Date(item.oldest_pending_at).toLocaleString('zh-CN')
                : '无'
            }}<template v-if="item.oldest_pending_age_seconds !== null"
              >（距今 {{ item.oldest_pending_age_seconds }} 秒）</template
            ></span
          >
        </el-descriptions-item>
      </el-descriptions>
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
    </el-card>
  </section>
</template>
<style scoped>
[tabindex='-1'] {
  scroll-margin-top: 72px;
}
[tabindex='-1']:focus {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 3px;
}
.maintenance-status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
:deep(.el-table tr.located-worker > td) {
  background: var(--el-color-primary-light-9);
}
</style>
