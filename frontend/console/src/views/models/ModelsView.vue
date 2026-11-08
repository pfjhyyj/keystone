<script setup lang="ts">
import { computed, inject, nextTick, onMounted, ref, watch } from 'vue'
import type { TableInstance } from 'element-plus'
import { RouterLink, routeLocationKey, routerKey } from 'vue-router'
import { can } from '../../stores/session'
import { request } from '../../api/client'
import {
  modelApi,
  operationLabels,
  type ModelOperation,
  resolveChannel,
  type Channel,
  type ModelEntry,
  type ModelOptions,
} from '../../api/models'
import { usePagedList } from '../../composables/usePagedList'
import { useAction } from '../../composables/useAction'
import AppDialog from '../../components/AppDialog.vue'
import RowMore from '../../components/RowMore.vue'
import ListPager from '../../components/ListPager.vue'
import ChannelPicker from './components/ChannelPicker.vue'
import UpstreamModelPicker from './components/UpstreamModelPicker.vue'
import ChannelBalance from './components/ChannelBalance.vue'
import PricingDialog from './components/PricingDialog.vue'
const pricingEntry = ref<ModelEntry | null>(null)
const channels = usePagedList('model:manage', modelApi.channels),
  entries = usePagedList('model:manage', modelApi.entries)
const { busy, error, notice, run } = useAction()
const options = ref<ModelOptions | null>(null),
  optionsError = ref('')
const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined)
const localTab = ref('channels')
const configurationTab = computed({
  get: () =>
    ['channels', 'models'].includes(String(route?.query.tab))
      ? String(route?.query.tab)
      : localTab.value,
  set: (value: string) => {
    localTab.value = value
    if (router) void router.replace({ query: { ...route?.query, tab: value } })
  },
})
const channelTable = ref<TableInstance>(),
  modelTable = ref<TableInstance>()
watch(configurationTab, async (tab) => {
  await nextTick()
  if (tab === 'channels') channelTable.value?.doLayout()
  if (tab === 'models') modelTable.value?.doLayout()
})

const form = ref<'channel' | 'entry' | null>(null),
  editingChannel = ref<Channel | null>(null),
  editingEntry = ref<ModelEntry | null>(null)
const name = ref(''),
  provider = ref(''),
  apiBase = ref(''),
  channelFields = ref<Record<string, string>>({}),
  credential = ref(''),
  billing = ref(''),
  billingAk = ref(''),
  billingSk = ref(''),
  billingToken = ref(''),
  clearBilling = ref(false),
  account = ref(''),
  active = ref(true)
const creatableChannels = computed(
  () => options.value?.channels?.filter((item) => item.creatable) ?? [],
)
const selectedChannel = computed(() =>
  resolveChannel({ provider: provider.value, api_base: apiBase.value }, options.value),
)
const cloudBilling = computed(() => selectedChannel.value.billing_credentials === 'cloud')
const keyBilling = computed(() => selectedChannel.value.billing_credentials === 'key')
const describeChannel = (row: Channel) => resolveChannel(row, options.value)
const key = ref(''),
  modelName = ref(''),
  channelId = ref(''),
  upstream = ref('')
const operations = ref<ModelOperation[]>(['text.generate'])
const baseline = ref('')
const fingerprint = computed(() =>
  JSON.stringify([
    name.value,
    provider.value,
    apiBase.value,
    channelFields.value,
    credential.value,
    billing.value,
    billingAk.value,
    billingSk.value,
    billingToken.value,
    clearBilling.value,
    account.value,
    active.value,
    key.value,
    modelName.value,
    channelId.value,
    upstream.value,
    operations.value,
  ]),
)
watch(
  form,
  () => {
    baseline.value = fingerprint.value
  },
  { flush: 'sync' },
)
const selectedAction = ref<{
  kind: 'channel' | 'entry'
  id: string
  name: string
  key?: string
  mode: 'rename' | 'delete'
} | null>(null)
const actionName = ref('')
function configureAction(
  kind: 'channel' | 'entry',
  row: Channel | ModelEntry,
  mode: 'rename' | 'delete',
) {
  const display = row.name || ('key' in row ? row.key : '')
  selectedAction.value = {
    kind,
    id: row.id,
    name: display,
    key: 'key' in row ? row.key : undefined,
    mode,
  }
  actionName.value = display
  error.value = ''
}
async function submitAction() {
  await run(async () => {
    const action = selectedAction.value
    if (!action || !can('model:manage')) return
    const path = action.kind === 'channel' ? '/model-channels/' : '/model-entries/'
    if (action.mode === 'rename')
      await request(`${path}${action.id}/name`, 'PATCH', { name: actionName.value })
    else await request(`${path}${action.id}`, 'DELETE')
    selectedAction.value = null
    notice.value =
      action.mode === 'rename' ? '名称已修改，模型 key 和流程绑定保持不变。' : '配置已删除。'
    await (action.kind === 'channel' ? channels : entries).reload()
  })
}
async function loadOptions() {
  optionsError.value = ''
  try {
    options.value = await modelApi.options()
  } catch (e) {
    options.value = null
    optionsError.value = e instanceof Error ? e.message : '配置不可用'
  }
}
function channel(row: Channel | null) {
  editingChannel.value = row
  name.value = row?.name ?? ''
  const descriptor = row ? describeChannel(row) : creatableChannels.value[0]
  provider.value = row?.provider ?? descriptor?.id ?? ''
  apiBase.value = row?.api_base ?? descriptor?.default_api_base ?? ''
  channelFields.value = Object.fromEntries(
    (descriptor?.fields ?? []).map((field) => [
      field.name,
      String((row as unknown as Record<string, unknown> | null)?.[field.name] ?? ''),
    ]),
  )
  credential.value = ''
  billing.value = ''
  billingAk.value = billingSk.value = billingToken.value = ''
  clearBilling.value = false
  account.value = row?.upstream_account_ref ?? ''
  active.value = row?.is_active ?? true
  error.value = ''
  form.value = 'channel'
}
function entry(row: ModelEntry | null) {
  editingEntry.value = row
  key.value = row?.key ?? ''
  modelName.value = row?.name ?? ''
  channelId.value = row?.channel_id ?? ''
  upstream.value = row?.provider_model ?? ''
  operations.value = [...(row?.operations ?? ['text.generate'])]
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
        ...Object.fromEntries(
          selectedChannel.value.fields.map((field) => [
            field.name,
            channelFields.value[field.name]?.trim() || null,
          ]),
        ),
        ...(credential.value ? { api_key: credential.value } : {}),
        ...(keyBilling.value && billing.value && !clearBilling.value
          ? { billing_api_key: billing.value }
          : {}),
        ...(cloudBilling.value &&
        !clearBilling.value &&
        (billingAk.value || billingSk.value || billingToken.value)
          ? {
              billing_credentials: {
                access_key_id: billingAk.value,
                access_key_secret: billingSk.value,
                ...(billingToken.value ? { security_token: billingToken.value } : {}),
              },
            }
          : {}),
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
        name: modelName.value.trim() || key.value,
        provider_model: upstream.value,
        operations: operations.value,
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
    const updated = await request<Channel>(`/model-channels/${row.id}/balance`, 'POST')
    notice.value = '余额查询已结束，请查看最新查询状态与成功采集时间。'
    channels.rows.value = channels.rows.value.map((channel) =>
      channel.id === updated.id ? updated : channel,
    )
  })
}
watch(form, (value) => {
  if (value !== 'channel') {
    credential.value = ''
    billing.value = ''
    billingAk.value = billingSk.value = billingToken.value = ''
    clearBilling.value = false
  }
})
watch(provider, () => {
  if (form.value !== 'channel' || editingChannel.value) return
  channelFields.value = {}
  billing.value = billingAk.value = billingSk.value = billingToken.value = ''
  clearBilling.value = false
  apiBase.value =
    options.value?.channels?.find((item) => item.id === provider.value)?.default_api_base ?? ''
})
watch(
  () => selectedChannel.value.billing_credentials,
  () => {
    billing.value = billingAk.value = billingSk.value = billingToken.value = ''
    clearBilling.value = false
  },
)
watch(upstream, (value) => {
  if (form.value !== 'entry' || editingEntry.value || key.value || !value) return
  const suggested = value
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[^a-z]+/, '')
    .slice(0, 64)
  key.value = suggested
})
onMounted(() => {
  if (can('model:manage')) void loadOptions()
})
</script>
<template>
  <section aria-label="模型接入" class="models-page">
    <div v-show="form !== 'entry'" class="heading">
      <h1>模型接入</h1>
      <RouterLink v-if="can('model_invocation:read')" class="usage-entry" to="/model-usage"
        >查看用量与费用 →</RouterLink
      >
    </div>
    <el-alert
      v-if="error && !form && !selectedAction"
      role="alert"
      type="error"
      show-icon
      :closable="false"
      >{{ error }}</el-alert
    >
    <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
      notice
    }}</el-alert>
    <el-alert v-if="optionsError && can('model:manage')" role="alert" type="error" :closable="false"
      >{{ optionsError }}
      <el-button link native-type="button" :disabled="busy" @click="run(loadOptions)"
        >重试加载服务商</el-button
      ></el-alert
    >
    <el-tabs v-show="form !== 'entry'" v-model="configurationTab" class="model-config-tabs">
      <el-tab-pane v-if="can('model:manage')" label="渠道配置" name="channels">
        <section id="configuration-channels" aria-label="渠道账户">
          <div class="heading">
            <h2>渠道账户</h2>
            <div class="row-actions">
              <el-button
                :disabled="busy || !creatableChannels.length"
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
              ref="channelTable"
              native-scrollbar
              :data="channels.rows.value"
              row-key="id"
              stripe
              class="data-table configuration-table"
            >
              <el-table-column label="渠道" min-width="190"
                ><template #default="{ row: row }"
                  ><span class="truncate" :title="row.name">{{ row.name }}</span
                  ><small :title="row.id">{{ row.id.slice(0, 8) }}…</small></template
                ></el-table-column
              >
              <el-table-column label="服务商" min-width="110"
                ><template #default="{ row: row }">{{
                  describeChannel(row).label
                }}</template></el-table-column
              >
              <el-table-column label="状态" min-width="90"
                ><template #default="{ row: row }"
                  ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
                    row.is_active ? '启用' : '停用'
                  }}</StatusTag></template
                ></el-table-column
              >
              <el-table-column label="余额 / Key 额度" min-width="240"
                ><template #default="{ row }"
                  ><ChannelBalance
                    :channel="row"
                    :available="describeChannel(row).balance"
                    :bills-available="describeChannel(row).bills"
                    :busy="busy"
                    :error="error"
                    @inspect="error = ''"
                    @refresh="balance(row)" /></template
              ></el-table-column>
              <el-table-column label="操作" min-width="220"
                ><template #default="{ row: row }"
                  ><div class="configuration-actions">
                    <el-button
                      :disabled="busy || !options"
                      @click="channel(row)"
                      native-type="button"
                      type="primary"
                      link
                      >编辑渠道</el-button
                    ><RouterLink
                      v-if="can('model_invocation:read')"
                      :to="{ path: '/model-usage', query: { channel_id: row.id } }"
                      >查看用量</RouterLink
                    >
                    <RowMore :label="`${row.name || row.key || '配置'}的更多操作`"
                      ><el-button
                        :disabled="busy"
                        native-type="button"
                        type="primary"
                        link
                        @click="configureAction('channel', row, 'rename')"
                        >修改渠道名称</el-button
                      ><el-button
                        :disabled="busy"
                        native-type="button"
                        type="danger"
                        link
                        @click="configureAction('channel', row, 'delete')"
                        >删除渠道</el-button
                      ></RowMore
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
      </el-tab-pane>
      <el-tab-pane v-if="can('model:manage')" label="模型配置" name="models">
        <section id="configuration-models" aria-label="模型配置">
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
              ref="modelTable"
              native-scrollbar
              :data="entries.rows.value"
              row-key="id"
              stripe
              class="data-table configuration-table"
            >
              <el-table-column label="模型名称 / key" min-width="200"
                ><template #default="{ row: row }"
                  ><span class="truncate" :title="row.name || row.key">{{
                    row.name || row.key
                  }}</span
                  ><small
                    v-if="row.name && row.name !== row.key"
                    class="truncate"
                    :title="row.key"
                    >{{ row.key }}</small
                  ></template
                ></el-table-column
              >
              <el-table-column label="所属渠道" min-width="170"
                ><template #default="{ row: row }"
                  ><span
                    class="truncate"
                    :title="
                      channels.rows.value.find((channel) => channel.id === row.channel_id)?.name ??
                      row.channel_id
                    "
                    >{{
                      channels.rows.value.find((channel) => channel.id === row.channel_id)?.name ??
                      `${row.channel_id.slice(0, 8)}…`
                    }}</span
                  ></template
                ></el-table-column
              >
              <el-table-column label="上游模型" min-width="180"
                ><template #default="{ row: row }"
                  ><span class="truncate" :title="row.provider_model">{{
                    row.provider_model
                  }}</span></template
                ></el-table-column
              >
              <el-table-column label="操作能力" min-width="160"
                ><template #default="{ row }">{{
                  (row.operations ?? ['text.generate'])
                    .map((op: ModelOperation) => operationLabels[op])
                    .join('、')
                }}</template></el-table-column
              >
              <el-table-column label="预估价格" min-width="115"
                ><template #default="{ row }"
                  ><el-button
                    type="primary"
                    link
                    native-type="button"
                    @click="pricingEntry = row"
                    >{{
                      row.pricing_mode === 'override'
                        ? '自定义价格'
                        : row.pricing_mode === 'disabled'
                          ? '预估已关闭'
                          : '继承参考价'
                    }}</el-button
                  ></template
                ></el-table-column
              >
              <el-table-column label="状态" min-width="90"
                ><template #default="{ row: row }"
                  ><StatusTag :status="row.is_active ? 'active' : 'disabled'">{{
                    row.is_active ? '启用' : '停用'
                  }}</StatusTag></template
                ></el-table-column
              >
              <el-table-column label="操作" min-width="220"
                ><template #default="{ row: row }"
                  ><div class="configuration-actions">
                    <el-button
                      :disabled="busy"
                      @click="entry(row)"
                      native-type="button"
                      type="primary"
                      link
                      >编辑模型</el-button
                    >
                    <RouterLink
                      v-if="can('model_invocation:read')"
                      :to="{ path: '/model-usage', query: { model_entry_id: row.id } }"
                      >查看用量</RouterLink
                    >
                    <RowMore :label="`${row.name || row.key || '配置'}的更多操作`"
                      ><el-button
                        :disabled="busy"
                        native-type="button"
                        type="primary"
                        link
                        @click="configureAction('entry', row, 'rename')"
                        >修改模型名称</el-button
                      ><el-button
                        :disabled="busy"
                        native-type="button"
                        type="danger"
                        link
                        @click="configureAction('entry', row, 'delete')"
                        >删除模型</el-button
                      ></RowMore
                    >
                  </div>
                </template></el-table-column
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
      </el-tab-pane>
    </el-tabs>
    <AppDialog
      v-if="form"
      :page="form === 'entry'"
      :drawer="form === 'channel'"
      protect-changes
      :dirty="fingerprint !== baseline"
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
      <div :class="{ 'model-editor-layout': form === 'entry' }">
        <form id="model-config-form" class="dialog-form" @submit.prevent="save">
          <template v-if="form === 'channel'"
            ><label>渠道名称<el-input v-model="name" required maxlength="128" /></label>
            <div class="form-field">
              <span class="form-label">服务商</span
              ><AppSelect v-model="provider" aria-label="服务商" :disabled="!!editingChannel"
                ><el-option
                  v-if="editingChannel"
                  :value="provider"
                  :label="selectedChannel.label" /><el-option
                  v-for="descriptor in editingChannel ? [] : creatableChannels"
                  :key="descriptor.id"
                  :value="descriptor.id"
                  :label="resolveChannel({ provider: descriptor.id, api_base: '' }, options).label"
              /></AppSelect>
            </div>
            <label
              >服务地址<el-input
                v-model="apiBase"
                type="url"
                required
                maxlength="512"
                :readonly="!selectedChannel.custom_base"
            /></label>
            <template v-for="field in selectedChannel.fields" :key="field.name">
              <label
                >{{ field.label
                }}<el-input
                  v-model="channelFields[field.name]"
                  :aria-label="field.label"
                  :pattern="field.pattern"
                  :placeholder="field.placeholder"
              /></label>
              <p v-if="field.hint">{{ field.hint }}</p>
            </template>
            <label
              >推理 API Key<el-input
                v-model="credential"
                type="password"
                autocomplete="new-password"
                :required="!editingChannel?.has_api_key"
                maxlength="4096"
                :placeholder="editingChannel?.has_api_key ? '已设置，留空保留' : '请输入 API Key'"
            /></label>
            <p v-if="selectedChannel.hint">{{ selectedChannel.hint }}</p>
            <label v-if="keyBilling"
              >账务 API Key<el-input
                v-model="billing"
                type="password"
                autocomplete="new-password"
                maxlength="4096"
                :disabled="clearBilling"
                :placeholder="
                  editingChannel?.has_billing_api_key ? '已设置，留空保留' : '可选账务 API Key'
                "
            /></label>
            <template v-if="cloudBilling">
              <p>
                余额和月账单使用独立的云账务 AccessKey，推理 API Key
                无法查询。留空保留已有凭据，替换时同时填写 ID 和 Secret。
              </p>
              <label
                >账务 AccessKey ID<el-input
                  v-model="billingAk"
                  aria-label="账务 AccessKey ID"
                  type="password"
                  autocomplete="new-password"
                  maxlength="256"
                  :disabled="clearBilling"
                  :required="!clearBilling && !!(billingSk || billingToken)"
                  :placeholder="
                    editingChannel?.has_billing_api_key
                      ? '已设置，留空保留'
                      : '可选，开通账务查询时填写'
                  "
              /></label>
              <label
                >账务 AccessKey Secret<el-input
                  v-model="billingSk"
                  aria-label="账务 AccessKey Secret"
                  type="password"
                  autocomplete="new-password"
                  maxlength="1024"
                  :disabled="clearBilling"
                  :required="!clearBilling && !!(billingAk || billingToken)"
              /></label>
              <label
                >账务 STS Token（可选）<el-input
                  v-model="billingToken"
                  aria-label="账务 STS Token"
                  type="password"
                  autocomplete="new-password"
                  maxlength="4096"
                  :disabled="clearBilling"
              /></label>
              <p>
                只授予余额和账单查询权限。STS
                凭据到期后须重新填写完整凭据；账户余额包含该云账户下其他云产品。
              </p>
            </template>
            <el-checkbox
              v-if="
                editingChannel?.has_billing_api_key &&
                selectedChannel.billing_credentials !== 'none'
              "
              v-model="clearBilling"
              >清除已保存的账务 Key</el-checkbox
            >
            <label
              >账户标识（非秘密）<el-input
                v-model="account"
                maxlength="128"
                :disabled="!!editingChannel?.upstream_account_ref"
            /></label>
            <p>
              密钥保存后不回显，编辑时留空保留原值。已确定的账户标识不能更换，换账户须新建渠道。
            </p>
          </template>
          <template v-else
            ><fieldset class="form-section">
              <legend>模型来源</legend>
              <ChannelPicker v-model="channelId" :disabled="busy" />
              <UpstreamModelPicker
                class="full-field"
                v-model="upstream"
                :channel-id="channelId"
                :disabled="busy"
              />
            </fieldset>
            <fieldset class="form-section">
              <legend>基本信息</legend>
              <label
                >模型 key<el-input
                  v-model="key"
                  :disabled="!!editingEntry"
                  required
                  pattern="[a-z][a-z0-9_-]{0,63}" /></label
              ><label
                >模型显示名称<el-input
                  v-model="modelName"
                  maxlength="128"
                  placeholder="留空使用模型 key"
              /></label>
            </fieldset>
            <fieldset class="form-section">
              <legend>模型操作能力</legend>
              <el-checkbox-group v-model="operations" aria-label="模型操作能力">
                <el-checkbox v-for="(label, op) in operationLabels" :key="op" :value="op">{{
                  label
                }}</el-checkbox>
              </el-checkbox-group>
              <p>按上游模型实际能力配置，可用目录还会核验渠道协议支持范围。</p>
            </fieldset>
          </template>
          <el-checkbox class="check-option" v-model="active">启用配置</el-checkbox>
          <p>
            显示名称可修改，服务商和模型 key
            创建后固定。未被引用的配置可删除，有调用记录或流程引用时只能停用。
          </p>
          <div v-if="form === 'channel'" class="form-actions">
            <el-button :disabled="busy" native-type="submit" type="primary">保存渠道</el-button>
          </div>
        </form>
        <aside v-if="form === 'entry'" class="model-preview" aria-label="模型配置预览">
          <h2>配置预览</h2>
          <h3>{{ modelName || key || '尚未命名的模型' }}</h3>
          <p>
            所属渠道：{{
              channels.rows.value.find((item) => item.id === channelId)?.name || '请选择渠道'
            }}
          </p>
          <p>上游模型：{{ upstream || '请选择上游模型' }}</p>
          <p>操作能力：{{ operations.map((op) => operationLabels[op]).join('、') || '未选择' }}</p>
          <p>模型 key：{{ key || '尚未填写' }}</p>
          <StatusTag :status="active ? 'active' : 'disabled'">{{
            active ? '保存后启用' : '保存后停用'
          }}</StatusTag>
        </aside>
      </div>
      <template #footer="{ close }"
        ><el-button :disabled="busy" @click="close">取消</el-button
        ><el-button form="model-config-form" :disabled="busy" native-type="submit" type="primary">{{
          editingEntry ? '保存模型' : '创建模型'
        }}</el-button></template
      >
    </AppDialog>
    <AppDialog
      v-if="selectedAction"
      :protect-changes="selectedAction.mode === 'rename'"
      :title="selectedAction.mode === 'rename' ? '修改名称' : '删除配置'"
      :busy="busy"
      @close="
        () => {
          selectedAction = null
          error = ''
        }
      "
    >
      <el-alert v-if="error" type="error" role="alert" :closable="false">{{ error }}</el-alert>
      <form class="dialog-form" @submit.prevent="submitAction">
        <p>
          {{ selectedAction.name
          }}<span v-if="selectedAction.key">（{{ selectedAction.key }}）</span>
        </p>
        <label v-if="selectedAction.mode === 'rename'"
          >显示名称<el-input v-model="actionName" required maxlength="128"
        /></label>
        <p v-else>
          确认删除此配置？删除后不可恢复。存在模型配置、调用记录或流程引用时会拒绝删除，可改为停用。
        </p>
        <el-button
          :disabled="busy"
          native-type="submit"
          :type="selectedAction.mode === 'delete' ? 'danger' : 'primary'"
          >{{ selectedAction.mode === 'rename' ? '保存名称' : '确认删除' }}</el-button
        >
      </form>
    </AppDialog>
  </section>
  <PricingDialog
    v-if="pricingEntry"
    :entry="pricingEntry"
    @close="pricingEntry = null"
    @saved="entries.reload()"
  />
</template>

<style scoped>
.models-page > .heading {
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 20px;
}
.usage-entry {
  color: var(--el-color-primary);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}
.model-config-tabs :deep(.el-tabs__header) {
  margin-bottom: 22px;
}
.model-config-tabs :deep(.el-tabs__item) {
  height: 44px;
}
.configuration-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: start;
  gap: 8px 14px;
}
.configuration-actions :deep(.el-button) {
  margin-left: 0;
  justify-self: start;
}
.configuration-actions a {
  color: var(--el-color-primary);
  text-decoration: none;
  font-size: 12px;
  line-height: 24px;
}
.configuration-table :deep(td.el-table__cell) {
  vertical-align: top;
  padding-top: 12px;
  padding-bottom: 12px;
}
.configuration-table :deep(.cell > small) {
  white-space: nowrap;
}
.truncate {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 600px) {
  .models-page > .heading {
    align-items: flex-start;
    gap: 8px;
  }
}
</style>
