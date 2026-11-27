<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request } from '../../../api/client'
import type { Channel, CloudBillReport } from '../../../api/models'
import AppSelect from '../../../components/AppSelect.vue'
import ListPager from '../../../components/ListPager.vue'
import MoneyAmount from '../../../components/MoneyAmount.vue'
import { usePageSize } from '../../../composables/usePagedList'
import { shanghaiInput, statisticsTime } from '../../usage/time'

const props = defineProps<{ channel: Channel }>()
const MAX_BILL_OFFSET = 10_000
const billSizes = [20, 50, 100] as const
const { size, setSize } = usePageSize('cloud-bills', billSizes)
const period = ref(shanghaiInput(new Date().toISOString()).slice(0, 7))
const report = ref<CloudBillReport | null>(null),
  busy = ref(false),
  error = ref('')
const total = computed(() => {
  const value = report.value?.total
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
})
const page = computed(() =>
  report.value ? Math.floor(report.value.offset / report.value.limit) : 0,
)
const maxPage = computed(() => Math.floor(MAX_BILL_OFFSET / size.value))
const totalBeyondLimit = computed(
  () => total.value !== null && Math.ceil(total.value / size.value) - 1 > maxPage.value,
)
// The shared pager can jump to any page in a known total; bills have a lower offset ceiling.
const numbered = computed(() => total.value !== null && !totalBeyondLimit.value)
const canNext = computed(() => {
  if (!report.value || page.value >= maxPage.value) return false
  return total.value === null ? report.value.has_more : (page.value + 1) * size.value < total.value
})
let controller: AbortController | null = null
function reset() {
  controller?.abort()
  report.value = null
  error.value = ''
  busy.value = false
}
watch([period, () => props.channel.id, () => props.channel.has_billing_api_key], reset, {
  flush: 'sync',
})
onBeforeUnmount(() => controller?.abort())
function changeSize(value: number) {
  const previous = size.value
  if (value === previous) return
  setSize(value)
  if (size.value !== previous && (report.value || busy.value)) void load()
}
async function load(target = 0) {
  if (
    !props.channel.has_billing_api_key ||
    !Number.isInteger(target) ||
    target < 0 ||
    target > maxPage.value
  )
    return
  controller?.abort()
  const pending = new AbortController()
  controller = pending
  report.value = null
  busy.value = true
  error.value = ''
  try {
    const query = new URLSearchParams({
      period: period.value,
      offset: String(target * size.value),
      limit: String(size.value),
    })
    const result = await request<CloudBillReport>(
      `/model-channels/${props.channel.id}/bills?${query}`,
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
      <ListPager
        v-if="numbered"
        :page="page"
        :count="report.items.length"
        :loading="busy"
        :size="size"
        :total="total"
        :size-options="billSizes"
        @change="load"
        @size-change="changeSize"
      />
      <template v-else>
        <p v-if="total === null">上游未提供账单总条数。</p>
        <p v-else>共 {{ total }} 条账单；最多可查询至第 {{ maxPage + 1 }} 页。</p>
        <p v-if="total === null && report.has_more && page === maxPage">已达到可查询的分页上限。</p>
      </template>
    </template>
    <div v-if="!numbered && channel.has_billing_api_key" class="bill-pagination">
      <template v-if="report">
        <el-button :disabled="busy || page === 0" @click="load(page - 1)">上一页账单</el-button>
        <span>第 {{ page + 1 }} 页 · 本页 {{ report.items.length }} 条</span>
        <el-button :disabled="busy || !canNext" @click="load(page + 1)">下一页账单</el-button>
      </template>
      <AppSelect
        class="bill-size"
        aria-label="每页账单条数"
        :model-value="String(size)"
        :disabled="busy"
        @update:model-value="changeSize(Number($event))"
      >
        <el-option
          v-for="option in billSizes"
          :key="option"
          :value="String(option)"
          :label="`${option} 条/页`"
        />
      </AppSelect>
    </div>
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
.bill-size {
  width: 120px;
}
</style>
