<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { request } from '../../../api/client'
import type { Channel, CloudBillReport } from '../../../api/models'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import { shanghaiInput, statisticsTime } from '../../usage/time'

const props = defineProps<{ channel: Channel }>()
const period = ref(shanghaiInput(new Date().toISOString()).slice(0, 7))
const report = ref<CloudBillReport | null>(null),
  busy = ref(false),
  error = ref('')
let controller: AbortController | null = null
function reset() {
  controller?.abort()
  report.value = null
  error.value = ''
  busy.value = false
}
watch([period, () => props.channel.id], reset)
onBeforeUnmount(() => controller?.abort())
async function load(offset = 0) {
  controller?.abort()
  const pending = new AbortController()
  controller = pending
  report.value = null
  busy.value = true
  error.value = ''
  try {
    const result = await request<CloudBillReport>(
      `/model-channels/${props.channel.id}/bills?${new URLSearchParams({ period: period.value, offset: String(offset) })}`,
      'GET',
      undefined,
      true,
      pending.signal,
    )
    if (!pending.signal.aborted) report.value = result
  } catch (reason) {
    if (!pending.signal.aborted)
      error.value = reason instanceof Error ? reason.message : '账单查询失败'
  } finally {
    if (!pending.signal.aborted) busy.value = false
  }
}
</script>
<template>
  <section aria-label="云账户月账单">
    <h3>云账户月账单</h3>
    <p>
      包含该云账户下其他产品和渠道的费用。月账单存在出账延迟，当月数据可能尚未完整；以下金额不分摊到单次模型调用。
    </p>
    <p v-if="!channel.has_billing_api_key">请先在编辑渠道中配置云账务 AccessKey。</p>
    <form @submit.prevent="load()">
      <label
        >账期<el-input
          v-model="period"
          type="month"
          aria-label="账单月份"
          required
          :disabled="busy"
      /></label>
      <el-button native-type="submit" :disabled="busy || !channel.has_billing_api_key">{{
        busy ? '查询中…' : '查询月账单'
      }}</el-button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p>{{ report.period }} · 采集于 {{ statisticsTime(report.fetched_at) }}</p>
      <p v-if="!report.items.length">本页没有账单记录，不代表没有消费。</p>
      <article v-for="(item, index) in report.items" :key="index">
        <h4>{{ item.product }} · {{ item.product_code }}</h4>
        <p>{{ item.category }}</p>
        <dl>
          <template v-for="(value, label) in item.amounts" :key="label">
            <dt>{{ label }}</dt>
            <dd><MoneyAmount :amount="value" :unit="item.unit" /></dd>
          </template>
        </dl>
      </article>
      <div class="bill-pagination">
        <el-button
          :disabled="busy || report.offset === 0"
          @click="load(Math.max(0, report.offset - report.limit))"
          >上一页账单</el-button
        >
        <span>第 {{ Math.floor(report.offset / report.limit) + 1 }} 页</span>
        <el-button :disabled="busy || !report.has_more" @click="load(report.offset + report.limit)"
          >下一页账单</el-button
        >
      </div>
    </template>
  </section>
</template>
<style scoped>
section {
  margin-top: 24px;
}
form {
  display: flex;
  gap: 12px;
  align-items: end;
  flex-wrap: wrap;
}
label {
  display: grid;
  gap: 6px;
}
article {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
h4 {
  margin: 0 0 8px;
  overflow-wrap: anywhere;
}
p {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
dl {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px 12px;
  font-size: 13px;
}
dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.bill-pagination {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
</style>
