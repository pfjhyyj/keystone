<script setup lang="ts">
import { computed, ref } from 'vue'
import { can } from '../../../stores/session'
import { request } from '../../../api/client'
import { adminApi, type ManagedUser } from '../../../api/admin'
import {
  messagingApi,
  type MessageChannel,
  type MessageChat,
  type MessageDestination,
} from '../../../api/messaging'
import { usePagedList } from '../../../composables/usePagedList'
import { useAction } from '../../../composables/useAction'
import { confirmAction } from '../../../utils/confirm'
import AppDialog from '../../../components/AppDialog.vue'
import AppSelect from '../../../components/AppSelect.vue'
import ListPager from '../../../components/ListPager.vue'
import RowMore from '../../../components/RowMore.vue'
import StatusTag from '../../../components/StatusTag.vue'

const props = defineProps<{ channel: MessageChannel }>()
const emit = defineEmits<{ changed: [] }>()
const list = usePagedList('messaging:manage', (offset) =>
  messagingApi.groups(props.channel.id, offset),
)
const { busy, error, notice, run } = useAction()
const dialog = ref(false),
  editing = ref<MessageDestination | null>(null),
  chatId = ref(''),
  key = ref(''),
  name = ref(''),
  active = ref(true),
  chats = ref<MessageChat[]>([]),
  chatsToken = ref<string | null>(null),
  chatsLoading = ref(false),
  chatsError = ref('')
const taken = computed(() => new Set(list.rows.value.map((row) => row.external_id)))
const delegating = ref<MessageDestination | null>(null),
  executor = ref(''),
  users = ref<ManagedUser[]>([]),
  usersLoading = ref(false)
let userSearch = 0

async function searchUsers(q: string) {
  const current = ++userSearch
  usersLoading.value = true
  try {
    const rows = await adminApi.users(0, q.trim())
    if (current === userSearch) users.value = rows
  } catch {
    if (current === userSearch) users.value = []
  } finally {
    if (current === userSearch) usersLoading.value = false
  }
}
function delegate(row: MessageDestination) {
  delegating.value = row
  executor.value = row.executing_user?.id ?? ''
  users.value = []
  error.value = ''
  void searchUsers('')
}
async function saveExecutor(userId: string | null) {
  const row = delegating.value
  if (!row) return
  await run(async () => {
    await request(`/messaging/destinations/${row.id}/executor`, 'PUT', { user_id: userId })
    delegating.value = null
    notice.value = userId ? '执行用户已设置' : '已取消执行用户'
    await list.reload()
  })
}

async function loadChats(more = false) {
  chatsLoading.value = true
  chatsError.value = ''
  try {
    const page = await messagingApi.chats(
      props.channel.id,
      more ? (chatsToken.value ?? '') : undefined,
    )
    chats.value = more ? [...chats.value, ...page.items] : page.items
    chatsToken.value = page.page_token
  } catch (reason) {
    chatsError.value = reason instanceof Error ? reason.message : '读取群列表失败'
  } finally {
    chatsLoading.value = false
  }
}
function selectChat(value: string) {
  const previous = chats.value.find((item) => item.chat_id === chatId.value)?.name
  chatId.value = value
  const chat = chats.value.find((item) => item.chat_id === value)
  if (chat && (!name.value || name.value === previous)) name.value = chat.name
}
function edit(row: MessageDestination | null) {
  editing.value = row
  key.value = row?.key ?? ''
  name.value = row?.name ?? ''
  active.value = row?.is_active ?? true
  chatId.value = row?.external_id ?? ''
  chats.value = []
  chatsToken.value = null
  error.value = ''
  dialog.value = true
  if (!row) void loadChats()
}
async function save() {
  await run(async () => {
    if (editing.value)
      await request(`/messaging/destinations/${editing.value.id}`, 'PATCH', {
        name: name.value,
        is_active: active.value,
      })
    else
      await request('/messaging/destinations', 'POST', {
        key: key.value,
        channel_id: props.channel.id,
        name: name.value,
        external_id: chatId.value,
      })
    dialog.value = false
    notice.value = '群已保存'
    list.search()
    emit('changed')
  })
}
async function sendTest(row: MessageDestination, card: boolean) {
  await run(async () => {
    await request(`/messaging/destinations/${row.id}/test${card ? '?card=true' : ''}`, 'POST')
    notice.value = card
      ? '测试卡片已登记。点击卡片上的“确认收到”后，可在消息记录的“收到的操作”里查看；点击人须先绑定飞书账号'
      : '测试消息已登记，Worker 将在几秒内发送，可在消息记录里查看结果'
  })
}
async function remove(row: MessageDestination) {
  if (
    !(await confirmAction(
      `删除群“${row.name}”（${row.key}）？发往它的消息记录会一并删除，待发送的消息不再发送，删除后不可恢复。只想暂停可改为停用。`,
      { title: '删除群', confirmText: '确认删除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/destinations/${row.id}`, 'DELETE')
    notice.value = '群已删除'
    await list.reload()
    emit('changed')
  })
}
</script>
<template>
  <div class="panel">
    <p>应用机器人所在的群。每个群配置一个消息标识，业务代码按标识发送，换群只需在这里修改。</p>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error && !dialog" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <div class="row-actions">
      <el-button type="primary" :disabled="busy" native-type="button" @click="edit(null)"
        >添加群</el-button
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
        <el-table-column label="群" min-width="160"
          ><template #default="{ row }"
            >{{ row.name }}<small>{{ row.external_id }}</small></template
          ></el-table-column
        >
        <el-table-column label="消息标识" min-width="140"
          ><template #default="{ row }"
            ><code>{{ row.key }}</code></template
          ></el-table-column
        >
        <el-table-column label="执行用户" min-width="120"
          ><template #default="{ row }">{{
            row.executing_user ? (row.executing_user.label ?? row.executing_user.id) : '—'
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
                :disabled="busy || !row.is_active || !channel.is_active"
                native-type="button"
                @click="sendTest(row, false)"
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
                  :disabled="busy || !row.is_active || !channel.is_active"
                  native-type="button"
                  link
                  @click="sendTest(row, true)"
                  >发送测试卡片</el-button
                ><el-button
                  v-if="can('messaging:delegate')"
                  :disabled="busy || !can('user:read')"
                  native-type="button"
                  link
                  @click="delegate(row)"
                  >设置执行用户</el-button
                ><el-button
                  :disabled="busy"
                  native-type="button"
                  type="danger"
                  link
                  @click="remove(row)"
                  >删除群</el-button
                ></RowMore
              >
            </div></template
          ></el-table-column
        >
        <template #empty
          ><el-empty
            :image-size="64"
            description="还没有添加群。先把应用机器人拉进群，再点“添加群”。"
        /></template>
      </el-table>
    </div>
    <ListPager
      :page="list.page.value"
      :count="list.rows.value.length"
      :loading="busy || list.loading.value"
      @change="(value: number) => (list.page.value = value)"
    />
    <AppDialog
      v-if="dialog"
      protect-changes
      :title="editing ? '编辑群' : '添加群'"
      :busy="busy"
      @close="
        () => {
          dialog = false
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="save">
        <template v-if="!editing">
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
                :disabled="taken.has(item.chat_id)"
              /> </AppSelect
          ></label>
          <el-alert v-if="chatsError" role="alert" type="error" show-icon :closable="false">{{
            chatsError
          }}</el-alert>
          <p v-else-if="!chatsLoading && !chats.length">
            没有读到群。请先把应用机器人拉进群，再点“重新读取”。
          </p>
          <div class="row-actions">
            <el-button :disabled="chatsLoading" native-type="button" @click="loadChats()"
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
              v-model="key"
              required
              pattern="[a-z][a-z0-9_.]{0,63}"
              placeholder="ops.alerts"
          /></label>
          <p>业务代码按消息标识发送，标识保存后不可修改。</p>
        </template>
        <p v-else>消息标识 {{ editing.key }}，群 {{ editing.external_id }}。</p>
        <label>名称<el-input v-model="name" required maxlength="128" /></label>
        <el-checkbox v-if="editing" class="check-option" v-model="active">启用这个群</el-checkbox>
        <el-button :disabled="busy" native-type="submit" type="primary">保存</el-button>
      </form>
    </AppDialog>
    <AppDialog
      v-if="delegating"
      protect-changes
      title="设置执行用户"
      :busy="busy"
      @close="
        () => {
          delegating = null
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="saveExecutor(executor)">
        <p>
          群“{{
            delegating.name
          }}”的成员发送声明为按群执行的命令或点击这类按钮时，以执行用户的身份和当前权限执行，不需要绑定飞书账号。这相当于把该用户的权限交给群里所有人，只能选择权限不超过你的用户。
        </p>
        <label
          >执行用户<AppSelect
            v-model="executor"
            required
            filterable
            remote
            aria-label="执行用户"
            placeholder="搜索用户名或显示名"
            :remote-method="searchUsers"
            :loading="usersLoading"
          >
            <el-option
              v-if="delegating.executing_user && !users.some((u) => u.id === executor)"
              :label="delegating.executing_user.label ?? delegating.executing_user.id"
              :value="delegating.executing_user.id"
            />
            <el-option
              v-for="item in users"
              :key="item.id"
              :label="item.display_name || item.username"
              :value="item.id"
            /> </AppSelect
        ></label>
        <div class="row-actions">
          <el-button :disabled="busy || !executor" native-type="submit" type="primary"
            >保存</el-button
          ><el-button
            v-if="delegating.executing_user"
            :disabled="busy"
            native-type="button"
            @click="saveExecutor(null)"
            >取消执行用户</el-button
          >
        </div>
      </form>
    </AppDialog>
  </div>
</template>
