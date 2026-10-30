<script setup lang="ts">
import { computed, ref } from 'vue'
import { balanceStates, money, type Channel } from '../../../api/models'
import { statisticsTime } from '../../usage/time'
import AppDialog from '../../../components/AppDialog.vue'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import CloudBills from './CloudBills.vue'
const props = defineProps<{
  channel: Channel
  available: boolean
  billsAvailable?: boolean
  busy: boolean
  error: string
}>()
const emit = defineEmits<{ refresh: []; inspect: [] }>()
const opened = ref(false)
const snapshot = computed(() => props.channel.balance_snapshot)
const remaining = computed(() =>
  snapshot.value?.items.find((item) => item.kind === 'remaining' || item.kind === 'available'),
)
const labels: Record<string, string> = {
  remaining: '剩余',
  used: '已用',
  granted: '总额',
  available: '可用额度',
  cash: '现金余额',
  arrears: '欠费金额',
  credit: '信控额度',
  frozen: '冻结金额',
}
function inspect() {
  emit('inspect')
  opened.value = true
}
</script>
<template>
  <div class="balance-preview">
    <strong v-if="remaining" class="balance-value"
      ><MoneyAmount :amount="remaining.amount" :unit="remaining.unit"
    /></strong>
    <strong v-else-if="snapshot?.unlimited" class="balance-value">Key 不限额</strong>
    <strong v-else class="balance-unknown">{{
      available ? '尚无余额数据' : '暂不支持查询'
    }}</strong>
    <span v-if="snapshot" class="balance-caption"
      >{{ snapshot.scope === 'token_quota' ? '仅 Key 额度' : '账户余额'
      }}<template v-if="snapshot.unlimited"> · 当前 Token 不限额</template></span
    >
    <span v-if="snapshot && channel.balance_check_status !== 'ok'" class="balance-stale"
      >上次成功结果 · {{ balanceStates[channel.balance_check_status] }}</span
    >
    <span v-else-if="channel.balance_checked_at" class="balance-date"
      >{{ balanceStates[channel.balance_check_status] }} ·
      {{ statisticsTime(channel.balance_checked_at) }}</span
    >
    <span v-else class="balance-date">{{ balanceStates[channel.balance_check_status] }}</span>
    <div class="balance-actions">
      <el-button link type="primary" native-type="button" :disabled="busy" @click="inspect"
        >额度详情</el-button
      ><el-button link native-type="button" :disabled="busy || !available" @click="emit('refresh')"
        >刷新余额</el-button
      >
    </div>
    <AppDialog
      v-if="opened"
      drawer
      append-to-body
      :title="`${channel.name} · 余额与额度`"
      :busy="busy"
      @close="opened = false"
    >
      <el-alert v-if="error" role="alert" type="error" :closable="false">{{ error }}</el-alert>
      <p v-if="!available">当前渠道尚未接入账户余额查询，请在服务商控制台查看。</p>
      <p v-if="!snapshot">未知，不代表零余额。</p>
      <p v-if="snapshot && channel.balance_check_status !== 'ok'" class="balance-stale">
        这是上次成功结果，不代表当前余额。
      </p>
      <template v-if="snapshot">
        <h3>{{ snapshot.scope === 'token_quota' ? '当前 Key 的 Token 额度' : '账户余额' }}</h3>
        <p v-if="snapshot.scope === 'token_quota'">
          上游未返回账户余额，以下仅为当前 Key 的 Token 额度，不是账户总余额。
        </p>
        <div class="balance-facts">
          <div v-for="item in snapshot.items" :key="item.kind + item.unit">
            <span>{{ labels[item.kind] ?? item.kind }}</span
            ><strong>{{ money(item.amount) }} {{ item.unit }}</strong>
          </div>
        </div>
        <h3 v-if="snapshot.token_items">当前 Key 的 Token 额度</h3>
        <p v-if="snapshot.unlimited">当前 Token 不限额，调用仍受账户余额限制。</p>
        <div v-if="snapshot.token_items" class="balance-facts">
          <div v-for="item in snapshot.token_items" :key="item.kind + item.unit">
            <span>{{ labels[item.kind] ?? item.kind }}</span
            ><strong>{{ money(item.amount) }} {{ item.unit }}</strong>
          </div>
        </div>
      </template>
      <p v-if="snapshot && ['aliyun_account', 'volcengine_account'].includes(snapshot.scope)">
        这是云账户余额，含其他云产品共享资金；可用额度和现金余额可能重叠，不能相加。
      </p>
      <h3>查询记录</h3>
      <dl class="balance-metadata">
        <dt>查询状态</dt>
        <dd>{{ balanceStates[channel.balance_check_status] }}</dd>
        <dt>最近尝试</dt>
        <dd>
          {{
            channel.balance_last_attempt_at ? statisticsTime(channel.balance_last_attempt_at) : '无'
          }}
        </dd>
        <dt>成功采集</dt>
        <dd>
          {{ channel.balance_checked_at ? statisticsTime(channel.balance_checked_at) : '无' }}
        </dd>
        <dt>渠道 ID</dt>
        <dd>{{ channel.id }}</dd>
      </dl>
      <el-button
        type="primary"
        native-type="button"
        :disabled="busy || !available"
        @click="emit('refresh')"
        >刷新余额</el-button
      >
      <CloudBills v-if="billsAvailable" :channel="channel" />
    </AppDialog>
  </div>
</template>
<style scoped>
h3 {
  margin: 24px 0 12px;
  font-size: 14px;
}
p {
  font-size: 13px;
  line-height: 1.6;
}
.balance-preview {
  display: grid;
  gap: 4px;
  line-height: 1.5;
}
.balance-value {
  color: var(--el-text-color-primary);
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.balance-caption,
.balance-date {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.balance-unknown {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  font-weight: 400;
}
.balance-stale {
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
}
.balance-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.balance-actions .el-button + .el-button {
  margin-left: 0;
}
.balance-facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 14px 0 24px;
}
.balance-facts > div {
  display: grid;
  gap: 6px;
  padding: 14px;
  background: var(--el-fill-color-light);
  border-radius: 8px;
}
.balance-facts span {
  color: var(--el-text-color-secondary);
}
.balance-facts strong {
  font-size: 18px;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.balance-metadata {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 12px;
  margin: 16px 0 24px;
}
.balance-metadata dt {
  color: var(--el-text-color-secondary);
}
.balance-metadata dd {
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
