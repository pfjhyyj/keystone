<script setup lang="ts">
import { computed, inject, ref } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { ElRadioButton, ElRadioGroup } from 'element-plus'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import {
  messagingApi,
  type MessageChannel,
  type MessageChat,
  type MessageDelivery,
  type MessageDestination,
  type MessageProvider,
} from '../../api/messaging'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import AppSelect from '../../components/AppSelect.vue'
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
const providerText: Record<MessageProvider, string> = {
  feishu_webhook: '飞书群机器人',
  feishu_app: '飞书自建应用',
}
const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined)
const channelList = usePagedList('messaging:manage', messagingApi.channels)
const targetList = usePagedList('messaging:manage', messagingApi.appDestinations)
const deliveryList = usePagedList('message_delivery:read', messagingApi.deliveries)
const { busy, error, notice, run } = useAction()
const appChannels = computed(() =>
  channelList.rows.value.filter((row) => row.provider === 'feishu_app'),
)
const tabs = computed(() => [
  ...(can('messaging:manage') ? ['channels'] : []),
  ...(can('messaging:manage') && (appChannels.value.length || targetList.rows.value.length)
    ? ['targets']
    : []),
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
const channelName = (id: string) =>
  channelList.rows.value.find((row) => row.id === id)?.name ?? id.slice(0, 8)
const dialogOpen = computed(() => channelDialog.value || targetDialog.value)

const channelDialog = ref(false),
  editingChannel = ref<MessageChannel | null>(null),
  provider = ref<MessageProvider>('feishu_webhook'),
  name = ref(''),
  key = ref(''),
  active = ref(true),
  replaceCredential = ref(true),
  webhook = ref(''),
  secret = ref(''),
  appId = ref(''),
  appSecret = ref('')
const isApp = computed(() => provider.value === 'feishu_app')
function editChannel(row: MessageChannel | null) {
  editingChannel.value = row
  provider.value = row?.provider ?? 'feishu_webhook'
  name.value = row?.name ?? ''
  key.value = row?.destination?.key ?? ''
  active.value = row?.is_active ?? true
  replaceCredential.value = !row
  webhook.value = secret.value = appSecret.value = ''
  appId.value = row?.app_id ?? ''
  error.value = ''
  channelDialog.value = true
}
async function saveChannel() {
  await run(async () => {
    const editing = editingChannel.value
    const credential = !replaceCredential.value
      ? undefined
      : isApp.value
        ? { app_secret: appSecret.value }
        : { url: webhook.value, ...(secret.value ? { secret: secret.value } : {}) }
    if (editing) {
      await request(`/messaging/channels/${editing.id}`, 'PATCH', {
        name: name.value,
        is_active: active.value,
        ...(credential ? { credential } : {}),
      })
      // Group bots saved before keys moved onto the channel form get theirs here.
      if (!isApp.value && !editing.destination)
        await request('/messaging/destinations', 'POST', {
          key: key.value,
          channel_id: editing.id,
          name: name.value,
        })
    } else
      await request(
        '/messaging/channels',
        'POST',
        isApp.value
          ? { name: name.value, provider: 'feishu_app', app_id: appId.value, credential }
          : {
              name: name.value,
              provider: 'feishu_webhook',
              credential,
              destination_key: key.value,
            },
      )
    webhook.value = secret.value = appSecret.value = ''
    channelDialog.value = false
    notice.value = !editing && isApp.value ? '渠道已保存，接下来为它添加要发送的群' : '渠道已保存'
    channelList.search()
  })
}
async function deleteChannel(row: MessageChannel) {
  const keys =
    row.provider === 'feishu_app'
      ? '它的发送目标'
      : `消息标识${row.destination ? `“${row.destination.key}”` : ''}`
  if (
    !(await confirmAction(
      `删除“${row.name}”？${keys}和投递记录会一并删除，待发送的消息不再发送，删除后不可恢复。只想暂停可改为停用。`,
      { title: '删除渠道', confirmText: '确认删除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/channels/${row.id}`, 'DELETE')
    notice.value = '渠道已删除'
    await channelList.reload()
    targetList.search()
    if (can('message_delivery:read')) deliveryList.search()
  })
}
async function sendTest(destinationId: string) {
  await run(async () => {
    await request(`/messaging/destinations/${destinationId}/test`, 'POST')
    notice.value = '测试消息已登记，Worker 将在几秒内发送，可在投递记录中查看结果'
    if (can('message_delivery:read')) deliveryList.search()
  })
}

const targetDialog = ref(false),
  editingTarget = ref<MessageDestination | null>(null),
  targetChannel = ref(''),
  chatId = ref(''),
  targetKey = ref(''),
  targetName = ref(''),
  targetActive = ref(true),
  chats = ref<MessageChat[]>([]),
  chatsToken = ref<string | null>(null),
  chatsLoading = ref(false),
  chatsError = ref('')
const takenChats = computed(
  () => new Set(targetList.rows.value.map((row) => `${row.channel_id}/${row.external_id}`)),
)
async function loadChats(more = false) {
  if (!targetChannel.value) return
  const channel = targetChannel.value
  chatsLoading.value = true
  chatsError.value = ''
  try {
    const page = await messagingApi.chats(channel, more ? (chatsToken.value ?? '') : undefined)
    if (channel !== targetChannel.value) return
    chats.value = more ? [...chats.value, ...page.items] : page.items
    chatsToken.value = page.page_token
  } catch (reason) {
    chatsError.value = reason instanceof Error ? reason.message : '读取群列表失败'
  } finally {
    chatsLoading.value = false
  }
}
function selectChannel(value: string) {
  targetChannel.value = value
  chatId.value = ''
  chats.value = []
  chatsToken.value = null
  void loadChats()
}
function selectChat(value: string) {
  const previous = chats.value.find((item) => item.chat_id === chatId.value)?.name
  chatId.value = value
  const chat = chats.value.find((item) => item.chat_id === value)
  if (chat && (!targetName.value || targetName.value === previous)) targetName.value = chat.name
}
function editTarget(row: MessageDestination | null, channel?: MessageChannel) {
  editingTarget.value = row
  targetKey.value = row?.key ?? ''
  targetName.value = row?.name ?? ''
  targetActive.value = row?.is_active ?? true
  chatId.value = row?.external_id ?? ''
  chats.value = []
  chatsToken.value = null
  chatsError.value = ''
  error.value = ''
  targetDialog.value = true
  if (row) targetChannel.value = row.channel_id
  else selectChannel(channel?.id ?? appChannels.value[0]?.id ?? '')
}
function addTargetFor(channel: MessageChannel) {
  tab.value = 'targets'
  editTarget(null, channel)
}
async function saveTarget() {
  await run(async () => {
    if (editingTarget.value)
      await request(`/messaging/destinations/${editingTarget.value.id}`, 'PATCH', {
        name: targetName.value,
        is_active: targetActive.value,
      })
    else
      await request('/messaging/destinations', 'POST', {
        key: targetKey.value,
        channel_id: targetChannel.value,
        name: targetName.value,
        external_id: chatId.value,
      })
    targetDialog.value = false
    notice.value = '发送目标已保存'
    targetList.search()
  })
}
async function deleteTarget(row: MessageDestination) {
  if (
    !(await confirmAction(
      `删除发送目标“${row.name}”（${row.key}）？它的投递记录会一并删除，待发送的消息不再发送，删除后不可恢复。只想暂停可改为停用。`,
      { title: '删除发送目标', confirmText: '确认删除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/destinations/${row.id}`, 'DELETE')
    notice.value = '发送目标已删除'
    await targetList.reload()
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
    <el-alert v-if="error && !dialogOpen" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
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
            <el-table-column label="类型" min-width="130"
              ><template #default="{ row }"
                >{{ providerText[row.provider as MessageProvider]
                }}<small v-if="row.app_id">{{ row.app_id }}</small></template
              ></el-table-column
            >
            <el-table-column label="消息标识" min-width="140"
              ><template #default="{ row }">{{
                row.provider === 'feishu_app'
                  ? '按发送目标配置'
                  : (row.destination?.key ?? '未设置')
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
                    v-if="row.provider === 'feishu_app'"
                    type="primary"
                    link
                    :disabled="busy"
                    native-type="button"
                    @click="addTargetFor(row)"
                    >添加群</el-button
                  ><el-button
                    v-else
                    type="primary"
                    link
                    :disabled="busy || !row.is_active || !row.destination?.is_active"
                    native-type="button"
                    @click="sendTest(row.destination!.id)"
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
      <el-tab-pane v-if="tabs.includes('targets')" label="发送目标" name="targets">
        <p>自建应用可以发到机器人所在的多个群，每个群配置一个消息标识，业务按标识发送。</p>
        <div class="row-actions">
          <el-button
            type="primary"
            :disabled="busy || !appChannels.length"
            native-type="button"
            @click="editTarget(null)"
            >添加群</el-button
          ><el-button
            :disabled="busy || targetList.loading.value"
            native-type="button"
            @click="targetList.reload()"
            >刷新</el-button
          >
        </div>
        <el-alert
          v-if="targetList.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ targetList.error.value }}</el-alert
        >
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="targetList.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="发送目标" min-width="160"
              ><template #default="{ row }"
                >{{ row.name }}<small>{{ row.external_id }}</small></template
              ></el-table-column
            >
            <el-table-column label="消息标识" min-width="140"
              ><template #default="{ row }">{{ row.key }}</template></el-table-column
            >
            <el-table-column label="渠道" min-width="140"
              ><template #default="{ row }">{{
                channelName(row.channel_id)
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
                    :disabled="busy || !row.is_active"
                    native-type="button"
                    @click="sendTest(row.id)"
                    >发送测试消息</el-button
                  ><el-button
                    type="primary"
                    link
                    :disabled="busy"
                    native-type="button"
                    @click="editTarget(row)"
                    >编辑</el-button
                  ><RowMore :label="`${row.name}的更多操作`"
                    ><el-button
                      :disabled="busy"
                      native-type="button"
                      type="danger"
                      link
                      @click="deleteTarget(row)"
                      >删除发送目标</el-button
                    ></RowMore
                  >
                </div></template
              ></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无发送目标" /></template>
          </el-table>
        </div>
        <ListPager
          :page="targetList.page.value"
          :count="targetList.rows.value.length"
          :loading="busy || targetList.loading.value"
          @change="(value: number) => (targetList.page.value = value)"
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
            <el-table-column label="错误" min-width="220"
              ><template #default="{ row }"
                >{{ row.error_code ?? '—'
                }}<small v-if="row.error_hint">{{ row.error_hint }}</small></template
              ></el-table-column
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
        <div v-if="!editingChannel" class="check-option">
          <ElRadioGroup v-model="provider" aria-label="渠道类型">
            <ElRadioButton value="feishu_webhook">飞书群机器人</ElRadioButton>
            <ElRadioButton value="feishu_app">飞书自建应用</ElRadioButton>
          </ElRadioGroup>
        </div>
        <p v-if="!editingChannel && !isApp">
          只向添加该机器人的群推送，不支持交互；一个群机器人对应一个消息标识。
        </p>
        <p v-if="!editingChannel && isApp">
          可发到应用机器人所在的多个群。保存后在“发送目标”里选择群；应用需开通“以应用的身份发消息”和“获取群组信息”权限，并启用机器人能力。
        </p>
        <label>渠道名称<el-input v-model="name" required maxlength="128" /></label>
        <template v-if="!isApp">
          <label
            >消息标识<el-input
              v-model="key"
              :disabled="!!editingChannel?.destination"
              required
              pattern="[a-z][a-z0-9_.]{0,63}"
              placeholder="ops.alerts"
          /></label>
          <p>业务代码按消息标识发送，换群或换机器人只改渠道配置；标识保存后不可修改。</p>
        </template>
        <label v-if="isApp"
          >App ID<el-input
            v-model="appId"
            :disabled="!!editingChannel"
            required
            pattern="cli_[A-Za-z0-9]{1,60}"
            placeholder="cli_…"
        /></label>
        <el-checkbox v-if="editingChannel" class="check-option" v-model="active"
          >启用渠道</el-checkbox
        >
        <el-checkbox v-if="editingChannel" class="check-option" v-model="replaceCredential">{{
          isApp ? '更换 App Secret' : '更换 Webhook 与签名密钥'
        }}</el-checkbox>
        <template v-if="replaceCredential && isApp">
          <label
            >App Secret<el-input
              v-model="appSecret"
              required
              type="password"
              show-password
              autocomplete="off"
          /></label>
          <p>凭据加密保存，保存后不再显示。换应用请新建渠道。</p>
        </template>
        <template v-if="replaceCredential && !isApp">
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
    <AppDialog
      v-if="targetDialog"
      protect-changes
      :title="editingTarget ? '编辑发送目标' : '添加群'"
      :busy="busy"
      @close="
        () => {
          targetDialog = false
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="saveTarget">
        <template v-if="!editingTarget">
          <label
            >渠道<AppSelect
              :model-value="targetChannel"
              required
              aria-label="渠道"
              @update:model-value="selectChannel"
            >
              <el-option
                v-for="item in appChannels"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              /> </AppSelect
          ></label>
          <label
            >群<AppSelect
              :model-value="chatId"
              required
              filterable
              aria-label="群"
              :disabled="chatsLoading && !chats.length"
              :placeholder="chatsLoading ? '正在读取机器人所在的群…' : '选择群'"
              @update:model-value="selectChat"
            >
              <el-option
                v-for="item in chats"
                :key="item.chat_id"
                :label="item.external ? `${item.name}（外部群）` : item.name"
                :value="item.chat_id"
                :disabled="takenChats.has(`${targetChannel}/${item.chat_id}`)"
              /> </AppSelect
          ></label>
          <el-alert v-if="chatsError" role="alert" type="error" show-icon :closable="false">{{
            chatsError
          }}</el-alert>
          <p v-else-if="!chatsLoading && !chats.length">
            没有读到群。请先把应用机器人拉进群，再点“重新读取”。
          </p>
          <div class="row-actions">
            <el-button
              :disabled="chatsLoading || !targetChannel"
              native-type="button"
              @click="loadChats()"
              >重新读取</el-button
            ><el-button
              v-if="chatsToken"
              :disabled="chatsLoading"
              native-type="button"
              @click="loadChats(true)"
              >加载更多群</el-button
            >
          </div>
          <label
            >消息标识<el-input
              v-model="targetKey"
              required
              pattern="[a-z][a-z0-9_.]{0,63}"
              placeholder="ops.alerts"
          /></label>
        </template>
        <p v-else>消息标识 {{ editingTarget.key }}，群 {{ editingTarget.external_id }}。</p>
        <label>名称<el-input v-model="targetName" required maxlength="128" /></label>
        <el-checkbox v-if="editingTarget" class="check-option" v-model="targetActive"
          >启用发送目标</el-checkbox
        >
        <p v-if="!editingTarget">业务代码按消息标识发送，标识保存后不可修改。</p>
        <el-button :disabled="busy" native-type="submit" type="primary">保存发送目标</el-button>
      </form>
    </AppDialog>
  </section>
</template>
