<script setup lang="ts">
import { onMounted, onScopeDispose, ref } from 'vue'
import { can } from '../../stores/session'
import { flowApi, flowStates, ongoing } from '../../api/flows'
import { usePagedList } from '../../composables/usePagedList'
import FlowRunDialog from './components/FlowRunDialog.vue'
import ListPager from '../../components/ListPager.vue'
const status = ref(''),
  applied = ref(''),
  selected = ref('')
const { rows, page, loading, error, refresh, reload, search } = usePagedList(
  'flow:read',
  (offset) => flowApi.runs(offset, applied.value),
)
let timer: ReturnType<typeof setInterval> | undefined
function filter(value = status.value) {
  status.value = value
  applied.value = value
  search()
}
onMounted(() => {
  timer = setInterval(() => {
    if (rows.value.some(ongoing)) void refresh()
  }, 3000)
})
onScopeDispose(() => clearInterval(timer))
</script>
<template>
  <section aria-label="Flow 运行">
    <div class="heading">
      <div>
        <h1>Flow 运行</h1>
        <p>查看业务发起的运行、步骤与结果。</p>
      </div>
    </div>
    <section v-if="can('flow:read')" aria-label="运行记录">
      <p>
        {{ can('flow:manage') ? '可见全部运行' : '仅显示本人运行' }}。未完成的可见运行每 3
        秒刷新，离开页面后停止。
      </p>
      <form class="module-form" @submit.prevent="filter()">
        <div class="form-field">
          <span class="form-label">运行状态</span
          ><AppSelect v-model="status" aria-label="运行状态" placeholder="全部状态"
            ><el-option value="" :label="'全部'" /><el-option
              v-for="(label, key) in flowStates"
              :key="key"
              :value="key"
              :label="label"
          /></AppSelect>
        </div>
        <el-button :disabled="loading" native-type="submit" type="default">查询流程</el-button
        ><el-button :disabled="loading" @click="filter('')" native-type="button" type="default"
          >重置筛选</el-button
        ><el-button :disabled="loading" @click="reload" native-type="button" type="default">
          刷新流程
        </el-button>
      </form>
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false"
        >{{ error }}；自动刷新时可能保留上次确认的数据。</el-alert
      >
      <p v-if="loading" role="status">正在加载运行…</p>
      <div v-if="!error || rows.length" class="table-scroll">
        <el-table
          :fit="false"
          native-scrollbar
          table-layout="auto"
          :data="rows"
          row-key="id"
          stripe
          class="data-table"
        >
          <el-table-column label="流程" min-width="180"
            ><template #default="{ row: row }"
              >{{ row.definition_key
              }}<small :title="row.id">{{ row.id.slice(0, 8) }}…</small></template
            ></el-table-column
          >
          <el-table-column label="受理时间" min-width="180"
            ><template #default="{ row: row }">{{
              new Date(row.created_at).toLocaleString()
            }}</template></el-table-column
          >
          <el-table-column label="状态" min-width="120"
            ><template #default="{ row: row }"
              ><StatusTag :status="row.status">{{
                row.cancel_requested_at && ongoing(row) ? '取消处理中' : flowStates[row.status]
              }}</StatusTag></template
            ></el-table-column
          >
          <el-table-column label="操作" min-width="220"
            ><template #default="{ row: row }"
              ><el-button @click="selected = row.id" native-type="button" type="primary" link
                >查看运行</el-button
              ></template
            ></el-table-column
          >
          <template #empty><el-empty :image-size="64" description="暂无符合条件的运行" /></template
        ></el-table>
      </div>
      <ListPager :page="page" :count="rows.length" :loading="loading" @change="page = $event" />
    </section>
    <FlowRunDialog
      v-if="selected"
      :key="selected"
      :id="selected"
      @close="selected = ''"
      @changed="reload"
    />
  </section>
</template>
