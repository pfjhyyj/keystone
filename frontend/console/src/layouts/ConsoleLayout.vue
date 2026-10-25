<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import {
  ArrowDown,
  Coin,
  Collection,
  Document,
  Files,
  Fold,
  Grid,
  Key,
  Management,
  Menu,
  Setting,
  User,
} from '@element-plus/icons-vue'
import {
  can,
  clearSession,
  identity,
  initialized,
  sessionVersion,
  logout,
  refreshSession,
  SESSION_EVENT,
} from '../stores/session'
import { loginDestination, navigation } from '../router'
const route = useRoute(),
  router = useRouter()
const collapsed = ref(false),
  mobileOpen = ref(false),
  logoutBusy = ref(false),
  logoutError = ref('')
const groups = [
  { title: '工作台', paths: ['/'] },
  { title: '访问控制', paths: ['/users', '/roles', '/audit'] },
  { title: '平台资源', paths: ['/files', '/dictionary'] },
  { title: 'AI 与流程', paths: ['/models', '/model-usage', '/flows'] },
]
const icons = {
  '/': Grid,
  '/users': User,
  '/roles': Key,
  '/audit': Document,
  '/files': Files,
  '/dictionary': Collection,
  '/models': Setting,
  '/model-usage': Coin,
  '/flows': Management,
}
const visible = computed(() =>
  navigation.filter((item) => !item.permissions.length || item.permissions.some(can)),
)
const menus = computed(() =>
  groups
    .map((group) => ({
      ...group,
      items: visible.value.filter((item) => group.paths.includes(item.path)),
    }))
    .filter((group) => group.items.length),
)
const title = computed(
  () =>
    navigation.find((item) => item.path === route.path)?.label ??
    (route.path === '/account' ? '个人中心' : '页面不存在'),
)
const group = computed(
  () => groups.find((item) => item.paths.includes(route.path))?.title ?? '账户设置',
)
const allowed = computed(() => {
  const item = navigation.find((item) => item.path === route.path)
  return !item || !item.permissions.length || item.permissions.some(can)
})
watch(
  () => route.fullPath,
  () => {
    mobileOpen.value = false
    logoutError.value = ''
  },
)
watch(
  [() => identity.value?.user.id, () => identity.value?.user.password_change_required, initialized],
  () => {
    if (!initialized.value) return
    if (!identity.value && route.path !== '/login')
      void router.replace({ path: '/login', query: { next: route.fullPath } })
    else if (identity.value?.user.password_change_required && route.path !== '/account')
      void router.replace('/account')
    else if (
      identity.value &&
      !identity.value.user.password_change_required &&
      route.path === '/login'
    )
      void router.replace(loginDestination(route.query.next))
  },
)
async function command(value: string) {
  if (value !== 'logout') {
    await router.push({ path: '/account', query: { tab: value } })
    return
  }
  if (logoutBusy.value) return
  logoutBusy.value = true
  try {
    await logout()
  } catch (error) {
    logoutError.value = error instanceof Error ? error.message : '退出失败，请重试'
  } finally {
    logoutBusy.value = false
  }
}
function focused() {
  void refreshSession()
}
function changed(event: StorageEvent) {
  if (event.key === SESSION_EVENT) {
    clearSession()
    void refreshSession()
  }
}
onMounted(() => {
  window.addEventListener('focus', focused)
  window.addEventListener('storage', changed)
})
onBeforeUnmount(() => {
  window.removeEventListener('focus', focused)
  window.removeEventListener('storage', changed)
})
</script>
<template>
  <div v-if="!initialized" class="initial-loading"><el-skeleton :rows="5" animated /></div>
  <RouterView v-else-if="!identity" />
  <div
    v-else
    class="console-shell"
    :class="{
      'is-collapsed': collapsed,
      'mobile-open': mobileOpen,
      'is-restricted': identity.user.password_change_required,
    }"
    @keydown.esc="mobileOpen = false"
  >
    <button
      v-if="mobileOpen"
      class="sidebar-scrim"
      aria-label="收起导航"
      @click="mobileOpen = false"
    />
    <aside v-if="!identity.user.password_change_required" class="sidebar">
      <RouterLink class="brand" to="/" aria-label="Keystone 首页"
        ><span class="brand-mark">K</span
        ><span class="brand-name">keystone<small>控制台</small></span></RouterLink
      >
      <nav aria-label="平台模块">
        <section v-for="menu in menus" :key="menu.title" class="nav-group">
          <p class="nav-group-title">{{ menu.title }}</p>
          <RouterLink
            v-for="item in menu.items"
            :key="item.path"
            :to="item.path"
            class="nav-link"
            :aria-label="item.label"
            :aria-current="route.path === item.path ? 'page' : undefined"
            :title="collapsed ? item.label : undefined"
            ><el-icon><component :is="icons[item.path as keyof typeof icons]" /></el-icon
            ><span class="nav-label">{{ item.label }}</span
            ><span v-if="route.path === item.path" class="nav-active-dot"
          /></RouterLink>
        </section>
      </nav>
      <div class="sidebar-footer">
        <el-icon><Setting /></el-icon><span class="nav-label">Keystone Platform</span>
      </div>
    </aside>
    <div class="main-shell">
      <header class="app-header">
        <div class="header-leading">
          <el-button
            v-if="!identity.user.password_change_required"
            text
            class="desktop-menu-toggle"
            :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'"
            @click="collapsed = !collapsed"
            ><el-icon><Fold /></el-icon
          ></el-button>
          <el-button
            v-if="!identity.user.password_change_required"
            text
            class="mobile-menu-toggle"
            aria-label="展开导航"
            @click="mobileOpen = !mobileOpen"
            ><el-icon><Menu /></el-icon
          ></el-button>
          <el-breadcrumb separator="/"
            ><el-breadcrumb-item>{{ group }}</el-breadcrumb-item
            ><el-breadcrumb-item>{{ title }}</el-breadcrumb-item></el-breadcrumb
          >
        </div>
        <div class="header-trailing">
          <span class="header-product">Keystone Console</span>
          <el-dropdown trigger="click" @command="command"
            ><el-button text class="user-menu" :disabled="logoutBusy" aria-label="账户菜单"
              ><el-avatar :size="28">{{ identity.user.display_name.slice(0, 1) }}</el-avatar
              ><span>{{ identity.user.display_name }}</span
              ><el-icon><ArrowDown /></el-icon
            ></el-button>
            <template #dropdown
              ><el-dropdown-menu
                ><template v-if="!identity.user.password_change_required"
                  ><el-dropdown-item command="profile">个人中心</el-dropdown-item
                  ><el-dropdown-item command="password">修改密码</el-dropdown-item
                  ><el-dropdown-item command="tokens">个人令牌</el-dropdown-item></template
                ><el-dropdown-item command="logout" divided
                  >退出登录</el-dropdown-item
                ></el-dropdown-menu
              ></template
            >
          </el-dropdown>
        </div>
      </header>
      <main class="workspace">
        <el-alert v-if="logoutError" type="error" :closable="false" show-icon>{{
          logoutError
        }}</el-alert>
        <section v-if="!allowed" class="access-denied">
          <el-empty description="请选择可访问的模块，或联系管理员。"
            ><h1>没有此页面的访问权限</h1></el-empty
          >
        </section>
        <RouterView v-else v-slot="{ Component }"
          ><div class="page-view">
            <component :is="Component" :key="`${route.path}:${sessionVersion}`" /></div
        ></RouterView>
        <footer class="app-footer">Keystone · 统一控制台</footer>
      </main>
    </div>
  </div>
</template>
