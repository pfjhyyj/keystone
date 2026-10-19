<script setup lang="ts">
import { computed, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import AuthPanel from './AuthPanel.vue'
import { can, checking, identity, initialized, sessionVersion } from './access'
import { loginDestination, navigation } from './router'
const route = useRoute(), router = useRouter()
const visible = computed(() => navigation.filter(item => !item.permissions.length || item.permissions.some(can)))
const allowed = computed(() => {
  const item = navigation.find(item => item.path === route.path)
  return !item || !item.permissions.length || item.permissions.some(can)
})
watch([() => identity.value?.user.id, () => identity.value?.user.password_change_required, initialized], () => {
  if (!initialized.value) return
  if (!identity.value && route.path !== '/login') void router.replace({ path: '/login', query: { next: route.fullPath } })
  else if (identity.value?.user.password_change_required && route.path !== '/account') void router.replace('/account')
  else if (identity.value && !identity.value.user.password_change_required && route.path === '/login') void router.replace(loginDestination(route.query.next))
})
</script>
<template>
  <div class="shell">
    <header class="topbar"><RouterLink class="brand" to="/" aria-label="Keystone 首页"><span class="mark">K</span>keystone</RouterLink><span class="top-label">管理后台</span></header>
    <AuthPanel><main>
      <nav class="module-tabs" aria-label="平台模块"><RouterLink v-for="item in visible" :key="item.path" :to="item.path" :aria-current="route.path === item.path ? 'page' : undefined">{{ item.label }}</RouterLink></nav>
      <p v-if="checking" role="status">正在确认当前权限…</p>
      <section v-else-if="!allowed" role="alert"><h1>没有此页面的访问权限</h1><p>请选择可访问的模块，或联系管理员。</p></section>
      <RouterView v-else v-slot="{ Component }"><component :is="Component" :key="`${route.path}:${sessionVersion}`" /></RouterView>
    </main></AuthPanel>
  </div>
</template>
