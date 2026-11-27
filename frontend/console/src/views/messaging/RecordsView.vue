<script setup lang="ts">
import { computed, inject, ref, shallowRef, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { can, sessionVersion } from '../../stores/session'
import {
  messagingApi,
  type MessageChannel,
  type MessageDelivery,
  type MessageInbound,
} from '../../api/messaging'
import { usePagedList } from '../../composables/usePagedList'
import AppSelect from '../../components/AppSelect.vue'
import ListPager from '../../components/ListPager.vue'
import StatusTag from '../../components/StatusTag.vue'
import { deliveryText, inboundKind, inboundText, time } from './labels'

const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined)
const channelId = ref(typeof route?.query.channel === 'string' ? route.query.channel : ''),
  sentStatus = ref(''),
  receivedStatus = ref('')
const sent = usePagedList('messaging.sent-records', 'message_delivery:read', (offset, limit) =>
  messagingApi.deliveriesPage(offset, limit, {
    channelId: channelId.value,
    status: sentStatus.value,
  }),
)
const received = usePagedList(
  'messaging.received-records',
  'message_delivery:read',
  (offset, limit) =>
    messagingApi.inboundsPage(offset, limit, {
      channelId: channelId.value,
      status: receivedStatus.value,
    }),
)
// The channel filter uses options, not the page size from the channel management table.
const channels = shallowRef<MessageChannel[]>([])
watch(
  [() => can('messaging:manage'), sessionVersion],
  async ([allowed], _, onCleanup) => {
    channels.value = []
    if (!allowed) return
    let current = true
    onCleanup(() => (current = false))
    try {
      const options = await messagingApi.channels(0)
      if (current) channels.value = options
    } catch {
      // Keep raw channel IDs as labels when the options are unavailable.
    }
  },
  { immediate: true },
)
const channelName = (id: string) =>
  channels.value.find((row) => row.id === id)?.name ?? id.slice(0, 8)
const TABS = ['sent', 'received']
const localTab = ref('')
const tab = computed({
  get: () => {
    const wanted = String(route?.query.tab ?? localTab.value)
    return TABS.includes(wanted) ? wanted : 'sent'
  },
  set: (value: string) => {
    localTab.value = value
    if (router) void router.replace({ query: { ...route?.query, tab: value } })
  },
})
function filterChannel(value: string) {
  channelId.value = value
  if (router) void router.replace({ query: { ...route?.query, channel: value || undefined } })
  sent.search()
  received.search()
}
</script>
<template>
  <section aria-label="消息记录">
    <div class="heading"><h1>消息记录</h1></div>
    <p>只记录发送和处理的结果，不保存消息内容。</p>
    <el-tabs v-model="tab">
      <el-tab-pane label="发出的消息" name="sent">
        <form class="module-form" @submit.prevent="sent.search()">
          <div v-if="can('messaging:manage')" class="form-field">
            <span class="form-label">渠道</span
            ><AppSelect
              :model-value="channelId"
              aria-label="渠道"
              @update:model-value="filterChannel($event ?? '')"
              ><el-option value="" label="全部渠道" /><el-option
                v-for="item in channels"
                :key="item.id"
                :label="item.name"
                :value="item.id"
            /></AppSelect>
          </div>
          <div class="form-field">
            <span class="form-label">发送状态</span
            ><AppSelect v-model="sentStatus" aria-label="发送状态" @change="sent.search()"
              ><el-option value="" label="全部" /><el-option
                v-for="(label, value) in deliveryText"
                :key="value"
                :label="label"
                :value="value"
            /></AppSelect>
          </div>
          <el-button :disabled="sent.loading.value" native-type="button" @click="sent.reload()"
            >刷新</el-button
          >
        </form>
        <el-alert v-if="sent.error.value" role="alert" type="error" show-icon :closable="false">{{
          sent.error.value
        }}</el-alert>
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="sent.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="登记时间" min-width="170"
              ><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column
            >
            <el-table-column label="发送到" min-width="160"
              ><template #default="{ row }"
                >{{
                  row.destination_key ??
                  (row.recipient
                    ? `用户 ${row.recipient.label ?? row.recipient.id}`
                    : row.reply_inbound_id
                      ? '回复收到的命令'
                      : '—')
                }}<small>{{ channelName(row.channel_id) }}</small></template
              ></el-table-column
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
                  deliveryText[row.status as MessageDelivery['status']]
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
            <template #empty><el-empty :image-size="64" description="暂无发出的消息" /></template>
          </el-table>
        </div>
        <ListPager
          :page="sent.page.value"
          :count="sent.rows.value.length"
          :loading="sent.loading.value"
          :size="sent.size.value"
          :total="sent.total.value"
          @change="sent.page.value = $event"
          @size-change="sent.setSize"
        />
      </el-tab-pane>
      <el-tab-pane label="收到的操作" name="received">
        <p>
          飞书里点击卡片按钮、私聊应用机器人发送绑定码，会记录在这里。只有飞书自建应用渠道能收到。
        </p>
        <form class="module-form" @submit.prevent="received.search()">
          <div v-if="can('messaging:manage')" class="form-field">
            <span class="form-label">渠道</span
            ><AppSelect
              :model-value="channelId"
              aria-label="渠道"
              @update:model-value="filterChannel($event ?? '')"
              ><el-option value="" label="全部渠道" /><el-option
                v-for="item in channels"
                :key="item.id"
                :label="item.name"
                :value="item.id"
            /></AppSelect>
          </div>
          <div class="form-field">
            <span class="form-label">处理结果</span
            ><AppSelect v-model="receivedStatus" aria-label="处理结果" @change="received.search()"
              ><el-option value="" label="全部" /><el-option
                v-for="(label, value) in inboundText"
                :key="value"
                :label="label[0]"
                :value="value"
            /></AppSelect>
          </div>
          <el-button
            :disabled="received.loading.value"
            native-type="button"
            @click="received.reload()"
            >刷新</el-button
          >
        </form>
        <el-alert
          v-if="received.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ received.error.value }}</el-alert
        >
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="received.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="收到时间" min-width="170"
              ><template #default="{ row }">{{ time(row.received_at) }}</template></el-table-column
            >
            <el-table-column label="操作" min-width="150"
              ><template #default="{ row }"
                >{{ inboundKind[row.kind as MessageInbound['kind']]
                }}<small>{{
                  row.command_key
                    ? `${row.command_module}.${row.command_key}`
                    : (row.action_key ?? channelName(row.channel_id))
                }}</small></template
              ></el-table-column
            >
            <el-table-column label="操作人" min-width="180"
              ><template #default="{ row }"
                >{{ row.actor ? (row.actor.label ?? row.actor.id) : '未绑定的飞书用户'
                }}<small>{{ row.external_user_id }}</small></template
              ></el-table-column
            >
            <el-table-column label="结果" min-width="100"
              ><template #default="{ row }"
                ><StatusTag :status="inboundText[row.status as MessageInbound['status']][1]">{{
                  inboundText[row.status as MessageInbound['status']][0]
                }}</StatusTag></template
              ></el-table-column
            >
            <el-table-column label="原因" min-width="220"
              ><template #default="{ row }"
                >{{ row.error_code ?? '—'
                }}<small v-if="row.error_hint">{{ row.error_hint }}</small></template
              ></el-table-column
            >
            <el-table-column label="处理时间" min-width="170"
              ><template #default="{ row }">{{ time(row.processed_at) }}</template></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无收到的操作" /></template>
          </el-table>
        </div>
        <ListPager
          :page="received.page.value"
          :count="received.rows.value.length"
          :loading="received.loading.value"
          :size="received.size.value"
          :total="received.total.value"
          @change="received.page.value = $event"
          @size-change="received.setSize"
        />
      </el-tab-pane>
    </el-tabs>
  </section>
</template>
