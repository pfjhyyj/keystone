<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteUpdate } from 'vue-router'
import { request } from '../../api/client'
import type { PersonalToken } from '../../api/client'
import {
  changePassword as updatePassword,
  identity,
  sessionError,
  sessionVersion,
} from '../../stores/session'
import { useAction } from '../../composables/useAction'
import { ElMessageBox } from 'element-plus'
import PermissionPicker from '../roles/components/PermissionPicker.vue'
const route = useRoute(),
  router = useRouter()
const { busy, error, run } = useAction()
const creating = ref(false)
const tokenCatalog = computed(() =>
  (identity.value?.permissions ?? []).map((key) => ({ key, name: key, description: '' })),
)
const currentPassword = ref(''),
  newPassword = ref(''),
  confirmation = ref('')
const tokens = ref<PersonalToken[]>([]),
  tokenName = ref(''),
  tokenDays = ref(30),
  selected = ref<string[]>([]),
  revealed = ref('')
const forced = computed(() => !!identity.value?.user.password_change_required)
const active = computed(() =>
  forced.value
    ? 'password'
    : ['profile', 'password', 'tokens'].includes(String(route.query.tab))
      ? String(route.query.tab)
      : 'profile',
)
function clearForms() {
  creating.value = false
  tokens.value = []
  revealed.value = ''
  selected.value = []
  currentPassword.value = ''
  newPassword.value = ''
  confirmation.value = ''
}
async function changePassword() {
  await run(async () => {
    try {
      if (newPassword.value !== confirmation.value) throw new Error('两次新密码不一致')
      await updatePassword(currentPassword.value, newPassword.value)
    } finally {
      currentPassword.value = ''
      newPassword.value = ''
      confirmation.value = ''
    }
  })
}
async function loadTokens() {
  await run(async () => {
    tokens.value = await request<PersonalToken[]>('/auth/tokens')
  })
}
async function createToken() {
  await run(async () => {
    revealed.value = ''
    const created = await request<PersonalToken>('/auth/tokens', 'POST', {
      name: tokenName.value,
      permissions: selected.value,
      expires_at: new Date(Date.now() + tokenDays.value * 86400000).toISOString(),
    })
    revealed.value = created.token ?? ''
    tokenName.value = ''
    selected.value = []
    tokens.value = await request<PersonalToken[]>('/auth/tokens')
  })
}
async function revokeToken(id: string) {
  const token = tokens.value.find((item) => item.id === id)
  if (!token || busy.value) return
  try {
    await ElMessageBox.confirm(
      `撤销“${token.name}”后，使用该令牌的脚本将无法继续访问。`,
      '撤销个人令牌？',
      { confirmButtonText: '确认撤销令牌', cancelButtonText: '保留令牌', type: 'warning' },
    )
  } catch {
    return
  }
  await run(async () => {
    await request(`/auth/tokens/${id}`, 'DELETE')
    revealed.value = ''
    tokens.value = await request<PersonalToken[]>('/auth/tokens')
  })
}
function tab(value: string | number) {
  if (!busy.value) void router.replace({ path: '/account', query: { tab: String(value) } })
}
watch(
  active,
  (value) => {
    clearForms()
    error.value = ''
    if (value === 'tokens') void loadTokens()
  },
  { immediate: true },
)
watch(sessionVersion, clearForms, { flush: 'sync' })
onBeforeRouteUpdate(() => !busy.value)
onBeforeUnmount(clearForms)
</script>
<template>
  <section v-if="identity" class="account-page" aria-label="账户">
    <div class="heading">
      <div>
        <h1>个人中心</h1>
        <p>管理你的账户安全与个人 API 令牌。</p>
      </div>
      <el-tag disable-transitions effect="plain">{{
        identity.is_administrator ? '内置管理员' : '平台账户'
      }}</el-tag>
    </div>
    <el-alert v-if="error || sessionError" type="error" :closable="false" show-icon>{{
      error || sessionError
    }}</el-alert>
    <el-alert v-if="forced" type="warning" :closable="false" show-icon
      >首次使用前需要修改初始密码。</el-alert
    >
    <el-tabs :model-value="active" :before-leave="() => !busy" @tab-change="tab">
      <el-tab-pane label="账户信息" name="profile" :disabled="forced">
        <div class="profile-summary">
          <el-avatar :size="64">{{ identity.user.display_name.slice(0, 1) }}</el-avatar>
          <div>
            <h2>{{ identity.user.display_name }}</h2>
            <p>{{ identity.user.username }}</p>
          </div>
        </div>
        <el-descriptions :column="1" border
          ><el-descriptions-item label="用户 ID"
            ><code>{{ identity.user.id }}</code></el-descriptions-item
          ><el-descriptions-item label="登录名">{{ identity.user.username }}</el-descriptions-item
          ><el-descriptions-item label="账户类型">{{
            identity.is_administrator ? '内置管理员' : '平台账户'
          }}</el-descriptions-item
          ><el-descriptions-item label="当前权限"
            >{{ identity.permissions.length }} 项功能权限</el-descriptions-item
          ></el-descriptions
        >
      </el-tab-pane>
      <el-tab-pane label="修改密码" name="password">
        <div class="account-section">
          <h2 v-if="forced">首次使用，请先修改密码</h2>
          <p>修改后需重新登录。个人令牌独立管理，不随改密撤销。</p>
          <form class="password-form" @submit.prevent="changePassword">
            <label
              >当前密码<el-input
                v-model="currentPassword"
                type="password"
                autocomplete="current-password"
                required
                maxlength="128"
                show-password /></label
            ><label
              >新密码（8–128 个字符）<el-input
                v-model="newPassword"
                type="password"
                autocomplete="new-password"
                required
                minlength="8"
                maxlength="128"
                show-password /></label
            ><label
              >确认新密码<el-input
                v-model="confirmation"
                type="password"
                autocomplete="new-password"
                required
                minlength="8"
                maxlength="128"
                show-password /></label
            ><el-button type="primary" native-type="submit" :loading="busy">修改并退出</el-button>
          </form>
        </div>
      </el-tab-pane>
      <el-tab-pane label="个人令牌" name="tokens" :disabled="forced">
        <div class="account-section">
          <div class="heading">
            <h2>个人 API 令牌</h2>
            <el-button type="primary" :disabled="busy" @click="creating = !creating">{{
              creating ? '收起签发表单' : '新建令牌'
            }}</el-button>
          </div>
          <p>仅代表你本人，权限始终受当前账户权限和所选范围限制。</p>
          <form v-if="creating" class="token-form" @submit.prevent="createToken">
            <div class="token-fields">
              <label
                >用途名称<el-input
                  v-model="tokenName"
                  required
                  maxlength="128"
                  placeholder="例如：本地脚本" /></label
              ><label
                >有效天数<el-input
                  v-model.number="tokenDays"
                  type="number"
                  required
                  min="1"
                  max="90"
              /></label>
            </div>
            <fieldset>
              <legend>允许的权限</legend>
              <PermissionPicker v-model="selected" :catalog="tokenCatalog" :disabled="busy" />
              <p v-if="!identity.permissions.length">当前账户没有功能权限，可签发空权限令牌。</p>
            </fieldset>
            <el-button type="primary" native-type="submit" :loading="busy">签发令牌</el-button>
          </form>
          <el-alert v-if="revealed" type="warning" :closable="false" show-icon class="token-secret"
            ><strong>请现在保存，关闭后无法再次查看。</strong
            ><el-input
              type="textarea"
              :model-value="revealed"
              readonly
              aria-label="新令牌明文"
              :rows="3"
            /><el-button @click="revealed = ''">我已保存，关闭明文</el-button></el-alert
          >
          <el-table
            :fit="false"
            native-scrollbar
            table-layout="auto"
            :data="tokens"
            row-key="id"
            stripe
            class="token-list"
            ><el-table-column prop="name" label="用途名称" min-width="160" /><el-table-column
              label="状态"
              width="100"
              ><template #default="{ row }"
                ><StatusTag
                  :status="
                    row.revoked_at || new Date(row.expires_at).getTime() <= Date.now()
                      ? 'disabled'
                      : 'active'
                  "
                  >{{
                    row.revoked_at
                      ? '已撤销'
                      : new Date(row.expires_at).getTime() <= Date.now()
                        ? '已到期'
                        : '有效'
                  }}</StatusTag
                ></template
              ></el-table-column
            ><el-table-column label="到期时间" min-width="180"
              ><template #default="{ row }">{{
                new Date(row.expires_at).toLocaleString()
              }}</template></el-table-column
            ><el-table-column label="权限范围" min-width="240"
              ><template #default="{ row }">{{
                row.permissions.join('、') || '无功能权限'
              }}</template></el-table-column
            ><el-table-column label="操作" width="90"
              ><template #default="{ row }"
                ><el-button
                  link
                  type="danger"
                  :disabled="busy || !!row.revoked_at"
                  @click="revokeToken(row.id)"
                  >撤销</el-button
                ></template
              ></el-table-column
            ><template #empty><el-empty description="尚无个人令牌。" :image-size="64" /></template
          ></el-table>
          <p v-if="tokens.length === 50">此处显示最近 50 条；完整分页查询由 API 提供。</p>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>
