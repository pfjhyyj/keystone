<script setup lang="ts">
import { inject, ref } from 'vue'
import { routerKey } from 'vue-router'
import { request } from '../../api/client'
import { messagingApi, type MessageChannel, type MessageProvider } from '../../api/messaging'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import { confirmAction } from '../../utils/confirm'
import ListPager from '../../components/ListPager.vue'
import RowMore from '../../components/RowMore.vue'
import StatusTag from '../../components/StatusTag.vue'
import ChannelDialog from './components/ChannelDialog.vue'
import ConnectionStatus from './components/ConnectionStatus.vue'
import FeishuAppGuide from './components/FeishuAppGuide.vue'
import { providerText } from './labels'

const router = inject(routerKey, undefined)
const list = usePagedList('messaging.channels', 'messaging:manage', messagingApi.channelsPage)
const { busy, error, notice, run } = useAction()
const dialog = ref(false),
  editing = ref<MessageChannel | null>(null)
const detail = (id: string) => `/messaging/channels/${id}`

function edit(row: MessageChannel | null) {
  editing.value = row
  error.value = ''
  dialog.value = true
}
async function saved(created: { id: string; app: boolean } | null) {
  dialog.value = false
  // A new app has nothing to send to yet: continue on its page to add groups.
  if (created?.app && router) {
    await router.push({ path: detail(created.id), query: { tab: 'groups' } })
    return
  }
  notice.value = '渠道已保存'
  list.search()
}
async function sendTest(row: MessageChannel) {
  await run(async () => {
    await request(`/messaging/destinations/${row.destination!.id}/test`, 'POST')
    notice.value = '测试消息已登记，Worker 将在几秒内发送，可在消息记录里查看结果'
  })
}
async function remove(row: MessageChannel) {
  const owned =
    row.provider === 'feishu_app'
      ? '它的群、成员绑定'
      : `消息标识${row.destination ? `“${row.destination.key}”` : ''}`
  if (
    !(await confirmAction(
      `删除“${row.name}”？${owned}和消息记录会一并删除，待发送的消息不再发送，删除后不可恢复。只想暂停可改为停用。`,
      { title: '删除渠道', confirmText: '确认删除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/channels/${row.id}`, 'DELETE')
    notice.value = '渠道已删除'
    await list.reload()
  })
}
</script>
<template>
  <section aria-label="消息渠道">
    <div class="heading"><h1>消息渠道</h1></div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error && !dialog" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <FeishuAppGuide />
    <div class="row-actions">
      <el-button type="primary" :disabled="busy" native-type="button" @click="edit(null)"
        >新建渠道</el-button
      ><el-button :disabled="busy || list.loading.value" native-type="button" @click="list.reload()"
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
        row-key="id"
        stripe
        class="data-table"
      >
        <el-table-column label="渠道" min-width="180"
          ><template #default="{ row }"
            ><RouterLink v-if="row.provider === 'feishu_app'" :to="detail(row.id)">{{
              row.name
            }}</RouterLink
            ><template v-else>{{ row.name }}</template
            ><small>{{ providerText[row.provider as MessageProvider] }}</small></template
          ></el-table-column
        >
        <el-table-column label="发送到" min-width="160"
          ><template #default="{ row }"
            ><template v-if="row.provider === 'feishu_app'"
              >{{ row.groups ?? 0 }} 个群 · {{ row.members ?? 0 }} 位成员</template
            ><template v-else
              >消息标识 <code>{{ row.destination?.key ?? '未设置' }}</code></template
            ></template
          ></el-table-column
        >
        <el-table-column label="接收点击" min-width="200"
          ><template #default="{ row }"
            ><ConnectionStatus v-if="row.provider === 'feishu_app'" :channel="row" /><span v-else
              >不支持</span
            ></template
          ></el-table-column
        >
        <el-table-column label="状态" min-width="90"
          ><template #default="{ row }"
            ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
              row.is_active ? '启用' : '停用'
            }}</StatusTag></template
          ></el-table-column
        >
        <el-table-column label="操作" min-width="200"
          ><template #default="{ row }"
            ><div class="row-actions">
              <el-button
                v-if="row.provider === 'feishu_app'"
                type="primary"
                link
                native-type="button"
                @click="router?.push(detail(row.id))"
                >管理群和成员</el-button
              ><el-button
                v-else
                type="primary"
                link
                :disabled="busy || !row.is_active || !row.destination?.is_active"
                native-type="button"
                @click="sendTest(row)"
                >发送测试消息</el-button
              ><el-button
                type="primary"
                link
                :disabled="busy"
                native-type="button"
                @click="edit(row)"
                >编辑</el-button
              ><RowMore :label="`${row.name}的更多操作`"
                ><el-button
                  :disabled="busy"
                  native-type="button"
                  type="danger"
                  link
                  @click="remove(row)"
                  >删除渠道</el-button
                ></RowMore
              >
            </div></template
          ></el-table-column
        >
        <template #empty
          ><el-empty
            :image-size="64"
            description="还没有消息渠道。群里的自定义机器人或飞书自建应用都可以作为渠道。"
        /></template>
      </el-table>
    </div>
    <ListPager
      :page="list.page.value"
      :count="list.rows.value.length"
      :loading="busy || list.loading.value"
      :size="list.size.value"
      :total="list.total.value"
      @change="list.page.value = $event"
      @size-change="list.setSize"
    />
    <ChannelDialog v-if="dialog" :channel="editing" @close="dialog = false" @saved="saved" />
  </section>
</template>
