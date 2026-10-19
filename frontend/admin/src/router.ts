import { createRouter, createWebHistory, type RouterHistory } from 'vue-router'
import { h } from 'vue'
import { ensureSession, identity, refreshSession } from './access'

export const navigation = [
  { path: '/', label: '平台状态', permissions: [] },
  { path: '/users', label: '用户管理', permissions: ['user:read'] },
  { path: '/roles', label: '角色与权限', permissions: ['role:read'] },
  { path: '/audit', label: '操作审计', permissions: ['audit:read'] },
  {
    path: '/files',
    label: '文件库',
    permissions: ['file:create', 'file:inspect', 'file:manage', 'file:content_read'],
  },
  { path: '/dictionary', label: '业务字典', permissions: ['dictionary:read'] },
  {
    path: '/models',
    label: '模型接入',
    permissions: ['model:invoke', 'model:manage', 'model_invocation:read'],
  },
  { path: '/flows', label: 'Flow 流程', permissions: ['flow:start', 'flow:read'] },
]
export function loginDestination(value: unknown) {
  return typeof value === 'string' &&
    [...navigation.map((item) => item.path), '/account'].includes(value.split('?')[0]!)
    ? value
    : '/'
}
export function makeRouter(history: RouterHistory = createWebHistory(import.meta.env.BASE_URL)) {
  const router = createRouter({
    history,
    routes: [
      { path: '/', component: () => import('./views/OverviewView.vue') },
      { path: '/users', component: () => import('./views/UsersView.vue') },
      { path: '/roles', component: () => import('./views/RolesView.vue') },
      { path: '/audit', component: () => import('./views/AuditView.vue') },
      { path: '/files', component: () => import('./FileLibrary.vue') },
      { path: '/dictionary', component: () => import('./DictionaryManager.vue') },
      { path: '/models', component: () => import('./ModelManager.vue') },
      { path: '/flows', component: () => import('./FlowManager.vue') },
      { path: '/login', component: () => import('./views/LoginView.vue') },
      {
        path: '/account',
        component: () => import('./views/AccountView.vue'),
      },
      {
        path: '/:pathMatch(.*)*',
        component: {
          render: () => h('section', [h('h1', '页面不存在'), h('p', '请从导航选择页面。')]),
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
    if (to.path === '/login') return loginDestination(to.query.next)
    return true
  })
  return router
}
