<script setup lang="ts">
import { reactive, ref } from 'vue'
import { adminApi, type AuditLog } from '../admin-api'
import { usePagedList } from '../composables/usePagedList'
import AppDialog from '../components/AppDialog.vue'
import ListPager from '../components/ListPager.vue'
const filters = reactive({
  action: '',
  actor_type: '',
  actor_id: '',
  target_type: '',
  target_id: '',
  outcome: '',
  since: '',
  until: '',
})
const applied = ref<Record<string, string>>({}),
  validation = ref(''),
  selected = ref<AuditLog | null>(null)
const { rows, page, loading, error, reload, search } = usePagedList('audit:read', (offset) =>
  adminApi.audit(offset, applied.value),
)
const actors: Record<string, string> = { user: '用户', system: '系统', anonymous: '匿名' }
const outcomes: Record<string, string> = { success: '成功', failure: '失败', denied: '拒绝' }
function apply() {
  validation.value = ''
  if (
    filters.actor_id.trim() &&
    !/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/i.test(filters.actor_id.trim())
  ) {
    validation.value = '操作者 ID 须为完整 UUID。'
    return
  }
  const until = filters.until ? new Date(filters.until).getTime() : Date.now()
  const since = filters.since ? new Date(filters.since).getTime() : until - 30 * 86400000
  if (
    !Number.isFinite(since) ||
    !Number.isFinite(until) ||
    since > until ||
    until - since > 366 * 86400000
  ) {
    validation.value = '时间范围须有效、按先后排列且不超过 366 天。'
    return
  }
  applied.value = Object.fromEntries(
    Object.entries(filters)
      .filter(([, value]) => value.trim())
      .map(([key, value]) => [
        key,
        ['since', 'until'].includes(key) ? new Date(value).toISOString() : value.trim(),
      ]),
  )
  selected.value = null
  search()
}
function reset() {
  Object.assign(filters, {
    action: '',
    actor_type: '',
    actor_id: '',
    target_type: '',
    target_id: '',
    outcome: '',
    since: '',
    until: '',
  })
  apply()
}
</script>
<template>
  <section aria-label="操作审计">
    <div class="heading">
      <div>
        <h1>操作审计</h1>
        <p>默认最近 30 天，单次最多查询 366 天。日志只读，目标 ID 可以是字符串。</p>
      </div>
    </div>
    <form class="module-form" @submit.prevent="apply">
      <label
        >操作代码<el-input
          v-model="filters.action"
          maxlength="100"
          placeholder="例如 file.delete.request"
      /></label>
      <div class="form-field">
        <span class="form-label">操作者类型</span
        ><AppSelect v-model="filters.actor_type" aria-label="操作者类型"
          ><el-option value="" :label="'全部'" /><el-option
            v-for="(label, key) in actors"
            :key="key"
            :value="key"
            :label="label"
        /></AppSelect>
      </div>
      <label>操作者 ID<el-input v-model="filters.actor_id" maxlength="36" /></label>
      <label
        >目标类型<el-input
          v-model="filters.target_type"
          maxlength="100"
          placeholder="例如 file" /></label
      ><label>目标 ID<el-input v-model="filters.target_id" maxlength="128" /></label>
      <div class="form-field">
        <span class="form-label">操作结果</span
        ><AppSelect v-model="filters.outcome" aria-label="操作结果"
          ><el-option value="" :label="'全部'" /><el-option
            v-for="(label, key) in outcomes"
            :key="key"
            :value="key"
            :label="label"
        /></AppSelect>
      </div>
      <label>开始时间<el-input v-model="filters.since" type="datetime-local" /></label
      ><label>结束时间<el-input v-model="filters.until" type="datetime-local" /></label>
      <el-button :disabled="loading" native-type="submit" type="default">查询审计</el-button
      ><el-button :disabled="loading" @click="reset" native-type="button" type="default"
        >重置</el-button
      ><el-button :disabled="loading" @click="reload" native-type="button" type="default"
        >刷新</el-button
      >
    </form>
    <el-alert v-if="validation || error" role="alert" type="error" show-icon :closable="false">{{
      validation || error
    }}</el-alert>
    <p v-if="loading" role="status">正在加载审计…</p>
    <div v-else class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        table-layout="auto"
        :data="rows"
        row-key="id"
        stripe
        class="data-table"
      >
        <el-table-column label="时间" min-width="180"
          ><template #default="{ row: row }">{{
            new Date(row.occurred_at).toLocaleString()
          }}</template></el-table-column
        >
        <el-table-column label="操作者" min-width="180"
          ><template #default="{ row: row }"
            >{{ actors[row.actor_type] }}
            <p v-if="row.actor_id">{{ row.actor_id }}</p></template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="220"
          ><template #default="{ row: row }"
            ><code>{{ row.action }}</code></template
          ></el-table-column
        >
        <el-table-column label="目标" min-width="180"
          ><template #default="{ row: row }"
            >{{ row.target_type ?? '—' }}
            <p>{{ row.target_id ?? '—' }}</p></template
          ></el-table-column
        >
        <el-table-column label="结果" min-width="120"
          ><template #default="{ row: row }">{{ outcomes[row.outcome] }}</template></el-table-column
        >
        <el-table-column label="详情" min-width="180"
          ><template #default="{ row: row }"
            ><el-button @click="selected = row" native-type="button" type="primary" link
              >查看详情</el-button
            ></template
          ></el-table-column
        >
        <template #empty
          ><el-empty :image-size="64" description="没有符合条件的审计记录。" /></template
      ></el-table>
    </div>
    <ListPager :page="page" :count="rows.length" :loading="loading" @change="page = $event" />
    <AppDialog v-if="selected" title="审计详情" @close="selected = null"
      ><dl>
        <dt>记录 ID</dt>
        <dd>{{ selected.id }}</dd>
        <dt>关联 ID</dt>
        <dd>{{ selected.correlation_id ?? '无' }}</dd>
        <dt>操作</dt>
        <dd>{{ selected.action }}</dd>
      </dl>
      <pre class="audit-details">{{ JSON.stringify(selected.details, null, 2) }}</pre>
    </AppDialog>
  </section>
</template>
