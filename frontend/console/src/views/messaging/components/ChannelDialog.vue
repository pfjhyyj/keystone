<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElRadioButton, ElRadioGroup } from 'element-plus'
import { request } from '../../../api/client'
import type { MessageChannel, MessageProvider } from '../../../api/messaging'
import { useAction } from '../../../composables/useAction'
import AppDialog from '../../../components/AppDialog.vue'

const props = defineProps<{ channel: MessageChannel | null }>()
const emit = defineEmits<{ close: []; saved: [created: { id: string; app: boolean } | null] }>()
const { busy, error, run } = useAction()
const editing = props.channel
const provider = ref<MessageProvider>(editing?.provider ?? 'feishu_webhook'),
  name = ref(editing?.name ?? ''),
  key = ref(editing?.destination?.key ?? ''),
  active = ref(editing?.is_active ?? true),
  replaceCredential = ref(!editing),
  webhook = ref(''),
  secret = ref(''),
  appId = ref(editing?.app_id ?? ''),
  appSecret = ref('')
const isApp = computed(() => provider.value === 'feishu_app')

async function save() {
  await run(async () => {
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
      webhook.value = secret.value = appSecret.value = ''
      emit('saved', null)
      return
    }
    const created = await request<MessageChannel>(
      '/messaging/channels',
      'POST',
      isApp.value
        ? { name: name.value, provider: 'feishu_app', app_id: appId.value, credential }
        : { name: name.value, provider: 'feishu_webhook', credential, destination_key: key.value },
    )
    webhook.value = secret.value = appSecret.value = ''
    emit('saved', { id: created.id, app: isApp.value })
  })
}
</script>
<template>
  <AppDialog
    protect-changes
    :title="editing ? '编辑渠道' : '新建渠道'"
    :busy="busy"
    @close="emit('close')"
  >
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <form class="dialog-form" @submit.prevent="save">
      <div v-if="!editing" class="check-option">
        <ElRadioGroup v-model="provider" aria-label="渠道类型">
          <ElRadioButton value="feishu_webhook">飞书群机器人</ElRadioButton>
          <ElRadioButton value="feishu_app">飞书自建应用</ElRadioButton>
        </ElRadioGroup>
      </div>
      <p v-if="!editing && !isApp">
        在群设置里添加的自定义机器人，只能往这一个群推送，不支持按钮交互。业务按消息标识发送。
      </p>
      <p v-if="!editing && isApp">
        企业自建应用的机器人，可以发到它所在的多个群和绑定的成员，并接收卡片按钮点击。保存后在渠道详情里添加群、绑定成员；飞书后台的配置见“飞书自建应用配置说明”。
      </p>
      <label>渠道名称<el-input v-model="name" required maxlength="128" /></label>
      <template v-if="!isApp">
        <label
          >消息标识<el-input
            v-model="key"
            :disabled="!!editing?.destination"
            required
            pattern="[a-z][a-z0-9_.]{0,63}"
            placeholder="ops.alerts"
        /></label>
        <p>业务代码按消息标识发送，换群或换机器人只改渠道配置；标识保存后不可修改。</p>
      </template>
      <label v-if="isApp"
        >App ID<el-input
          v-model="appId"
          :disabled="!!editing"
          required
          pattern="cli_[A-Za-z0-9]{1,60}"
          placeholder="cli_…"
      /></label>
      <el-checkbox v-if="editing" class="check-option" v-model="active">启用渠道</el-checkbox>
      <el-checkbox v-if="editing" class="check-option" v-model="replaceCredential">{{
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
</template>
