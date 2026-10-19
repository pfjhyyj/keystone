<script setup lang="ts">
import { onMounted, onScopeDispose, ref } from 'vue'
import { can } from './access'
import { flowApi, flowStates, ongoing, type FlowDefinition, type FlowRun } from './flow-api'
import { usePagedList } from './composables/usePagedList'
import FlowStartDialog from './components/FlowStartDialog.vue'
import FlowRunDialog from './components/FlowRunDialog.vue'
import ListPager from './components/ListPager.vue'
const status = ref(''), applied = ref(''), starting = ref(false), selected = ref(''), notice = ref('')
const enabled = ref(false), definitions = ref<FlowDefinition[]>([]), idempotencyDays = ref(7), catalogError = ref(''), catalogLoading = ref(false)
const { rows, page, loading, error, refresh, reload, search } = usePagedList('flow:read', offset => flowApi.runs(offset, applied.value))
let alive = true, timer: ReturnType<typeof setInterval> | undefined
async function catalog() { if (!can('flow:start') || catalogLoading.value) return; catalogLoading.value = true; catalogError.value = ''; try { const result = await flowApi.definitions(); if (alive) { enabled.value = result.enabled; definitions.value = result.items; idempotencyDays.value = result.idempotency_days } } catch (reason) { if (alive) { enabled.value = false; definitions.value = []; catalogError.value = reason instanceof Error ? reason.message : '目录不可用' } } finally { if (alive) catalogLoading.value = false } }
function accepted(run: FlowRun) { starting.value = false; notice.value = `已受理：${run.id}`; search(); if (can('flow:read')) selected.value = run.id }
onMounted(() => { void catalog(); timer = setInterval(() => { if (!starting.value && rows.value.some(ongoing)) void refresh() }, 3000) })
onScopeDispose(() => { alive = false; clearInterval(timer) })
</script>
<template>
  <section aria-label="Flow 流程"><div class="heading"><div><p class="eyebrow">PLATFORM / FLOW</p><h1>Flow 流程</h1><p>发起任务，查看执行过程与结果。调用方式由部署配置决定，真实模型可能产生费用。</p></div></div><p v-if="notice" role="status">{{ notice }}</p>
    <section v-if="can('flow:start')" aria-label="流程目录"><div class="heading"><h2>流程目录</h2><div class="row-actions"><button :disabled="!enabled || !definitions.length || catalogLoading" @click="starting = true">发起流程</button><button class="secondary" :disabled="catalogLoading" @click="catalog">刷新流程目录</button></div></div><p v-if="catalogError" role="alert">{{ catalogError }}</p><p v-else-if="!enabled">Flow 尚未启用；完成执行服务配置后刷新目录。</p><p v-else>Flow 已启用，此配置不表示 Worker 已在线。等待执行时可检查 Worker。</p><ul><li v-for="definition in definitions" :key="definition.key">{{ definition.name }} · {{ definition.description }}</li></ul><p v-if="enabled && !definitions.length">没有符合当前权限的流程定义。</p></section>
    <section v-if="can('flow:read')" aria-label="运行记录"><h2>运行记录</h2><p>{{ can('flow:manage') ? '可见全部运行' : '仅显示本人运行' }}。未完成的可见运行每 3 秒刷新，离开页面后停止。</p><form class="module-form" @submit.prevent="applied = status; search()"><label>运行状态<select v-model="status" aria-label="运行状态"><option value="">全部</option><option v-for="(label, key) in flowStates" :key="key" :value="key">{{ label }}</option></select></label><button :disabled="loading">查询流程</button><button type="button" class="secondary" :disabled="loading" @click="reload">刷新流程</button></form><p v-if="error" role="alert">{{ error }}；自动刷新时可能保留上次确认的数据。</p><p v-if="loading" role="status">正在加载运行…</p>
      <div class="table-scroll"><table><thead><tr><th>流程</th><th>受理时间</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td>{{ definitions.find(d => d.key === row.definition_key)?.name ?? row.definition_key }}<small class="cell-description">{{ row.id }}</small></td><td>{{ new Date(row.created_at).toLocaleString() }}</td><td>{{ row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status] }}</td><td><button @click="selected = row.id">查看运行</button></td></tr><tr v-if="!loading && !error && !rows.length"><td colspan="4">暂无符合条件的运行</td></tr></tbody></table></div><ListPager :page="page" :count="rows.length" :loading="loading" @change="page = $event" />
    </section>
    <FlowStartDialog v-if="starting || (can('flow:start') && definitions.length)" :open="starting" :definitions="definitions" :enabled="enabled" :idempotency-days="idempotencyDays" @close="starting = false" @accepted="accepted" />
    <FlowRunDialog v-if="selected" :key="selected" :id="selected" @close="selected = ''" @changed="reload" />
  </section>
</template>
