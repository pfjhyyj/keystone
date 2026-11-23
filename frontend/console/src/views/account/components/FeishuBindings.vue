<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { messagingApi, type BindCode, type MyBinding } from '../../../api/messaging'
import { useAction } from '../../../composables/useAction'
import { confirmAction } from '../../../utils/confirm'
import StatusTag from '../../../components/StatusTag.vue'

const POLL_MS = 3000
const { busy, error, notice, run } = useAction()
const rows = ref<MyBinding[]>([]),
  loaded = ref(false),
  // The app the user chose to bind on; the code itself works on any of them.
  pending = ref(''),
  code = ref<BindCode | null>(null),
  copied = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined,
  alive = true

const snapshot = (items: MyBinding[]) =>
  JSON.stringify(items.map((row) => row.account && [row.account.id, row.account.external_user_id]))
const time = (value: string) => new Date(value).toLocaleString()

async function load() {
  rows.value = await messagingApi.myBindings()
  loaded.value = true
}
function stopWaiting() {
  clearTimeout(timer)
  timer = undefined
  code.value = null
  pending.value = ''
  copied.value = false
}
// While a code is shown, look for the binding to appear; stop when it does or the code lapses.
function watchForBinding(before: string) {
  timer = setTimeout(async () => {
    if (!alive || !code.value) return
    if (new Date(code.value.expires_at).getTime() <= Date.now()) {
      stopWaiting()
      error.value = '绑定码已过期，请重新生成'
      return
    }
    try {
      const items = await messagingApi.myBindings()
      if (!alive || !code.value) return
      if (snapshot(items) !== before) {
        rows.value = items
        stopWaiting()
        notice.value = '飞书账号已绑定'
        return
      }
    } catch {
      // A failed check is retried on the next tick; the code stays valid meanwhile.
    }
    watchForBinding(before)
  }, POLL_MS)
}
async function bind(row: MyBinding) {
  await run(async () => {
    stopWaiting()
    const issued = await messagingApi.bindCode()
    code.value = issued
    pending.value = row.channel_id
    watchForBinding(snapshot(rows.value))
  })
}
async function copy() {
  if (!code.value) return
  try {
    await navigator.clipboard.writeText(`绑定 ${code.value.code}`)
    copied.value = true
  } catch {
    copied.value = false
  }
}
async function unbind(row: MyBinding) {
  if (!row.account) return
  if (
    !(await confirmAction(
      `解除后，你在飞书里点击“${row.channel_name}”发来的卡片将不再生效，发给你的消息也会失败。之后可以重新绑定。`,
      { title: '解除飞书绑定？', confirmText: '确认解除', danger: true },
    ))
  )
    return
  const account = row.account
  await run(async () => {
    stopWaiting()
    await messagingApi.unbindMine(account.id)
    await load()
    notice.value = '已解除绑定'
  })
}
onMounted(() => run(load))
onBeforeUnmount(() => {
  alive = false
  stopWaiting()
})
</script>
<template>
  <div class="feishu-bindings">
    <p class="lead">
      绑定后，Keystone
      可以把消息直接发给你，你也可以在飞书里点击卡片上的按钮（如确认、审批），操作按你在 Keystone
      的权限执行。
    </p>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="notice" type="success" show-icon :closable="false">{{ notice }}</el-alert>
    <el-empty
      v-if="loaded && !rows.length"
      :image-size="64"
      description="管理员还没有配置飞书应用，暂时无法绑定"
    />
    <div v-for="row in rows" :key="row.channel_id" class="binding-card">
      <div class="binding-head">
        <div>
          <strong>{{ row.channel_name }}</strong>
          <StatusTag :status="row.account ? 'active' : 'disabled'">{{
            row.account ? '已绑定' : '未绑定'
          }}</StatusTag>
        </div>
        <div class="row-actions">
          <el-button
            v-if="!row.account"
            type="primary"
            :disabled="busy || pending === row.channel_id"
            @click="bind(row)"
            >绑定飞书账号</el-button
          >
          <template v-else>
            <el-button :disabled="busy || pending === row.channel_id" @click="bind(row)"
              >更换飞书账号</el-button
            ><el-button type="danger" link :disabled="busy" @click="unbind(row)"
              >解除绑定</el-button
            >
          </template>
        </div>
      </div>
      <p v-if="row.account" class="binding-facts">
        飞书账号 <code>{{ row.account.external_user_id }}</code> ·
        {{ time(row.account.created_at) }}
        绑定
      </p>
      <div v-if="code && pending === row.channel_id" class="bind-steps">
        <ol>
          <li>在飞书中搜索“{{ row.channel_name }}”，打开与它的单聊。</li>
          <li>
            发送下面这条消息：
            <span class="bind-message"
              ><code aria-label="绑定消息">绑定 {{ code.code }}</code
              ><el-button link type="primary" @click="copy">{{
                copied ? '已复制' : '复制'
              }}</el-button></span
            >
          </li>
          <li>收到机器人回复“已绑定”后，这里会自动更新。</li>
        </ol>
        <p class="binding-facts">
          {{ new Date(code.expires_at).toLocaleTimeString() }}
          前有效，只能使用一次。<el-button link @click="stopWaiting">取消</el-button>
        </p>
      </div>
    </div>
  </div>
</template>
<style scoped>
.feishu-bindings {
  display: grid;
  gap: 12px;
  max-width: 720px;
}
.binding-card {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 16px;
  display: grid;
  gap: 8px;
}
.binding-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.binding-head strong {
  margin-right: 8px;
}
.binding-facts {
  color: var(--el-text-color-secondary);
  margin: 0;
}
.bind-steps {
  background: var(--el-fill-color-light);
  border-radius: 6px;
  padding: 12px 16px;
}
.bind-steps ol {
  display: grid;
  gap: 8px;
  margin: 0 0 8px;
  padding-left: 20px;
}
.bind-message {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.bind-message code {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 1px;
}
</style>
