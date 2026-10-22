<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import {
  modelApi,
  providers,
  balanceStates,
  type Channel,
  type ModelEntry,
  type ModelOptions,
} from '../../api/models'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import ListPager from '../../components/ListPager.vue'
import ChannelPicker from './components/ChannelPicker.vue'
import ModelProbe from './components/ModelProbe.vue'
import InvocationHistory from './components/InvocationHistory.vue'
const channels = usePagedList('model:manage', modelApi.channels),
  entries = usePagedList('model:manage', modelApi.entries)
const { busy, error, notice, run } = useAction()
const options = ref<ModelOptions | null>(null),
  optionsError = ref(''),
  historyVersion = ref(0)
const form = ref<'channel' | 'entry' | null>(null),
  editingChannel = ref<Channel | null>(null),
  editingEntry = ref<ModelEntry | null>(null)
const name = ref(''),
  provider = ref('openrouter'),
  apiBase = ref(''),
  credential = ref(''),
  billing = ref(''),
  clearBilling = ref(false),
  account = ref(''),
  active = ref(true)
const key = ref(''),
  channelId = ref(''),
  upstream = ref('')
async function loadOptions() {
  optionsError.value = ''
  try {
    options.value = await request<ModelOptions>('/model-access/options')
  } catch (e) {
    options.value = null
    optionsError.value = e instanceof Error ? e.message : '配置不可用'
  }
}
function channel(row: Channel | null) {
  editingChannel.value = row
  name.value = row?.name ?? ''
  provider.value = row?.provider ?? 'openrouter'
  apiBase.value = row?.api_base ?? 'https://openrouter.ai/api/v1'
  credential.value = ''
  billing.value = ''
  clearBilling.value = false
  account.value = row?.upstream_account_ref ?? ''
  active.value = row?.is_active ?? true
  error.value = ''
  form.value = 'channel'
}
function entry(row: ModelEntry | null) {
  editingEntry.value = row
  key.value = row?.key ?? ''
  channelId.value = row?.channel_id ?? ''
  upstream.value = row?.provider_model ?? ''
  active.value = row?.is_active ?? true
  error.value = ''
  form.value = 'entry'
}
async function save() {
  await run(async () => {
    if (!can('model:manage')) return
    if (form.value === 'channel') {
      const body = {
        name: name.value,
        api_base: apiBase.value,
        ...(credential.value ? { api_key: credential.value } : {}),
        ...(billing.value && !clearBilling.value ? { billing_api_key: billing.value } : {}),
        clear_billing_api_key: clearBilling.value,
        upstream_account_ref: account.value || null,
        is_active: active.value,
      }
      if (editingChannel.value)
        await request(`/model-channels/${editingChannel.value.id}`, 'PATCH', body)
      else await request('/model-channels', 'POST', { ...body, provider: provider.value })
      channels.search()
    } else {
      const body = {
        channel_id: channelId.value,
        provider_model: upstream.value,
        is_active: active.value,
      }
      if (editingEntry.value)
        await request(`/model-entries/${editingEntry.value.id}`, 'PATCH', body)
      else await request('/model-entries', 'POST', { ...body, key: key.value })
      entries.search()
    }
    form.value = null
    notice.value = '配置已保存；可用模型选择器可手动刷新。'
  })
}
async function balance(row: Channel) {
  await run(async () => {
    await request(`/model-channels/${row.id}/balance`, 'POST')
    notice.value = '余额查询已结束，请查看最新查询状态与成功采集时间。'
    await channels.reload()
  })
}
watch(form, (value) => {
  if (value !== 'channel') {
    credential.value = ''
    billing.value = ''
    clearBilling.value = false
  }
})
watch(provider, (value) => {
  if (form.value === 'channel' && !editingChannel.value) {
    const bases: Record<string, string> = {
      openrouter: 'https://openrouter.ai/api/v1',
      openlux: 'https://api.openlux.ai/v1',
      yunwu: 'https://yunwu.ai/v1',
    }
    apiBase.value = bases[value] ?? ''
  }
})
onMounted(() => {
  if (can('model:manage')) void loadOptions()
})
</script>
<template>
  <section aria-label="模型接入">
    <div class="heading">
      <div>
        <h1>模型接入</h1>
        <p>管理渠道、模型与每次调用的费用。模型调用使用真实服务，未知费用不代表零费用。</p>
      </div>
    </div>
    <el-alert v-if="error && !form" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <template v-if="can('model:manage')">
      <el-alert v-if="optionsError" role="alert" type="error" show-icon :closable="false">{{
        optionsError
      }}</el-alert>
      <div class="row-actions">
        <span>真实发送（OpenRouter、Openlux、Yunwu）</span
        ><el-button :disabled="busy" @click="run(loadOptions)" native-type="button" type="default"
          >刷新服务商</el-button
        >
      </div>
      <section aria-label="渠道账户">
        <div class="heading">
          <h2>渠道账户</h2>
          <div class="row-actions">
            <el-button
              :disabled="busy || !options"
              @click="channel(null)"
              native-type="button"
              type="primary"
              >新建渠道</el-button
            ><el-button
              :disabled="busy || channels.loading.value"
              @click="channels.reload"
              native-type="button"
              type="default"
            >
              刷新渠道
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="channels.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ channels.error.value }}</el-alert
        >
        <p v-if="channels.loading.value" role="status">正在加载渠道…</p>
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            :data="channels.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="渠道" min-width="180"
              ><template #default="{ row: row }"
                >{{ row.name }}<small>{{ row.id }}</small></template
              ></el-table-column
            >
            <el-table-column label="服务商" min-width="180"
              ><template #default="{ row: row }">{{
                providers[row.provider as keyof typeof providers] ?? row.provider
              }}</template></el-table-column
            >
            <el-table-column label="状态" min-width="120"
              ><template #default="{ row: row }"
                ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
                  row.is_active ? '启用' : '停用'
                }}</StatusTag></template
              ></el-table-column
            >
            <el-table-column label="最近成功余额" min-width="180"
              ><template #default="{ row: row }"
                ><p v-for="item in row.balance_snapshot?.items" :key="item.kind + item.unit">
                  {{ item.kind }}：{{ item.amount }} {{ item.unit }}
                </p>
                <p v-if="!row.balance_snapshot">未知，不代表零余额</p>
                <small
                  >查询状态：{{ balanceStates[row.balance_check_status] }}<br />最近尝试：{{
                    row.balance_last_attempt_at
                      ? new Date(row.balance_last_attempt_at).toLocaleString()
                      : '无'
                  }}<br />成功采集：{{
                    row.balance_checked_at
                      ? new Date(row.balance_checked_at).toLocaleString()
                      : '无'
                  }}</small
                >
                <p v-if="row.balance_snapshot && row.balance_check_status !== 'ok'">
                  这是上次成功结果，不代表当前余额。
                </p></template
              ></el-table-column
            >
            <el-table-column label="操作" min-width="220"
              ><template #default="{ row: row }"
                ><div class="row-actions">
                  <el-button
                    :disabled="busy || !options"
                    @click="channel(row)"
                    native-type="button"
                    type="primary"
                    link
                    >编辑渠道</el-button
                  ><el-button
                    :disabled="busy"
                    @click="balance(row)"
                    native-type="button"
                    type="primary"
                    link
                    >刷新余额</el-button
                  >
                </div></template
              ></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无渠道" /></template
          ></el-table>
        </div>
        <ListPager
          :page="channels.page.value"
          :count="channels.rows.value.length"
          :loading="busy || channels.loading.value"
          @change="channels.page.value = $event"
        />
      </section>
      <section aria-label="模型配置">
        <div class="heading">
          <h2>模型配置</h2>
          <div class="row-actions">
            <el-button :disabled="busy" @click="entry(null)" native-type="button" type="primary"
              >新建模型</el-button
            ><el-button
              :disabled="busy || entries.loading.value"
              @click="entries.reload"
              native-type="button"
              type="default"
            >
              刷新模型配置
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="entries.error.value"
          role="alert"
          type="error"
          show-icon
          :closable="false"
          >{{ entries.error.value }}</el-alert
        >
        <p v-if="entries.loading.value" role="status">正在加载模型…</p>
        <div class="table-scroll">
          <el-table
            :fit="false"
            native-scrollbar
            :data="entries.rows.value"
            row-key="id"
            stripe
            class="data-table"
          >
            <el-table-column label="模型 key" min-width="180"
              ><template #default="{ row: row }">{{ row.key }}</template></el-table-column
            >
            <el-table-column label="渠道 ID" min-width="180"
              ><template #default="{ row: row }">{{ row.channel_id }}</template></el-table-column
            >
            <el-table-column label="上游模型" min-width="180"
              ><template #default="{ row: row }">{{
                row.provider_model
              }}</template></el-table-column
            >
            <el-table-column label="状态" min-width="120"
              ><template #default="{ row: row }"
                ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
                  row.is_active ? '启用' : '停用'
                }}</StatusTag></template
              ></el-table-column
            >
            <el-table-column label="操作" min-width="220"
              ><template #default="{ row: row }"
                ><el-button
                  :disabled="busy"
                  @click="entry(row)"
                  native-type="button"
                  type="primary"
                  link
                  >编辑模型</el-button
                ></template
              ></el-table-column
            >
            <template #empty><el-empty :image-size="64" description="暂无模型配置" /></template
          ></el-table>
        </div>
        <ListPager
          :page="entries.page.value"
          :count="entries.rows.value.length"
          :loading="busy || entries.loading.value"
          @change="entries.page.value = $event"
        />
      </section>
    </template>
    <ModelProbe v-if="can('model:invoke')" @completed="historyVersion++" />
    <InvocationHistory v-if="can('model_invocation:read')" :version="historyVersion" />
    <AppDialog
      v-if="form"
      :title="
        form === 'channel'
          ? editingChannel
            ? '编辑渠道'
            : '新建渠道'
          : editingEntry
            ? '编辑模型'
            : '新建模型'
      "
      :busy="busy"
      @close="
        () => {
          form = null
          error = ''
        }
      "
    >
      <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
        error
      }}</el-alert>
      <form class="dialog-form" @submit.prevent="save">
        <template v-if="form === 'channel'"
          ><label>渠道名称<el-input v-model="name" required maxlength="128" /></label>
          <div class="form-field">
            <span class="form-label">服务商</span
            ><AppSelect v-model="provider" aria-label="服务商" :disabled="!!editingChannel"
              ><el-option
                v-if="editingChannel && !options?.providers?.includes(provider)"
                :value="provider"
                :label="providers[provider as keyof typeof providers] ?? provider" /><el-option
                v-for="value in options?.providers ?? ['openrouter']"
                :key="value"
                :value="value"
                :label="providers[value as keyof typeof providers] ?? value"
            /></AppSelect>
          </div>
          <label>服务地址<el-input v-model="apiBase" type="url" required maxlength="512" /></label>
          <label
            >推理 API Key<el-input
              v-model="credential"
              type="password"
              autocomplete="new-password"
              :required="!editingChannel?.has_api_key"
              maxlength="4096"
              :placeholder="editingChannel?.has_api_key ? '已设置，留空保留' : '请输入 API Key'"
          /></label>
          <p v-if="provider === 'openrouter'">
            OpenRouter 地址固定为 https://openrouter.ai/api/v1。推理使用普通 API
            Key；可选的账户余额查询使用同一账户的 Management Key。
          </p>
          <p v-else-if="provider === 'openlux' || provider === 'yunwu'">
            Openlux 地址为 https://api.openlux.ai/v1，Yunwu 地址为 https://yunwu.ai/v1。
            费用自动使用原推理 Key 查询；更换 Token 或站点请新建渠道，保留旧渠道用于历史查费。
          </p>
          <label
            >账务 API Key<el-input
              v-model="billing"
              type="password"
              autocomplete="new-password"
              maxlength="4096"
              :disabled="clearBilling"
              :placeholder="
                editingChannel?.has_billing_api_key ? '已设置，留空保留' : '可选 Management Key'
              "
          /></label>
          <el-checkbox v-if="editingChannel?.has_billing_api_key" v-model="clearBilling"
            >清除已保存的账务 Key</el-checkbox
          >
          <label
            >账户标识（非秘密）<el-input
              v-model="account"
              maxlength="128"
              :disabled="!!editingChannel?.upstream_account_ref"
          /></label>
          <p>密钥保存后不回显；编辑时留空保留原值。已确定的账户标识不能更换，换账户须新建渠道。</p>
        </template>
        <template v-else
          ><label
            >模型 key<el-input
              v-model="key"
              :disabled="!!editingEntry"
              required
              pattern="[a-z][a-z0-9_-]{0,63}" /></label
          ><ChannelPicker v-model="channelId" :disabled="busy" /><label
            >上游模型<el-input v-model="upstream" required maxlength="128" /></label
        ></template>
        <el-checkbox class="check-option" v-model="active">启用配置</el-checkbox>
        <p>配置只停用，不删除。服务商和模型 key 创建后不可修改。</p>
        <el-button :disabled="busy" native-type="submit" type="primary">{{
          form === 'channel' ? '保存渠道' : '保存模型'
        }}</el-button>
      </form>
    </AppDialog>
  </section>
</template>
