<script setup lang="ts">
// Ether's independent detail sections and request generation, narrowed to Keystone's Run.
import { computed, onMounted, onScopeDispose, ref } from 'vue'
import { can, identity } from '../../../stores/session'
import { ApiError, downloadFile, request } from '../../../api/client'
import { flowApi, flowStates, ongoing, type FlowRun, type Steps } from '../../../api/flows'
import type { Costs } from '../../../api/models'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import CostSummary from '../../../components/models/CostSummary.vue'
const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const row = ref<FlowRun | null>(null),
  steps = ref<Steps | null>(null),
  costs = ref<Costs | null>(null)
const detailError = ref(''),
  stepError = ref(''),
  costError = ref(''),
  loading = ref(false),
  confirming = ref(false)
const { busy, error, notice, run } = useAction()
let generation = 0,
  alive = true,
  timer: ReturnType<typeof setInterval> | undefined
const cancelAllowed = computed(
  () =>
    row.value &&
    ongoing(row.value) &&
    !row.value.cancel_requested_at &&
    can('flow:cancel') &&
    (row.value.actor_id === identity.value?.user.id || can('flow:manage')),
)
async function refresh() {
  if (!alive || loading.value || busy.value || !can('flow:read')) return
  const token = ++generation,
    current = () => alive && token === generation && can('flow:read')
  loading.value = true
  try {
    const value = await flowApi.detail(props.id)
    if (!current()) return
    row.value = value
    detailError.value = ''
    await Promise.all([
      (async () => {
        try {
          const value = await request<Steps>(`/flow-runs/${props.id}/steps`)
          if (current()) {
            steps.value = value
            stepError.value = ''
          }
        } catch (reason) {
          if (current()) {
            steps.value = null
            stepError.value = reason instanceof Error ? reason.message : '执行历史暂不可用'
          }
        }
      })(),
      (async () => {
        if (!can('model_invocation:read')) {
          costs.value = null
          costError.value = '无查看费用权限'
          return
        }
        try {
          const value = await request<Costs>(`/flow-runs/${props.id}/costs`)
          if (current()) {
            costs.value = value
            costError.value = ''
          }
        } catch (reason) {
          if (current()) {
            costs.value = null
            costError.value = reason instanceof Error ? reason.message : '费用暂不可用'
          }
        }
      })(),
    ])
  } catch (reason) {
    if (current()) {
      detailError.value = reason instanceof Error ? reason.message : '运行查询失败'
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
        row.value = null
        steps.value = null
        costs.value = null
        confirming.value = false
        clearInterval(timer)
      }
    }
  } finally {
    if (current()) loading.value = false
  }
}
async function cancel() {
  if (!cancelAllowed.value || loading.value) return
  await run(async () => {
    const value = await request<FlowRun>(`/flow-runs/${props.id}/cancel`, 'POST')
    if (!alive) return
    generation++
    row.value = value
    confirming.value = false
    notice.value = '取消请求已受理，等待执行端确认；已发生的费用和文件写入不会撤回。'
    emit('changed')
  })
}
onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (row.value && ongoing(row.value)) void refresh()
  }, 3000)
})
onScopeDispose(() => {
  alive = false
  generation++
  clearInterval(timer)
})
</script>
<template>
  <AppDialog drawer title="运行详情" :busy="busy" @close="$emit('close')"
    ><el-alert v-if="detailError" role="alert" type="error" show-icon :closable="false"
      >{{ detailError }}{{ row ? '；以下保留最近确认状态，请刷新。' : '' }}</el-alert
    >
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <div class="row-actions">
      <el-button :disabled="busy || loading" @click="refresh" native-type="button" type="default"
        >刷新运行详情</el-button
      ><el-button
        v-if="cancelAllowed"
        :disabled="busy || loading"
        @click="confirming = true"
        native-type="button"
        type="primary"
      >
        取消流程
      </el-button>
    </div>
    <p v-if="loading" role="status">正在确认运行状态…</p>
    <template v-if="row"
      ><dl>
        <dt>流程 / 运行 ID</dt>
        <dd>{{ row.definition_key }}<br />{{ row.id }}</dd>
        <dt>状态</dt>
        <dd>
          <StatusTag :status="row.status">
            {{ row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status] }}
          </StatusTag>
        </dd>
        <dt>发起人</dt>
        <dd>{{ row.actor_id }}</dd>
        <dt>受理 / 完成时间</dt>
        <dd>
          {{ new Date(row.created_at).toLocaleString() }} /
          {{ row.finished_at ? new Date(row.finished_at).toLocaleString() : '未完成' }}
        </dd>
        <dt>最近执行状态确认</dt>
        <dd>
          {{ row.observed_at ? new Date(row.observed_at).toLocaleString() : '尚无执行端确认' }}
        </dd>
      </dl>
      <el-alert v-if="row.error_code" role="alert" type="error" show-icon :closable="false">
        执行错误：{{ row.error_code }}。重新执行须使用新的运行请求。
      </el-alert>
      <details>
        <summary>运行输入</summary>
        <pre class="audit-details">{{ JSON.stringify(row.input, null, 2) }}</pre>
      </details>
      <h3>结果</h3>
      <pre v-if="row.result" class="audit-details">{{ JSON.stringify(row.result, null, 2) }}</pre>
      <p v-else>尚无结果</p>
      <el-button
        v-if="row.result?.report_file_id && can('file:content_read')"
        :disabled="busy"
        @click="run(() => downloadFile(String(row?.result?.report_file_id), 'review-report.txt'))"
        native-type="button"
        type="primary"
      >
        下载报告
      </el-button>
      <h3>步骤记录</h3>
      <el-alert v-if="stepError" role="alert" type="error" show-icon :closable="false"
        >{{ stepError }}；不代表运行失败。</el-alert
      >
      <template v-else-if="steps"
        ><p v-if="!steps.available">尚未形成步骤历史。</p>
        <p v-if="steps.truncated">仅展示部分步骤历史。</p>
        <el-timeline class="flow-timeline">
          <el-timeline-item
            v-for="step in steps.items"
            :key="`${step.workflow_id}:${step.step_id}`"
            :timestamp="
              step.completed_at_epoch_ms
                ? new Date(step.completed_at_epoch_ms).toLocaleString()
                : '尚无完成时间'
            "
            type="primary"
            hollow
          >
            {{ step.name }}
          </el-timeline-item>
        </el-timeline></template
      >
      <h3>模型调用费用 · 最近 30 天</h3>
      <el-alert v-if="costError" role="alert" type="error" show-icon :closable="false">{{
        costError
      }}</el-alert>
      <CostSummary v-if="costs" :value="costs" />
      <form v-if="confirming" class="confirmation" @submit.prevent="cancel">
        <p>确认请求取消此流程？取消是异步请求，已发生的调用、费用和文件写入不会撤回。</p>
        <div class="row-actions">
          <el-button :disabled="busy || loading" native-type="submit" type="danger"
            >确认取消流程</el-button
          ><el-button
            :disabled="busy"
            @click="confirming = false"
            native-type="button"
            type="default"
          >
            继续等待
          </el-button>
        </div>
      </form>
    </template>
  </AppDialog>
</template>
