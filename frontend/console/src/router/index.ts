import { createRouter, createWebHistory, type Router, type RouterHistory } from 'vue-router'
import { h } from 'vue'
import { can, canReadAudit, ensureSession, identity, refreshSession } from '../stores/session'

export const navigation = [
  { path: '/', label: '管理总览', permissions: [] },
  { path: '/platform', label: '平台状态', permissions: [] },
  { path: '/users', label: '用户管理', permissions: ['user:read'] },
  { path: '/roles', label: '角色管理', permissions: ['role:read'] },
  { path: '/audit', label: '操作审计', permissions: ['audit:read'] },
  { path: '/dictionary', label: '业务字典', permissions: ['dictionary:read'] },
  {
    path: '/models',
    label: '模型接入',
    permissions: ['model:manage'],
  },
  { path: '/model-usage', label: '用量与费用', permissions: ['model_invocation:read'] },
  { path: '/flows', label: 'Flow 运行', permissions: ['flow:read'] },
]
export function canNavigate(item: (typeof navigation)[number]) {
  if (item.path === '/audit') return canReadAudit()
  return !item.permissions.length || item.permissions.some(can)
}
export function loginDestination(value: unknown, router: Router) {
  if (
    typeof value !== 'string' ||
    !value.startsWith('/') ||
    value.startsWith('//') ||
    /[\\\u0000-\u0020\u007f]/.test(value)
  )
    return '/'
  const target = router.resolve(value)
  return target.path !== '/login' &&
    target.matched.length > 0 &&
    target.matched.every((route) => route.meta.loginDestination !== false && !route.redirect)
    ? target.fullPath
    : '/'
}
export function makeRouter(history: RouterHistory = createWebHistory(import.meta.env.BASE_URL)) {
  const router = createRouter({
    history,
    routes: [
      { path: '/', component: () => import('../views/overview/DashboardView.vue') },
      {
        path: '/platform',
        component: () => import('../views/overview/OverviewView.vue'),
        props: (route) => ({
          section: route.hash,
          workerId: typeof route.query.worker === 'string' ? route.query.worker : undefined,
        }),
      },
      { path: '/users', component: () => import('../views/users/UsersView.vue') },
      { path: '/roles', component: () => import('../views/roles/RolesView.vue') },
      { path: '/audit', component: () => import('../views/audit/AuditView.vue') },
      { path: '/dictionary', component: () => import('../views/dictionary/DictionaryView.vue') },
      { path: '/models', component: () => import('../views/models/ModelsView.vue') },
      { path: '/model-usage', component: () => import('../views/usage/UsageView.vue') },
      { path: '/flows', component: () => import('../views/flows/FlowsView.vue') },
      {
        path: '/login',
        component: () => import('../views/login/LoginView.vue'),
        meta: { loginDestination: false },
      },
      {
        path: '/account',
        component: () => import('../views/account/AccountView.vue'),
      },
      {
        path: '/:pathMatch(.*)*',
        meta: { loginDestination: false },
        component: {
          render: () =>
            h('section', [
              h('h1', '页面不存在'),
              h('p', '请从导航选择页面。'),
              h('a', { href: import.meta.env.BASE_URL }, '返回管理总览'),
            ]),
        },
      },
    ],
  })
  router.beforeEach(async (to) => {
    await ensureSession()
    if (identity.value && to.path !== '/login') await refreshSession()
    if (!identity.value)
      return to.path === '/login'
        ? true
        : { path: '/login', query: { next: to.fullPath }, replace: true }
    if (identity.value.user.password_change_required)
      return to.path === '/account' ? true : { path: '/account', replace: true }
    if (to.path === '/login') return loginDestination(to.query.next, router)
    return true
  })
  return router
}
