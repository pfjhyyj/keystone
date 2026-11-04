<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { Refresh, ArrowRight, WarningFilled } from '@element-plus/icons-vue'
import { can, sessionVersion } from '../../stores/session'
import {
  getHealth,
  getReadiness,
  getWorkerStatus,
  type Health,
  type Readiness,
  type WorkerStatus,
} from '../../api/system'
import { modelApi, providers, channelProvider, type Channel } from '../../api/models'
import { usageApi, percent, type UsageStatistics } from '../../api/modelUsage'
import MoneyAmount from '../../components/MoneyAmount.vue'
import UsageTrend from '../usage/components/UsageTrend.vue'

const router = useRouter()
const health = ref<Health | null>(null),
  readiness = ref<Readiness | null>(null),
  worker = ref<WorkerStatus | null>(null)
const statistics = ref<UsageStatistics | null>(null),
  channels = ref<Channel[]>([])
const statusBusy = ref(false),
  usageBusy = ref(false),
  usageError = ref(''),
  channelError = ref('')
const checked = ref('尚未检查')
let alive = true,
  timer: ReturnType<typeof setInterval> | undefined
let usageGeneration = 0
const summary = computed(() => statistics.value?.summary)
const maintenanceIssue = computed(() =>
  Object.values(worker.value?.maintenance ?? {}).some((item) => item.status !== 'ready'),
)
const statusNames: Record<string, string> = {
  ready: '就绪',
  unready: '未就绪',
  offline: '离线',
  unknown: '无法确认',
  starting: '启动中',
  missing: '尚未启动',
  stopped: '已停止',
  draining: '正在排空',
  retired: '已退役',
}
const serviceStates = computed(() => [
  { name: 'API', good: !!health.value, text: health.value ? '在线' : '无法确认' },
  {
    name: '数据库',
    good: readiness.value?.checks.database === 'ok',
    text: readiness.value?.checks.database === 'ok' ? '正常' : '待检查',
  },
  {
    name: '数据库版本',
    good: readiness.value?.checks.schema === 'ok',
    text: readiness.value?.checks.schema === 'ok' ? '正常' : '待检查',
  },
  {
    name: '存储',
    good: readiness.value?.checks.storage === 'ok',
    text: readiness.value?.checks.storage === 'ok' ? '正常' : '待检查',
  },
  {
    name: 'Worker',
    good: worker.value?.status === 'ready',
    text: worker.value
      ? `${worker.value.ready_workers ?? '—'}/${worker.value.expected_workers ?? '—'} ${statusNames[worker.value.status]}`
      : '无法确认',
  },
])
async function refreshStatus() {
  if (statusBusy.value) return
  statusBusy.value = true
  const values = await Promise.allSettled([getHealth(), getReadiness(), getWorkerStatus()])
  if (!alive) return
  health.value = values[0].status === 'fulfilled' ? values[0].value : null
  readiness.value = values[1].status === 'fulfilled' ? values[1].value : null
  worker.value = values[2].status === 'fulfilled' ? values[2].value : null
  checked.value = new Date().toLocaleTimeString('zh-CN')
  statusBusy.value = false
}
async function refreshUsage() {
  if (usageBusy.value) return
  const generation = ++usageGeneration
  usageBusy.value = true
  usageError.value = ''
  channelError.value = ''
  const until = new Date(),
    since = new Date(until.getTime() - 30 * 86400000)
  const result = await Promise.allSettled([
    can('model_invocation:read')
      ? usageApi.statistics({
          since: since.toISOString(),
          until: until.toISOString(),
          group_by: 'channel',
          bucket: 'day',
        })
      : Promise.resolve(null),
    can('model:manage') ? modelApi.channels(0) : Promise.resolve([]),
  ])
  if (!alive || generation !== usageGeneration) return
  statistics.value = result[0].status === 'fulfilled' ? result[0].value : null
  if (result[0].status === 'rejected') usageError.value = '用量统计暂时不可用，请刷新重试。'
  channels.value = result[1].status === 'fulfilled' ? result[1].value : []
  if (result[1].status === 'rejected') channelError.value = '渠道列表暂时不可用。'
  usageBusy.value = false
}
function refresh() {
  void refreshStatus()
  void refreshUsage()
}
watch(
  sessionVersion,
  () => {
    usageGeneration++
    statistics.value = null
    channels.value = []
    usageBusy.value = false
    void refreshUsage()
  },
  { flush: 'sync' },
)
function drill(filters: Record<string, string>) {
  void router.push({
    path: '/model-usage',
    query: {
      since: statistics.value?.since,
      until: statistics.value?.until,
      ...filters,
      tab: 'details',
    },
  })
}
onMounted(() => {
  refresh()
  timer = setInterval(refreshStatus, 5000)
})
onScopeDispose(() => {
  alive = false
  clearInterval(timer)
})
</script>
<template>
  <section class="dashboard-page" aria-label="管理总览">
    <div class="heading">
      <h1>管理总览</h1>
      <el-button :icon="Refresh" :loading="statusBusy || usageBusy" @click="refresh"
        >刷新</el-button
      >
    </div>
    <div class="service-strip" aria-live="polite">
      <span v-for="item in serviceStates" :key="item.name"
        ><i class="state-dot" :class="{ 'is-warning': !item.good }" />{{ item.name }}
        <strong>{{ item.text }}</strong></span
      ><RouterLink to="/platform"
        >平台状态 <el-icon><ArrowRight /></el-icon
      ></RouterLink>
    </div>
    <div class="dashboard-columns">
      <div class="dashboard-operations">
        <section class="work-panel attention-panel">
          <div class="panel-heading">
            <h2>需要关注</h2>
            <span class="muted">最近检查 {{ checked }}</span>
          </div>
          <div v-if="!health || readiness?.status !== 'ready'" class="attention-row">
            <el-icon class="attention-marker"><WarningFilled /></el-icon>
            <div>
              <strong>平台依赖需要检查</strong>
              <p>请确认 API、数据库版本和存储的就绪状态。</p>
            </div>
            <RouterLink to="/platform"
              >检查依赖 <el-icon><ArrowRight /></el-icon
            ></RouterLink>
          </div>
          <div v-if="!worker || worker.status !== 'ready'" class="attention-row">
            <el-icon class="attention-marker"><WarningFilled /></el-icon>
            <div>
              <strong>Worker {{ statusNames[worker?.status ?? 'unknown'] }}</strong>
              <p>
                {{
                  worker
                    ? `${worker.ready_workers ?? '—'} / ${worker.expected_workers ?? '—'} 个进程就绪`
                    : '未能确认运行状态，请检查平台连接。'
                }}
              </p>
              <p v-if="maintenanceIssue">共享维护也需检查，进程就绪与维护状态分别确认。</p>
            </div>
            <RouterLink to="/platform"
              >查看实例 <el-icon><ArrowRight /></el-icon
            ></RouterLink>
          </div>
          <div v-if="worker?.status === 'ready' && maintenanceIssue" class="attention-row">
            <el-icon class="attention-marker"><WarningFilled /></el-icon>
            <div>
              <strong>共享维护需要检查</strong>
              <p>进程状态与文件清理、流程协调、费用扫描分别确认。</p>
            </div>
            <RouterLink to="/platform"
              >查看维护 <el-icon><ArrowRight /></el-icon
            ></RouterLink>
          </div>
          <div v-if="summary?.unknown_cost_calls" class="attention-row">
            <el-icon class="attention-marker"><WarningFilled /></el-icon>
            <div>
              <strong>费用待核对</strong>
              <p>
                {{ summary.pending_calls }} 次等待核对，{{ summary.unknown_cost_calls }}
                次费用未知。
              </p>
            </div>
            <el-button link @click="drill({ cost_known: 'false' })"
              >查看调用 <el-icon><ArrowRight /></el-icon
            ></el-button>
          </div>
          <p
            v-if="
              health &&
              readiness?.status === 'ready' &&
              worker?.status === 'ready' &&
              !maintenanceIssue &&
              !summary?.unknown_cost_calls
            "
            class="panel-empty"
          >
            当前已确认的状态没有需要处理的异常。
          </p>
        </section>
        <section class="work-panel">
          <div class="panel-heading">
            <h2>Worker 实例</h2>
            <RouterLink to="/platform">完整状态</RouterLink>
          </div>
          <div class="native-table-scroll">
            <table class="compact-table">
              <thead>
                <tr>
                  <th>固定身份</th>
                  <th>状态</th>
                  <th>最近心跳</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="instance in worker?.instances ?? []" :key="instance.worker_id">
                  <td>
                    <strong>{{ instance.worker_id }}</strong>
                  </td>
                  <td>
                    <span class="inline-state"
                      ><i
                        class="state-dot"
                        :class="{ 'is-warning': instance.status !== 'ready' }"
                      />{{ statusNames[instance.state] ?? instance.state }}</span
                    >
                  </td>
                  <td>
                    {{
                      instance.last_heartbeat_at
                        ? new Date(instance.last_heartbeat_at).toLocaleTimeString('zh-CN')
                        : '暂无记录'
                    }}
                  </td>
                </tr>
                <tr v-if="!worker?.instances?.length">
                  <td colspan="3">{{ worker ? '暂无登记实例。' : '运行状态暂不可用。' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
      <section v-if="can('model_invocation:read')" class="work-panel dashboard-usage">
        <div class="panel-heading">
          <h2>近 30 天用量</h2>
          <RouterLink to="/model-usage"
            >查看全部用量 <el-icon><ArrowRight /></el-icon
          ></RouterLink>
        </div>
        <el-alert v-if="usageError" type="error" :closable="false">{{ usageError }}</el-alert
        ><el-skeleton v-else-if="usageBusy && !summary" :rows="5" animated />
        <template v-else-if="summary"
          ><div class="dashboard-money">
            <div v-for="cost in summary.costs" :key="cost.unit">
              <span>已知费用 · {{ cost.unit }}</span
              ><strong><MoneyAmount :amount="cost.amount" :unit="cost.unit" /></strong>
            </div>
            <p v-if="!summary.costs.length">暂无已知费用，未知金额不计为零。</p>
          </div>
          <div class="dashboard-metrics">
            <span
              ><strong>{{ summary.calls.toLocaleString() }}</strong> 次调用</span
            ><span
              >成功率 <strong>{{ percent(summary.succeeded, summary.calls) }}</strong></span
            ><span
              >费用覆盖率 <strong>{{ percent(summary.known_calls, summary.calls) }}</strong></span
            >
          </div>
          <UsageTrend
            v-if="statistics"
            :points="statistics.trend"
            :since="statistics.since"
            :until="statistics.until"
            :units="statistics.summary.costs.map((cost) => cost.unit)"
            :timezone="statistics.timezone"
            @drill="drill"
        /></template>
      </section>
      <section v-else class="work-panel">
        <div class="panel-heading"><h2>平台资源</h2></div>
        <p class="panel-empty">从侧栏选择可访问的模块。用量与费用按当前账户权限开放。</p>
        <RouterLink to="/account">查看账户信息</RouterLink>
      </section>
    </div>
    <section v-if="can('model:manage')" class="work-panel dashboard-channels">
      <div class="panel-heading">
        <h2>渠道账户</h2>
        <RouterLink to="/models?tab=channels"
          >管理渠道 <el-icon><ArrowRight /></el-icon
        ></RouterLink>
      </div>
      <el-alert v-if="channelError" type="error" :closable="false">{{ channelError }}</el-alert>
      <div v-else class="native-table-scroll">
        <table class="compact-table">
          <thead>
            <tr>
              <th>渠道</th>
              <th>服务商</th>
              <th>状态</th>
              <th>本期已知费用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="channel in channels" :key="channel.id">
              <td>
                <strong>{{ channel.name }}</strong
                ><small>{{ channel.id.slice(0, 8) }}</small>
              </td>
              <td>
                {{
                  providers[channelProvider(channel) as keyof typeof providers] ?? channel.provider
                }}
              </td>
              <td>
                <StatusTag :status="channel.is_active ? 'active' : 'disabled'">{{
                  channel.is_active ? '启用' : '停用'
                }}</StatusTag>
              </td>
              <td>
                <template v-if="statistics?.groups.find((item) => item.key === channel.id)"
                  ><span
                    v-for="cost in statistics.groups.find((item) => item.key === channel.id)!.costs"
                    :key="cost.unit"
                    class="currency-line"
                    ><MoneyAmount :amount="cost.amount" :unit="cost.unit" /></span></template
                ><span v-else>{{
                  can('model_invocation:read') ? '本期无可用统计' : '无费用查看权限'
                }}</span>
              </td>
              <td>
                <RouterLink
                  v-if="can('model_invocation:read')"
                  :to="{ path: '/model-usage', query: { channel_id: channel.id } }"
                  >查看用量</RouterLink
                ><RouterLink v-else to="/models?tab=channels">配置渠道</RouterLink>
              </td>
            </tr>
            <tr v-if="!channels.length">
              <td colspan="5">暂无渠道，可到模型接入中配置。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>
