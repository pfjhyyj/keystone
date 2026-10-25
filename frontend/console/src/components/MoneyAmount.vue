<script setup lang="ts">
import { compactMoney, money } from '../api/models'
defineProps<{ amount: string; unit: string }>()
</script>
<template>
  <span class="money-amount" :title="`${money(amount)} ${unit}`">
    {{ compactMoney(amount) }} {{ unit }}
  </span>
</template>
<style scoped>
.money-amount {
  display: inline-block;
  min-width: 0;
  max-width: 100%;
  vertical-align: bottom;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
