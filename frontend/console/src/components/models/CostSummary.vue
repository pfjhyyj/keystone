<script setup lang="ts">
import { callStates, money, type Costs } from '../../api/models'
import { estimateStates } from '../../api/pricing'
defineProps<{ value: Costs }>()
</script>
<template>
  <div class="cost-summary">
    <p>
      <span v-for="(count, state) in value.attempts_by_status" :key="state"
        >{{ callStates[state] ?? state }}：{{ count }} 次　</span
      >
    </p>
    <p v-for="cost in value.known_costs" :key="cost.channel_id + cost.unit">
      渠道 {{ cost.channel_id }}：{{ money(cost.amount) }} {{ cost.unit }}（{{ cost.attempts }}
      次已知费用）
    </p>
    <p v-if="!value.known_costs.length">暂无已知费用，不代表零费用。</p>
    <p>
      {{ value.cost_basis === 'estimated' ? '暂无法预估' : '费用未知' }}：{{
        value.unknown_cost_attempts
      }}
      次。按渠道与单位分别统计，{{
        value.cost_basis === 'estimated' ? '预估不代表上游实扣。' : '已知费用不代表全部实扣。'
      }}
    </p>
    <p v-if="value.estimate_statuses">
      <span v-for="(count, state) in value.estimate_statuses" :key="state"
        >{{ estimateStates[state] ?? state }}：{{ count }} 次　</span
      >
    </p>
  </div>
</template>
