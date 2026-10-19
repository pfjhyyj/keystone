<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, request, setCsrf } from './auth'
import type { PersonalToken } from './auth'
import { clearSession, ensureSession, identity, initialized, refreshSession, sessionError, sessionVersion } from './access'

const loading = computed(() => !initialized.value)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const username = ref('')
const password = ref('')
const currentPassword = ref('')
const newPassword = ref('')
const confirmation = ref('')
const changing = ref(false)
const tokens = ref<PersonalToken[]>([])
const showTokens = ref(false)
const tokenName = ref('')
const tokenDays = ref(30)
const selected = ref<string[]>([])
const revealed = ref('')

function clearIdentity() {
  clearSession()
  clearForms()
}
function clearForms() {
  tokens.value = []
  revealed.value = ''
  selected.value = []
  showTokens.value = false
  changing.value = false
  password.value = ''
  currentPassword.value = ''
  newPassword.value = ''
  confirmation.value = ''
}

async function refreshIdentity() {
  await refreshSession()
}

async function action(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try { await work() }
  catch (e) {
    if (e instanceof ApiError && e.status === 401) clearIdentity()
    if (e instanceof ApiError && e.code === 'password_change_required') {
      revealed.value = ''
      showTokens.value = false
      await refreshIdentity().catch(clearIdentity)
    }
    error.value = e instanceof Error ? e.message : '操作失败，请重试'
  } finally { busy.value = false }
}

async function login() {
  await action(async () => {
    try {
      const result = await request<{ csrf_token: string }>('/auth/login', 'POST', {
        username: username.value, password: password.value,
      })
      setCsrf(result.csrf_token)
      await refreshIdentity()
    } finally { password.value = '' }
  })
}

async function logout() {
  await action(async () => { await request('/auth/logout', 'POST'); clearIdentity() })
}

async function changePassword() {
  await action(async () => {
    if (newPassword.value !== confirmation.value) throw new Error('两次新密码不一致')
    try {
      await request('/auth/password', 'POST', {
        current_password: currentPassword.value, password: newPassword.value,
      })
      clearIdentity()
      notice.value = '密码已修改，所有登录会话已撤销，请重新登录。'
    } finally { currentPassword.value = ''; newPassword.value = ''; confirmation.value = '' }
  })
}

async function loadTokens() {
  await action(async () => { tokens.value = await request<PersonalToken[]>('/auth/tokens'); showTokens.value = true })
}

async function createToken() {
  await action(async () => {
    revealed.value = ''
    const expires = new Date(Date.now() + tokenDays.value * 86400000).toISOString()
    const created = await request<PersonalToken>('/auth/tokens', 'POST', {
      name: tokenName.value, permissions: selected.value, expires_at: expires,
    })
    revealed.value = created.token ?? ''
    tokenName.value = ''
    selected.value = []
    tokens.value = await request<PersonalToken[]>('/auth/tokens')
  })
}

async function revokeToken(id: string) {
  await action(async () => {
    await request(`/auth/tokens/${id}`, 'DELETE')
    revealed.value = ''
    tokens.value = await request<PersonalToken[]>('/auth/tokens')
  })
}

watch(sessionVersion, clearForms, { flush: 'sync' })
onBeforeUnmount(clearForms)
onMounted(ensureSession)
</script>

<template>
  <section class="identity-panel" aria-label="账户">
    <p v-if="loading" role="status">正在确认登录状态…</p>
    <div v-else-if="!identity" class="login-box">
      <span class="eyebrow">KEYSTONE / ACCOUNT</span>
      <h1>登录管理后台</h1>
      <p>使用管理员为你创建的账户登录。</p>
      <form @submit.prevent="login">
        <label>登录名<input v-model="username" name="username" autocomplete="username" required maxlength="64"></label>
        <label>密码<input v-model="password" name="password" type="password" autocomplete="current-password" required maxlength="128"></label>
        <button :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
      </form>
    </div>
    <div v-else>
      <div class="account-heading">
        <div><span class="eyebrow">已登录</span><h2>{{ identity.user.display_name }}</h2><p>{{ identity.user.username }} · {{ identity.is_administrator ? '内置管理员' : '平台账户' }}</p></div>
        <div class="account-actions">
          <button v-if="!identity.user.password_change_required" class="secondary" :disabled="busy" @click="changing = !changing">修改密码</button>
          <button v-if="!identity.user.password_change_required" class="secondary" :disabled="busy" @click="loadTokens">个人令牌</button>
          <button :disabled="busy" @click="logout">退出登录</button>
        </div>
      </div>
      <form v-if="changing || identity.user.password_change_required" class="password-form" @submit.prevent="changePassword">
        <h3>{{ identity.user.password_change_required ? '首次使用，请先修改密码' : '修改密码' }}</h3>
        <p>修改后需重新登录。个人令牌独立管理，不随改密撤销。</p>
        <label>当前密码<input v-model="currentPassword" type="password" autocomplete="current-password" required maxlength="128"></label>
        <label>新密码（8–128 个字符）<input v-model="newPassword" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label>
        <label>确认新密码<input v-model="confirmation" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label>
        <button :disabled="busy">修改并退出</button>
      </form>
      <section v-if="showTokens && !identity.user.password_change_required" class="tokens-panel">
        <h3>个人 API 令牌</h3><p>仅代表你本人，权限始终受当前账户权限和所选范围限制。</p>
        <form @submit.prevent="createToken">
          <div class="token-fields">
            <label>用途名称<input v-model="tokenName" required maxlength="128"></label>
            <label>有效天数<input v-model.number="tokenDays" type="number" required min="1" max="90"></label>
          </div>
          <fieldset><legend>允许的权限</legend><label v-for="permission in identity.permissions" :key="permission" class="permission-option"><input v-model="selected" type="checkbox" :value="permission">{{ permission }}</label><p v-if="!identity.permissions.length">当前账户没有功能权限，可签发空权限令牌。</p></fieldset>
          <button :disabled="busy">签发令牌</button>
        </form>
        <div v-if="revealed" class="token-secret"><strong>请现在保存，关闭后无法再次查看。</strong><textarea :value="revealed" readonly aria-label="新令牌明文"></textarea><button class="secondary" @click="revealed = ''">我已保存，关闭明文</button></div>
        <ul class="token-list"><li v-for="item in tokens" :key="item.id"><div><strong>{{ item.name }}</strong><p>{{ item.revoked_at ? '已撤销' : new Date(item.expires_at).getTime() <= Date.now() ? '已到期' : '有效' }} · 到期 {{ new Date(item.expires_at).toLocaleString() }}</p><small>{{ item.permissions.join('、') || '无功能权限' }}</small></div><button class="secondary" :disabled="busy || !!item.revoked_at" @click="revokeToken(item.id)">撤销</button></li></ul>
        <p v-if="!tokens.length">尚无个人令牌。</p><p v-if="tokens.length === 50">此处显示最近 50 条；完整分页查询由 API 提供。</p>
      </section>
    </div>
    <p v-if="error" class="notice" role="alert">{{ error }}</p>
    <p v-if="sessionError" class="notice" role="alert">{{ sessionError }} <button :disabled="busy" @click="refreshIdentity">重新确认</button></p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
  <slot v-if="identity && !identity.user.password_change_required" :identity="identity" />
</template>
