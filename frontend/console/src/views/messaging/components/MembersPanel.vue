<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { can } from '../../../stores/session'
import { request } from '../../../api/client'
import { adminApi, type ManagedUser } from '../../../api/admin'
import {
  completeContact,
  contactField,
  messagingApi,
  type MessageAccount,
  type MessageChannel,
  type MessageLookup,
} from '../../../api/messaging'
import { usePagedList } from '../../../composables/usePagedList'
import { useAction } from '../../../composables/useAction'
import { confirmAction } from '../../../utils/confirm'
import AppDialog from '../../../components/AppDialog.vue'
import AppSelect from '../../../components/AppSelect.vue'
import ListPager from '../../../components/ListPager.vue'
import RowMore from '../../../components/RowMore.vue'
import { time } from '../labels'

const props = defineProps<{ channel: MessageChannel }>()
const emit = defineEmits<{ changed: [] }>()
const list = usePagedList('messaging.members', 'messaging:manage', (offset, limit) =>
  messagingApi.membersPage(props.channel.id, offset, limit),
)
const { busy, error, notice, run } = useAction()
const dialog = ref(false),
  user = ref(''),
  contact = ref(''),
  users = ref<ManagedUser[]>([]),
  usersLoading = ref(false),
  found = ref<(MessageLookup & { contact: string }) | null>(null),
  lookupLoading = ref(false),
  lookupMessage = ref('输入完整的手机号或邮箱')
let userSearch = 0,
  lookupGeneration = 0,
  lookupTimer: ReturnType<typeof setTimeout> | undefined

function lookupContact(query: string) {
  const value = query.trim(),
    current = ++lookupGeneration
  clearTimeout(lookupTimer)
  found.value = null
  lookupLoading.value = false
  if (!completeContact(value)) {
    lookupMessage.value = '输入完整的手机号或邮箱'
    return
  }
  lookupLoading.value = true
  lookupTimer = setTimeout(async () => {
    try {
      const result = await messagingApi.lookup(props.channel.id, value)
      if (current === lookupGeneration) found.value = { ...result, contact: value }
    } catch (reason) {
      if (current === lookupGeneration)
        lookupMessage.value = reason instanceof Error ? reason.message : '查询失败'
    } finally {
      if (current === lookupGeneration) lookupLoading.value = false
    }
  }, 300)
}
onBeforeUnmount(() => {
  lookupGeneration++
  clearTimeout(lookupTimer)
})
const foundLabel = computed(() => {
  const item = found.value
  if (!item) return ''
  const who = item.name ?? `飞书用户 ${item.external_user_id}`
  return item.bound_to ? `${who}（已绑定到 ${item.bound_to.label}）` : who
})
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
function open() {
  user.value = contact.value = ''
  found.value = null
  lookupMessage.value = '输入完整的手机号或邮箱'
  error.value = ''
  dialog.value = true
  void searchUsers('')
}
async function save() {
  await run(async () => {
    await request('/messaging/accounts', 'POST', {
      channel_id: props.channel.id,
      user_id: user.value,
      ...contactField(contact.value.trim()),
    })
    contact.value = ''
    dialog.value = false
    notice.value = '飞书账号已绑定'
    list.search()
    emit('changed')
  })
}
async function sendTest(row: MessageAccount, card: boolean) {
  await run(async () => {
    await request(`/messaging/accounts/${row.id}/test${card ? '?card=true' : ''}`, 'POST')
    notice.value = card
      ? '测试卡片已登记。对方点击“确认收到”后，可在消息记录的“收到的操作”里查看'
      : '测试消息已登记，Worker 将在几秒内发送，可在消息记录里查看结果'
  })
}
async function unbind(row: MessageAccount) {
  if (
    !(await confirmAction(
      `解除“${row.user.label ?? row.user.id}”的飞书账号绑定？之后发给该用户的消息会失败，已登记未发出的也不再发送。`,
      { title: '解除绑定', confirmText: '确认解除', danger: true },
    ))
  )
    return
  await run(async () => {
    await request(`/messaging/accounts/${row.id}`, 'DELETE')
    notice.value = '已解除绑定'
    await list.reload()
    emit('changed')
  })
}
</script>
<template>
  <div class="panel">
    <p>
      绑定后，业务可以把消息发给这位 Keystone
      用户，对方在飞书里点击卡片按钮时按自己的权限执行。成员也可以在个人中心生成绑定码自助绑定。
    </p>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error && !dialog" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <div class="row-actions">
      <el-button
        type="primary"
        :disabled="busy || !can('user:read')"
        native-type="button"
        @click="open"
        >绑定成员</el-button
      ><el-button :disabled="busy || list.loading.value" native-type="button" @click="list.reload()"
        >刷新</el-button
      >
    </div>
    <p v-if="!can('user:read')">替成员绑定需要查看用户的权限。</p>
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
        <el-table-column label="成员" min-width="160"
          ><template #default="{ row }"
            >{{ row.user.label ?? '已删除用户' }}<small>{{ row.external_user_id }}</small></template
          ></el-table-column
        >
        <el-table-column label="绑定时间" min-width="170"
          ><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column
        >
        <el-table-column label="操作" min-width="200"
          ><template #default="{ row }"
            ><div class="row-actions">
              <el-button
                type="primary"
                link
                :disabled="busy || !channel.is_active"
                native-type="button"
                @click="sendTest(row, false)"
                >发送测试消息</el-button
              ><RowMore :label="`${row.user.label ?? '用户'}的更多操作`"
                ><el-button
                  :disabled="busy || !channel.is_active"
                  native-type="button"
                  link
                  @click="sendTest(row, true)"
                  >发送测试卡片</el-button
                ><el-button
                  :disabled="busy"
                  native-type="button"
                  type="danger"
                  link
                  @click="unbind(row)"
                  >解除绑定</el-button
                ></RowMore
              >
            </div></template
          ></el-table-column
        >
        <template #empty><el-empty :image-size="64" description="还没有绑定的成员" /></template>
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
    <AppDialog
      v-if="dialog"
      protect-changes
      title="绑定成员"
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
        <label
          >Keystone 用户<AppSelect
            v-model="user"
            required
            filterable
            remote
            aria-label="用户"
            placeholder="搜索用户名或显示名"
            :remote-method="searchUsers"
            :loading="usersLoading"
          >
            <el-option
              v-for="item in users"
              :key="item.id"
              :label="item.display_name || item.username"
              :value="item.id"
            /> </AppSelect
        ></label>
        <label
          >飞书账号<AppSelect
            v-model="contact"
            required
            filterable
            remote
            aria-label="飞书账号"
            placeholder="输入手机号或邮箱搜索"
            :remote-method="lookupContact"
            :loading="lookupLoading"
            loading-text="正在向飞书查询…"
            :no-data-text="lookupMessage"
          >
            <el-option
              v-if="found"
              :key="found.external_user_id"
              :label="foundLabel"
              :value="found.contact"
              :disabled="!!found.bound_to"
            /> </AppSelect
        ></label>
        <p v-if="found?.name_hint">{{ found.name_hint }}</p>
        <p>
          企业邮箱查不到，请用对方在飞书登记的邮箱；非中国大陆手机号带“+”国家代码。对方须在应用的通讯录权限范围内。手机号和邮箱只用于查询，不保存。
        </p>
        <el-button :disabled="busy" native-type="submit" type="primary">绑定</el-button>
      </form>
    </AppDialog>
  </div>
</template>
