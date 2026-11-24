<script setup lang="ts">
import { environmentsApi, screenRoute, type Environment } from '../../api/environments'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import { can } from '../../stores/session'
import { confirmAction } from '../../utils/confirm'
import ListPager from '../../components/ListPager.vue'
import StatusTag from '../../components/StatusTag.vue'
import { backupText, canBackup, stateTag, stateText, unsettled } from './labels'

const list = usePagedList('environment:use', environmentsApi.list)
const { busy, error, notice, run } = useAction()

async function check(row: Environment) {
  await run(async () => {
    const session = await environmentsApi.check(row.id!, row.session!.id)
    notice.value =
      session.state === 'review'
        ? '暂时无法确认原会话，请稍后重新检查或联系部署管理员'
        : `会话${stateText[session.state]}`
    await list.reload()
  })
}
async function stop(row: Environment) {
  if (
    !(await confirmAction(`结束“${row.name}”？会关闭远程窗口，环境中的登录和数据会保留。`, {
      title: '结束会话',
      confirmText: '确认结束',
      danger: true,
    }))
  )
    return
  await run(async () => {
    const session = await environmentsApi.stop(row.id!, row.session!.id)
    notice.value = session.state === 'stopped' ? '会话已结束' : `会话${stateText[session.state]}`
    await list.reload()
  })
}
async function backup(row: Environment) {
  await run(async () => {
    try {
      await environmentsApi.backup(row.id!)
      notice.value = '备份已完成'
    } finally {
      await list.reload()
    }
  })
}
</script>
<template>
  <section aria-label="我的环境">
    <div class="heading">
      <h1>我的环境</h1>
      <p>在远程画面里登录、扫码或处理验证，业务任务会在同一环境中继续。</p>
    </div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <div class="row-actions">
      <el-button :disabled="busy || list.loading.value" native-type="button" @click="list.reload()"
        >刷新</el-button
      >
    </div>
    <el-alert v-if="list.error.value" role="alert" type="error" show-icon :closable="false">{{
      list.error.value
    }}</el-alert>
    <div class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        table-layout="auto"
        :data="list.rows.value"
        :row-key="(row: Environment) => `${row.kind}:${row.key}`"
        stripe
        class="data-table"
      >
        <el-table-column label="环境" min-width="200"
          ><template #default="{ row }"
            ><RouterLink :to="screenRoute(row.kind, row.key)">{{ row.name }}</RouterLink
            ><small
              ><code>{{ row.key }}</code></small
            ></template
          ></el-table-column
        >
        <el-table-column label="会话" min-width="110"
          ><template #default="{ row }"
            ><StatusTag
              v-if="row.session"
              :status="stateTag[row.session.state as keyof typeof stateTag]"
              >{{ stateText[row.session.state as keyof typeof stateText] }}</StatusTag
            ><span v-else>未启动</span></template
          ></el-table-column
        >
        <el-table-column label="最近备份" min-width="170"
          ><template #default="{ row }">{{
            row.backup_enabled ? backupText(row) : '未启用'
          }}</template></el-table-column
        >
        <el-table-column label="操作" min-width="240"
          ><template #default="{ row }"
            ><div class="row-actions">
              <RouterLink
                :to="screenRoute(row.kind, row.key)"
                class="el-button el-button--primary is-link"
                >打开画面</RouterLink
              ><el-button
                v-if="row.session && unsettled(row.session.state)"
                type="primary"
                link
                :disabled="busy"
                native-type="button"
                @click="check(row)"
                >重新检查</el-button
              ><el-button
                v-if="row.session?.can_stop && can('environment:stop')"
                type="danger"
                link
                :disabled="busy"
                native-type="button"
                @click="stop(row)"
                >结束</el-button
              ><el-button
                v-if="canBackup(row) && can('environment:stop')"
                type="primary"
                link
                :disabled="busy"
                native-type="button"
                @click="backup(row)"
                >备份</el-button
              >
            </div></template
          ></el-table-column
        >
        <template #empty
          ><el-empty
            :image-size="64"
            description="没有可用的环境。交互环境未配置时，请联系部署管理员。"
        /></template>
      </el-table>
    </div>
    <ListPager
      :page="list.page.value"
      :count="list.rows.value.length"
      :loading="busy || list.loading.value"
      @change="(value: number) => (list.page.value = value)"
    />
  </section>
</template>
