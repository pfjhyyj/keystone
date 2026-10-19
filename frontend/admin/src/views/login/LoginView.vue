<script setup lang="ts">
import { ref } from 'vue'
import { request, setCsrf } from '../../api/client'
import { refreshSession, sessionError } from '../../stores/session'
const username = ref(''),
  password = ref(''),
  busy = ref(false),
  error = ref('')
async function login() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await request<{ csrf_token: string }>('/auth/login', 'POST', {
      username: username.value,
      password: password.value,
    })
    setCsrf(result.csrf_token)
    await refreshSession()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '登录失败，请重试'
  } finally {
    password.value = ''
    busy.value = false
  }
}
</script>
<template>
  <main class="login-page">
    <section class="login-intro">
      <div class="login-brand"><span class="brand-mark">K</span>keystone</div>
      <div>
        <span class="login-kicker">PLATFORM CONSOLE</span>
        <h2>统一的管理工作台</h2>
        <p>在一个入口管理用户、平台资源与 AI 流程。</p>
      </div>
      <small>Keystone Platform</small>
    </section>
    <section class="login-card" aria-label="账户">
      <span class="login-mobile-brand">keystone</span>
      <p class="login-welcome">欢迎回来</p>
      <h1>登录管理后台</h1>
      <p class="login-subtitle">使用管理员为你创建的账户登录。</p>
      <el-alert v-if="error || sessionError" type="error" show-icon :closable="false">{{
        error || sessionError
      }}</el-alert>
      <form class="login-form" @submit.prevent="login">
        <label
          >登录名<el-input
            v-model="username"
            name="username"
            autocomplete="username"
            placeholder="请输入登录名"
            size="large"
            required
            maxlength="64" /></label
        ><label
          >密码<el-input
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            size="large"
            required
            maxlength="128"
            show-password /></label
        ><el-button type="primary" native-type="submit" size="large" :loading="busy">{{
          busy ? '正在登录…' : '登录'
        }}</el-button>
      </form>
      <p class="login-help">账户由管理员分配。忘记密码请联系管理员。</p>
    </section>
  </main>
</template>
