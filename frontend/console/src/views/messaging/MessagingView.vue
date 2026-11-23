<script setup lang="ts">
import { computed, inject, ref } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import { messagingApi, type MessageChannel, type MessageDelivery } from '../../api/messaging'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import ListPager from '../../components/ListPager.vue'
import RowMore from '../../components/RowMore.vue'
import { confirmAction } from '../../utils/confirm'

const statusText: Record<MessageDelivery['status'], string> = {
  pending: '待发送',
  cancelled: '已取消',
  sending: '发送中',
  succeeded: '已送达',
  failed: '失败',
  unknown: '结果未知',
  expired: '已过期',
}
const providerText = { feishu_webhook: '飞书群机器人' }
const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined)
const tabs = computed(() => [
  ...(can('messaging:manage') ? ['channels'] : []),
  ...(can('message_delivery:read') ? ['deliveries'] : []),
])
const localTab = ref('')
const tab = computed({
  get: () => {
    const wanted = String(route?.query.tab ?? localTab.value)
    return tabs.value.includes(wanted) ? wanted : (tabs.value[0] ?? '')
  },
  set: (value: string) => {
    localTab.value = value
    if (router) void router.replace({ query: { ...route?.query, tab: value } })
  },
})
const channelList = usePagedList('messaging:manage', messagingApi.channels)
const deliveryList = usePagedList('message_delivery:read', messagingApi.deliveries)
const { busy, error, notice, run } = useAction()

const channelDialog = ref(false),
  editingChannel = ref<MessageChannel | null>(null),
  name = ref(''),
  key = ref(''),
  active = ref(true),
  replaceCredential = ref(true),
  webhook = ref(''),
  secret = ref('')
function editChannel(row: MessageChannel | null) {
  editingChannel.value = row
  name.value = row?.name ?? ''
  key.value = row?.destination?.key ?? ''
  active.value = row?.is_active ?? true
  replaceCredential.value = !row
  webhook.value = ''
  secret.value = ''
  error.value = ''
  channelDialog.value = true
}
async function saveChannel() {
  await run(async () => {
    const editing = editingChannel.value
    const credential = replaceCredential.value
      ? { url: webhook.value, ...(secret.value ? { secret: secret.value } : {}) }
      : undefined
    if (editing) {
      await request(`/messaging/channels/${editing.id}`, 'PATCH', {
        name: name.value,
        is_active: active.value,
        ...(credential ? { credential } : {}),
      })
      // Channels saved before keys moved onto the channel form get theirs here.
      if (!editing.destination)
        await request('/messaging/destinations', 'POST', {
          key: key.value,
          channel_id: editing.id,
          name: name.value,
        })
    } else
      await request('/messaging/channels', 'POST', {
        name: name.value,
        provider: 'feishu_webhook',
        credential,
        destination_key: key.value,
      })
    webhook.value = secret.value = ''
    channelDialog.value = false
    notice.value = '渠道已保存'
    channelList.search()
  })
}
async function deleteChannel(row: MessageChannel) {
  if (
    !(await confirmAction(
      `删除“${row.name}”？消息标识${row.destination ? `“${row.destination.key}”` : ''}和投递记录会一并删除，待发送的消息不再发送，删除后不可恢复。只想暂停可改为停用。`,
      { title: '删除渠道', confirmText: '确认删除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/channels/${row.id}`, 'DELETE')
    notice.value = '渠道已删除'
    await channelList.reload()
    if (can('message_delivery:read')) deliveryList.search()
  })
}
async function sendTest(row: MessageChannel) {
  await run(async () => {
    await request(`/messaging/destinations/${row.destination!.id}/test`, 'POST')
    notice.value = '测试消息已登记，Worker 将在几秒内发送，可在投递记录中查看结果'
    if (can('message_delivery:read')) deliveryList.search()
  })
}
const time = (value: string | null) => (value ? new Date(value).toLocaleString() : '—')
</script>
<template>
  <section aria-label="消息渠道">
    <div class="heading"><h1>消息渠道</h1></div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert
      v-if="error && !channelDialog"
      role="alert"
      type="error"
      show-icon
      :closable="false"
      >{{ error }}</el-alert
    >
    <el-tabs v-model="tab">
      <el-tab-pane v-if="can('messaging:manage')" label="渠道" name="channels">
        <div class="row-actions">
          <el-button type="primary" :disabled="busy" native-type="button" @click="editChannel(null)"
            >新建渠道</el-button
          ><el-button
            :disabled="busy || channelList.loading.value"
            native-type="button"
            @click="channelList.reload()"
            >刷新</el-button
          >
        </div>
        <el-alert
          v-if="channelList.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ channelList.error.value }}</el-alert
        >
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="channelList.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="渠道" min-width="160"
              ><template #default="{ row }">{{ row.name }}</template></el-table-column
            >
            <el-table-column label="消息标识" min-width="140"
              ><template #default="{ row }">{{
                row.destination?.key ?? '未设置'
              }}</template></el-table-column
            >
            <el-table-column label="类型" min-width="130"
              ><template #default="{ row }">{{
                providerText[row.provider as keyof typeof providerText]
              }}</template></el-table-column
            >
            <el-table-column label="凭据" min-width="100"
              ><template #default="{ row }">{{
                row.has_credential ? '已设置' : '未设置'
              }}</template></el-table-column
            >
            <el-table-column label="状态" min-width="100"
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
                    @click="editChannel(row)"
                    >编辑渠道</el-button
                  ><RowMore :label="`${row.name}的更多操作`"
                    ><el-button
                      :disabled="busy"
                      native-type="button"
                      type="danger"
                      link
                      @click="deleteChannel(row)"
                      >删除渠道</el-button
                    ></RowMore
                  >
                </div></template
              ></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无消息渠道" /></template>
          </el-table>
        </div>
        <ListPager
          :page="channelList.page.value"
          :count="channelList.rows.value.length"
          :loading="busy || channelList.loading.value"
          @change="(value: number) => (channelList.page.value = value)"
        />
      </el-tab-pane>
      <el-tab-pane v-if="can('message_delivery:read')" label="投递记录" name="deliveries">
        <div class="row-actions">
          <el-button
            :disabled="deliveryList.loading.value"
            native-type="button"
            @click="deliveryList.reload()"
            >刷新</el-button
          >
        </div>
        <el-alert
          v-if="deliveryList.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ deliveryList.error.value }}</el-alert
        >
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="deliveryList.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="登记时间" min-width="170"
              ><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column
            >
            <el-table-column label="消息标识" min-width="130"
              ><template #default="{ row }">{{ row.destination_key }}</template></el-table-column
            >
            <el-table-column label="来源" min-width="200"
              ><template #default="{ row }"
                >{{ row.source_module }}.{{ row.source_type
                }}<small>{{ row.source_id }}</small></template
              ></el-table-column
            >
            <el-table-column label="状态" min-width="110"
              ><template #default="{ row }"
                ><StatusTag :status="row.status">{{
                  statusText[row.status as MessageDelivery['status']]
                }}</StatusTag></template
              ></el-table-column
            >
            <el-table-column label="尝试" min-width="70"
              ><template #default="{ row }">{{ row.attempts }}</template></el-table-column
            >
            <el-table-column label="错误" min-width="150"
              ><template #default="{ row }">{{ row.error_code ?? '—' }}</template></el-table-column
            >
            <el-table-column label="送达时间" min-width="170"
              ><template #default="{ row }">{{ time(row.sent_at) }}</template></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无投递记录" /></template>
          </el-table>
        </div>
        <ListPager
          :page="deliveryList.page.value"
          :count="deliveryList.rows.value.length"
          :loading="deliveryList.loading.value"
          @change="(value: number) => (deliveryList.page.value = value)"
        />
      </el-tab-pane>
    </el-tabs>
    <AppDialog
      v-if="channelDialog"
      protect-changes
      :title="editingChannel ? '编辑渠道' : '新建渠道'"
      :busy="busy"
      @close="
        () => {
          channelDialog = false
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="saveChannel">
        <label>渠道名称<el-input v-model="name" required maxlength="128" /></label>
        <label
          >消息标识<el-input
            v-model="key"
            :disabled="!!editingChannel?.destination"
            required
            pattern="[a-z][a-z0-9_.]{0,63}"
            placeholder="ops.alerts"
        /></label>
        <p>业务代码按消息标识发送，换群或换机器人只改渠道配置；标识保存后不可修改。</p>
        <p v-if="!editingChannel">类型：飞书群机器人。只向添加该机器人的群推送，不支持交互。</p>
        <el-checkbox v-if="editingChannel" class="check-option" v-model="active"
          >启用渠道</el-checkbox
        >
        <el-checkbox v-if="editingChannel" class="check-option" v-model="replaceCredential"
          >更换 Webhook 与签名密钥</el-checkbox
        >
        <template v-if="replaceCredential">
          <label
            >Webhook 地址<el-input
              v-model="webhook"
              required
              type="password"
              show-password
              autocomplete="off"
              placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
          /></label>
          <label
            >签名密钥（启用签名校验时填写）<el-input
              v-model="secret"
              type="password"
              show-password
              autocomplete="off"
          /></label>
          <p>凭据加密保存，保存后不再显示。</p>
        </template>
        <el-button :disabled="busy" native-type="submit" type="primary">保存渠道</el-button>
      </form>
    </AppDialog>
  </section>
</template>
