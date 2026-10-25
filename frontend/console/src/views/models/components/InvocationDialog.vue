<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { can } from '../../../stores/session'
import { request } from '../../../api/client'
import { callStates, costCheckStates, money, type Invocation } from '../../../api/models'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'
import { statisticsTime } from '../../usage/time'
const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const row = ref<Invocation | null>(null)
const { busy, error, run } = useAction()
async function load() {
  row.value = null
  row.value = await request<Invocation>(`/model-invocations/${props.id}`)
}
async function cost() {
  await run(async () => {
    await request(`/model-invocations/${props.id}/cost`, 'POST')
    await load()
    emit('changed')
  })
}
onMounted(() => run(load))
</script>
<template>
  <AppDialog drawer title="调用详情" :busy="busy" @close="$emit('close')"
    ><el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-button :disabled="busy" @click="run(load)" native-type="button" type="default"
      >刷新调用</el-button
    >
    <p v-if="busy" role="status">正在处理…</p>
    <template v-if="row"
      ><dl>
        <dt>调用 ID</dt>
        <dd>{{ row.id }}</dd>
        <dt>调用人 / 渠道 / 模型</dt>
        <dd>
          {{ row.user_name ?? row.user_id ?? '系统' }}<br />{{ row.channel_name ?? row.channel_id
          }}<br />{{ row.model_name ?? row.provider_model }} · {{ row.provider_model }}
        </dd>
        <dt>结果</dt>
        <dd>{{ callStates[row.status] }} · {{ row.error_code || '无错误码' }}</dd>
        <dt>登记 / 完成</dt>
        <dd>
          {{ statisticsTime(row.created_at) }} /
          {{ row.finished_at ? statisticsTime(row.finished_at) : '尚未完成' }}
        </dd>
        <dt>上游请求 ID</dt>
        <dd>{{ row.upstream_request_id ?? '未提供' }}</dd>
        <dt>上游报告费用</dt>
        <dd>
          {{ row.cost_amount === null ? '未知' : `${money(row.cost_amount)} ${row.cost_unit}` }} ·
          来源 {{ row.cost_source }}<br />{{ row.cost_note ?? '无补充说明' }}
        </dd>
        <template v-if="row.cost_check_status">
          <dt>费用核对</dt>
          <dd>
            {{ costCheckStates[row.cost_check_status] ?? row.cost_check_status }}
            <span v-if="row.cost_check_reason"> · {{ row.cost_check_reason }}</span>
            <br />最近核对：{{ row.cost_checked_at ? statisticsTime(row.cost_checked_at) : '无' }}
            <span v-if="row.cost_next_check_at">
              <br />下次核对：{{ statisticsTime(row.cost_next_check_at) }}
            </span>
          </dd>
        </template>
      </dl>
      <h3>用量</h3>
      <pre class="audit-details">{{ JSON.stringify(row.usage, null, 2) }}</pre>
      <h3>Flow 关联</h3>
      <pre class="audit-details">{{ JSON.stringify(row.flow_context, null, 2) }}</pre>
      <p>费用未知不代表未计费。补查只查询账务，不重新发送模型请求。</p>
      <el-button
        v-if="can('model:manage') && row.status !== 'running'"
        :disabled="busy"
        @click="cost"
        native-type="button"
        type="primary"
      >
        补查费用
      </el-button>
    </template>
  </AppDialog>
</template>
