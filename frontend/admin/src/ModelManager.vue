<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { can } from './access'
import { request } from './auth'
import { modelApi, providers, balanceStates, type Channel, type ModelEntry, type ModelOptions } from './model-api'
import { usePagedList } from './composables/usePagedList'
import { useAction } from './composables/useAction'
import AppDialog from './components/AppDialog.vue'
import ListPager from './components/ListPager.vue'
import ChannelPicker from './components/ChannelPicker.vue'
import ModelProbe from './components/ModelProbe.vue'
import InvocationHistory from './components/InvocationHistory.vue'
const channels = usePagedList('model:manage', modelApi.channels), entries = usePagedList('model:manage', modelApi.entries)
const { busy, error, notice, run } = useAction()
const options = ref<ModelOptions | null>(null), optionsError = ref(''), historyVersion = ref(0)
const form = ref<'channel' | 'entry' | null>(null), editingChannel = ref<Channel | null>(null), editingEntry = ref<ModelEntry | null>(null)
const name = ref(''), provider = ref('openrouter'), apiBase = ref(''), credential = ref(''), billing = ref(''), account = ref(''), active = ref(true)
const key = ref(''), channelId = ref(''), upstream = ref('')
async function loadOptions() { optionsError.value = ''; try { options.value = await request<ModelOptions>('/model-access/options') } catch (e) { options.value = null; optionsError.value = e instanceof Error ? e.message : '配置不可用' } }
function channel(row: Channel | null) { editingChannel.value = row; name.value = row?.name ?? ''; provider.value = row?.provider ?? 'openrouter'; apiBase.value = row?.api_base ?? (options.value?.mode === 'mock' ? 'https://mock.invalid/v1' : 'https://openrouter.ai/api/v1'); credential.value = row?.credential_ref ?? ''; billing.value = row?.billing_credential_ref ?? ''; account.value = row?.upstream_account_ref ?? ''; active.value = row?.is_active ?? true; error.value = ''; form.value = 'channel' }
function entry(row: ModelEntry | null) { editingEntry.value = row; key.value = row?.key ?? ''; channelId.value = row?.channel_id ?? ''; upstream.value = row?.provider_model ?? (options.value?.mode === 'mock' ? 'mock-chat' : ''); active.value = row?.is_active ?? true; error.value = ''; form.value = 'entry' }
async function save() { await run(async () => {
  if (!can('model:manage')) return
  if (form.value === 'channel') {
    const body = { name: name.value, api_base: apiBase.value, credential_ref: credential.value, billing_credential_ref: billing.value || null, upstream_account_ref: account.value || null, is_active: active.value }
    if (editingChannel.value) await request(`/model-channels/${editingChannel.value.id}`, 'PATCH', body)
    else await request('/model-channels', 'POST', { ...body, provider: provider.value })
    channels.search()
  } else {
    const body = { channel_id: channelId.value, provider_model: upstream.value, is_active: active.value }
    if (editingEntry.value) await request(`/model-entries/${editingEntry.value.id}`, 'PATCH', body)
    else await request('/model-entries', 'POST', { ...body, key: key.value })
    entries.search()
  }
  form.value = null; notice.value = '配置已保存；可用模型选择器可手动刷新。'
}) }
async function balance(row: Channel) { await run(async () => { await request(`/model-channels/${row.id}/balance`, 'POST'); notice.value = '余额查询已结束，请查看最新查询状态与成功采集时间。'; await channels.reload() }) }
onMounted(() => { if (can('model:manage')) void loadOptions() })
</script>
<template>
  <section aria-label="模型接入"><div class="heading"><div><p class="eyebrow">PLATFORM / MODELS</p><h1>模型接入</h1><p>管理渠道、模型与每次调用的费用。发送模式由部署配置决定，未知费用不代表零费用。</p></div></div>
    <p v-if="error && !form" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="can('model:manage')">
      <p v-if="optionsError" role="alert">{{ optionsError }}</p><div class="row-actions"><span>发送模式：{{ options?.mode === 'live' ? '真实发送（仅 OpenRouter）' : options?.mode === 'mock' ? '模拟' : options?.mode === 'disabled' ? '停用' : '尚未确认' }}</span><button class="secondary" :disabled="busy" @click="run(loadOptions)">刷新部署选项</button></div>
      <section aria-label="渠道账户"><div class="heading"><h2>渠道账户</h2><div class="row-actions"><button :disabled="busy || !options" @click="channel(null)">新建渠道</button><button class="secondary" :disabled="busy || channels.loading.value" @click="channels.reload">刷新渠道</button></div></div>
        <p v-if="channels.error.value" role="alert">{{ channels.error.value }}</p><p v-if="channels.loading.value" role="status">正在加载渠道…</p>
        <div class="table-scroll"><table><thead><tr><th>渠道</th><th>服务商</th><th>状态</th><th>最近成功余额</th><th>操作</th></tr></thead><tbody><tr v-for="row in channels.rows.value" :key="row.id"><td>{{ row.name }}<small class="cell-description">{{ row.id }}</small></td><td>{{ providers[row.provider as keyof typeof providers] ?? row.provider }}</td><td>{{ row.is_active ? '启用' : '停用' }}</td><td><p v-for="item in row.balance_snapshot?.items" :key="item.kind + item.unit">{{ item.kind }}：{{ item.amount }} {{ item.unit }}</p><p v-if="!row.balance_snapshot">未知，不代表零余额</p><small>查询状态：{{ balanceStates[row.balance_check_status] }}<br>最近尝试：{{ row.balance_last_attempt_at ? new Date(row.balance_last_attempt_at).toLocaleString() : '无' }}<br>成功采集：{{ row.balance_checked_at ? new Date(row.balance_checked_at).toLocaleString() : '无' }}</small><p v-if="row.balance_snapshot && row.balance_check_status !== 'ok'">这是上次成功结果，不代表当前余额。</p></td><td><div class="row-actions"><button :disabled="busy || !options" @click="channel(row)">编辑渠道</button><button :disabled="busy" @click="balance(row)">刷新余额</button></div></td></tr><tr v-if="!channels.loading.value && !channels.error.value && !channels.rows.value.length"><td colspan="5">暂无渠道</td></tr></tbody></table></div>
        <ListPager :page="channels.page.value" :count="channels.rows.value.length" :loading="busy || channels.loading.value" @change="channels.page.value = $event" />
      </section>
      <section aria-label="模型配置"><div class="heading"><h2>模型配置</h2><div class="row-actions"><button :disabled="busy" @click="entry(null)">新建模型</button><button class="secondary" :disabled="busy || entries.loading.value" @click="entries.reload">刷新模型配置</button></div></div>
        <p v-if="entries.error.value" role="alert">{{ entries.error.value }}</p><p v-if="entries.loading.value" role="status">正在加载模型…</p>
        <div class="table-scroll"><table><thead><tr><th>模型 key</th><th>渠道 ID</th><th>上游模型</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="row in entries.rows.value" :key="row.id"><td>{{ row.key }}</td><td>{{ row.channel_id }}</td><td>{{ row.provider_model }}</td><td>{{ row.is_active ? '启用' : '停用' }}</td><td><button :disabled="busy" @click="entry(row)">编辑模型</button></td></tr><tr v-if="!entries.loading.value && !entries.error.value && !entries.rows.value.length"><td colspan="5">暂无模型配置</td></tr></tbody></table></div>
        <ListPager :page="entries.page.value" :count="entries.rows.value.length" :loading="busy || entries.loading.value" @change="entries.page.value = $event" />
      </section>
    </template>
    <ModelProbe v-if="can('model:invoke')" @completed="historyVersion++" />
    <InvocationHistory v-if="can('model_invocation:read')" :version="historyVersion" />
    <AppDialog v-if="form" :title="form === 'channel' ? (editingChannel ? '编辑渠道' : '新建渠道') : (editingEntry ? '编辑模型' : '新建模型')" :busy="busy" @close="form = null; error = ''">
      <p v-if="error" role="alert">{{ error }}</p><form class="dialog-form" @submit.prevent="save">
        <template v-if="form === 'channel'"><label>渠道名称<input v-model="name" required maxlength="128"></label><label>服务商<select v-model="provider" aria-label="服务商" :disabled="!!editingChannel"><option v-for="(label, value) in providers" :key="value" :value="value">{{ label }}</option></select></label><label>服务地址<input v-model="apiBase" type="url" required maxlength="512"></label>
          <label>推理秘密引用<select v-model="credential" aria-label="推理秘密引用" required><option value="">请选择</option><option v-if="credential && !options?.credential_refs.includes(credential)" :value="credential">{{ credential }}（部署中缺失）</option><option v-for="r in options?.credential_refs" :key="r">{{ r }}</option></select></label>
          <p v-if="options?.mode === 'live'">OpenRouter 地址固定为 https://openrouter.ai/api/v1。推理使用普通 API Key，账户余额查询使用同一账户的 Management Key；账务未配置不影响推理。</p><label>账务秘密引用<select v-model="billing" aria-label="账务秘密引用"><option value="">未配置</option><option v-if="billing && !options?.credential_refs.includes(billing)" :value="billing">{{ billing }}（部署中缺失）</option><option v-for="r in options?.credential_refs" :key="r">{{ r }}</option></select></label><label>账户标识（非秘密）<input v-model="account" maxlength="128" :disabled="!!editingChannel?.upstream_account_ref"></label><p>这里只保存引用名，实际密钥由部署配置提供，不在浏览器中录入或显示。已确定的账户标识不能更换，换账户须新建渠道。</p>
        </template>
        <template v-else><label>模型 key<input v-model="key" :disabled="!!editingEntry" required pattern="[a-z][a-z0-9_-]{0,63}"></label><ChannelPicker v-model="channelId" :disabled="busy" /><label>上游模型<input v-model="upstream" required maxlength="128"></label></template>
        <label class="check-option"><input v-model="active" type="checkbox">启用配置</label><p>配置只停用，不删除。服务商和模型 key 创建后不可修改。</p><button :disabled="busy">{{ form === 'channel' ? '保存渠道' : '保存模型' }}</button>
      </form>
    </AppDialog>
  </section>
</template>
