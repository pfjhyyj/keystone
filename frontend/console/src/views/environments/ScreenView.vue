<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import type RFB from '@novnc/novnc'
import { environmentsApi, type Environment } from '../../api/environments'
import { useAction } from '../../composables/useAction'
import { loginDestination } from '../../router'
import { can } from '../../stores/session'
import { confirmAction } from '../../utils/confirm'
import AppDialog from '../../components/AppDialog.vue'
import StatusTag from '../../components/StatusTag.vue'
import { backupText, formatTime, stateTag, stateText, unsettled } from './labels'

const props = defineProps<{ kind: string; envKey: string; back?: string }>()
const router = useRouter()
const { busy, error, notice, run } = useAction()
const environment = ref<Environment | null>(null)
const viewer = shallowRef<RFB | null>(null)
const connecting = ref(false),
  connected = ref(false),
  screenMessage = ref('尚未连接画面')
const screen = ref<HTMLElement | null>(null)
const pasting = ref(false),
  pasteText = ref(''),
  pasteError = ref(''),
  pasted = ref(false)
let epoch = 0

const session = computed(() => environment.value?.session ?? null)
const state = computed(() => session.value?.state)
// Only same-site console paths are accepted, like the login destination.
const backPath = computed(() =>
  props.back && loginDestination(props.back, router) === props.back ? props.back : null,
)

async function load() {
  // Declared keys are few, so one page holds them; an unused key gets an empty placeholder.
  const rows = await environmentsApi.list(0)
  environment.value = rows.find((row) => row.kind === props.kind && row.key === props.envKey) ?? {
    id: null,
    kind: props.kind as Environment['kind'],
    key: props.envKey,
    name: props.envKey,
    session: null,
    backup_at: null,
    backup_failed: false,
    backup_enabled: false,
  }
  if ((viewer.value || connecting.value) && state.value !== 'ready') disconnect()
}

function disconnect(message = '尚未连接画面') {
  epoch++
  const active = viewer.value
  viewer.value = null
  connecting.value = connected.value = false
  pasting.value = pasted.value = false
  active?.disconnect()
  screen.value?.replaceChildren()
  screenMessage.value = message
}

async function connect(retry = true) {
  const current = environment.value
  if (!current?.id || !current.session) return
  disconnect('正在连接画面…')
  const started = epoch
  connecting.value = true
  try {
    const { default: Viewer } = await import('@novnc/novnc')
    if (started !== epoch || !screen.value) return
    const connection = new Viewer(
      screen.value,
      environmentsApi.screen(current.id, current.session.id),
      { shared: false },
    )
    viewer.value = connection
    connection.scaleViewport = true
    connection.resizeSession = false
    connection.addEventListener('connect', () => {
      if (started !== epoch) return
      connecting.value = false
      connected.value = true
    })
    connection.addEventListener('disconnect', () => {
      if (started !== epoch) return
      if (retry && connecting.value) {
        // After a reload the previous page may still be releasing input control.
        disconnect('正在重新连接画面…')
        connecting.value = true
        const again = epoch
        setTimeout(() => {
          if (again === epoch) void connect(false)
        }, 1250)
        return
      }
      disconnect('画面已断开，可以重新连接。')
      void run(load)
    })
  } catch {
    if (started === epoch) disconnect('无法连接画面，请重试。')
  }
}

async function open() {
  await run(async () => {
    const current = environment.value!
    try {
      if (!current.id)
        Object.assign(current, await environmentsApi.ensure(props.kind, props.envKey))
      current.session = await environmentsApi.open(current.id!)
    } catch (reason) {
      await load().catch(() => undefined)
      throw reason
    }
    if (current.session.restored_from)
      notice.value = `已从 ${formatTime(current.session.restored_from)} 的备份恢复环境`
    if (current.session.state === 'ready') await connect()
  })
}
async function check() {
  await run(async () => {
    const current = environment.value!
    current.session = await environmentsApi.check(current.id!, current.session!.id)
    if (current.session.state === 'review')
      error.value = '暂时无法确认原会话，请稍后重新检查或联系部署管理员'
  })
}
async function stop() {
  if (
    !(await confirmAction('结束会话会关闭远程窗口，环境中的登录和数据会保留。确定结束？', {
      title: '结束会话',
      confirmText: '确认结束',
      danger: true,
    }))
  )
    return
  disconnect()
  await run(async () => {
    const current = environment.value!
    current.session = await environmentsApi.stop(current.id!, current.session!.id)
    await load()
  })
}

// noVNC has no public flag for the extended clipboard; legacy RFB carries Latin-1 only.
function unicodeClipboard(connection: RFB) {
  const internal = connection as unknown as {
    _clipboardServerCapabilitiesFormats?: Record<number, unknown>
    _clipboardServerCapabilitiesActions?: Record<number, unknown>
  }
  return !!(
    internal._clipboardServerCapabilitiesFormats?.[1] &&
    internal._clipboardServerCapabilitiesActions?.[1 << 27]
  )
}
function sendPaste() {
  const connection = viewer.value
  if (!connection || !connected.value) return
  if (!unicodeClipboard(connection) && /[^\u0000-ÿ]/u.test(pasteText.value)) {
    pasteError.value = '当前画面不支持中文等字符。请结束会话后重新打开，再发送文本。'
    return
  }
  connection.clipboardPasteFrom(pasteText.value)
  pasting.value = false
  pasteText.value = pasteError.value = ''
  pasted.value = true
  connection.focus({ preventScroll: true })
}

onMounted(() =>
  run(async () => {
    await load()
    if (state.value === 'ready') await connect()
  }),
)
onBeforeUnmount(() => disconnect())
</script>
<template>
  <section aria-label="环境画面" class="environment-screen">
    <div class="heading">
      <div>
        <h1>{{ environment?.name ?? envKey }}</h1>
        <p aria-live="polite">
          <StatusTag v-if="state" :status="stateTag[state]">{{ stateText[state] }}</StatusTag
          ><span v-else>未启动</span>
          <template v-if="environment?.backup_enabled">
            · 最近备份：{{ backupText(environment) }}</template
          >
          <template v-if="pasted"> · 文本已发送，在远程窗口按 Ctrl+V 粘贴</template>
        </p>
      </div>
    </div>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <div class="row-actions">
      <el-button
        v-if="environment && !connected && !connecting && !unsettled(state)"
        type="primary"
        :disabled="busy"
        native-type="button"
        @click="state === 'ready' ? connect() : open()"
        >{{ state === 'ready' ? '连接画面' : '打开' }}</el-button
      ><el-button
        v-if="connected"
        :disabled="busy"
        native-type="button"
        @click="
          () => {
            pasteError = ''
            pasting = true
          }
        "
        >发送文本</el-button
      ><el-button v-if="connected || connecting" native-type="button" @click="disconnect()"
        >断开画面</el-button
      ><el-button v-if="unsettled(state)" :disabled="busy" native-type="button" @click="check"
        >重新检查</el-button
      ><el-button
        v-if="session?.can_stop && can('environment:stop')"
        type="danger"
        :disabled="busy"
        native-type="button"
        @click="stop"
        >结束会话</el-button
      ><el-button v-if="backPath" native-type="button" @click="router.push(backPath)"
        >返回业务页面</el-button
      >
    </div>
    <div class="environment-viewer">
      <div ref="screen" class="environment-canvas" :hidden="!viewer" />
      <p v-if="!connected" class="environment-placeholder">
        {{ busy && !connecting ? '正在处理…' : screenMessage }}
      </p>
    </div>
    <AppDialog v-if="pasting" title="发送文本到远程剪贴板" @close="pasting = false">
      <form class="dialog-form" @submit.prevent="sendPaste">
        <el-alert v-if="pasteError" role="alert" type="error" show-icon :closable="false">{{
          pasteError
        }}</el-alert>
        <label class="full-field"
          >文本<el-input
            v-model="pasteText"
            type="textarea"
            :rows="6"
            maxlength="4096"
            required
            aria-label="文本"
        /></label>
        <p class="full-field">只发送文字，不传文件或图片。发送后在远程窗口按 Ctrl+V 粘贴。</p>
        <div class="row-actions">
          <el-button type="primary" native-type="submit" :disabled="!pasteText">发送</el-button
          ><el-button native-type="button" @click="pasting = false">取消</el-button>
        </div>
      </form>
    </AppDialog>
  </section>
</template>
<style scoped>
.environment-viewer {
  position: relative;
  min-height: 420px;
  height: calc(100vh - 260px);
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: #111;
  overflow: hidden;
}
.environment-canvas {
  position: absolute;
  inset: 0;
}
.environment-placeholder {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  margin: 0;
  color: #d0d0d0;
}
</style>
