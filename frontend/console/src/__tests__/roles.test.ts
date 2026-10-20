import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { expect, it } from 'vitest'
import type { Permission } from '../api/admin'
import PermissionPicker from '../views/roles/components/PermissionPicker.vue'
import RolesView from '../views/roles/RolesView.vue'
import { identity } from '../stores/session'
import { authorize, chooseSelect, fakeApi, role } from './support'

const catalog: Permission[] = [
  { key: 'user:read', name: '查看用户', description: '查看用户' },
  { key: 'role:update', name: '修改角色权限', description: '修改角色权限' },
  { key: 'file:inspect', name: '查看文件', description: '查看文件元数据' },
  { key: 'file:manage', name: '管理文件', description: '管理文件与授权' },
  { key: 'flow:read', name: '查看流程', description: '读取本人流程' },
  { key: 'orders.invoice:read', name: '查看发票', description: '订单发票详情' },
  { key: 'constructor.record:read', name: '查看业务记录', description: '查看业务记录' },
]
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!

function picker(selected: string[] = [], readonly = false) {
  const wrapper = mount(PermissionPicker, {
    attachTo: document.body,
    props: {
      catalog,
      modelValue: selected,
      readonly,
      'onUpdate:modelValue': (value) => {
        void wrapper.setProps({ modelValue: value })
      },
    },
  })
  return wrapper
}

it('filters by module and keyword; bulk changes preserve selections outside the filter', async () => {
  authorize(catalog.map((permission) => permission.key))
  const wrapper = picker(['flow:read'])
  await chooseSelect(wrapper, 'identity', '角色权限模块')
  expect(wrapper.findAll('tbody tr')).toHaveLength(2)
  await button(wrapper, '全选当前筛选').trigger('click')
  expect(wrapper.props('modelValue')).toEqual(['flow:read', 'user:read', 'role:update'])
  await chooseSelect(wrapper, 'file', '角色权限模块')
  await wrapper.get('input[aria-label="搜索角色权限"]').setValue('  MANAGE  ')
  expect(wrapper.findAll('tbody tr')).toHaveLength(1)
  await button(wrapper, '全选当前筛选').trigger('click')
  expect(wrapper.props('modelValue')).toContain('file:manage')
  await button(wrapper, '清除当前筛选').trigger('click')
  expect(wrapper.props('modelValue')).toEqual(['flow:read', 'user:read', 'role:update'])
  await button(wrapper, '清除筛选').trigger('click')
  await wrapper.get('input[aria-label="搜索角色权限"]').setValue('not-a-permission')
  expect(wrapper.text()).toContain('没有符合筛选条件的权限')
  expect(button(wrapper, '全选当前筛选').attributes('disabled')).toBeDefined()
  await button(wrapper, '清除筛选').trigger('click')
  await chooseSelect(wrapper, 'orders', '角色权限模块')
  expect(wrapper.text()).toContain('orders.invoice:read')
  expect(wrapper.findAll('tbody tr')).toHaveLength(1)
  await chooseSelect(wrapper, 'constructor', '角色权限模块')
  expect(wrapper.get('.permission-module').text()).toBe('constructor')
})

it('does not grant or remove unavailable permissions during batch operations', async () => {
  authorize(['file:inspect'])
  const wrapper = picker(['file:manage'])
  await chooseSelect(wrapper, 'file', '角色权限模块')
  expect(
    wrapper.get('label[aria-label="管理文件 file:manage"] input').attributes('disabled'),
  ).toBeDefined()
  await button(wrapper, '全选当前筛选').trigger('click')
  expect(wrapper.props('modelValue')).toEqual(['file:manage', 'file:inspect'])
  await button(wrapper, '清除当前筛选').trigger('click')
  expect(wrapper.props('modelValue')).toEqual(['file:manage'])
})

it('supports filtering read-only role permissions without exposing mutation controls', async () => {
  authorize(['role:read'])
  const wrapper = picker(['user:read', 'file:inspect'], true)
  await wrapper.findAll('.permission-filters input[type=checkbox]')[0]!.setValue(true)
  expect(wrapper.findAll('tbody tr')).toHaveLength(2)
  expect(
    wrapper
      .findAll('tbody input[type=checkbox]')
      .every((node) => node.attributes('disabled') !== undefined),
  ).toBe(true)
  expect(wrapper.text()).not.toContain('全选当前筛选')
  await chooseSelect(wrapper, 'identity', '角色权限模块')
  expect(wrapper.findAll('tbody tr')).toHaveLength(1)
  expect(wrapper.props('modelValue')).toEqual(['user:read', 'file:inspect'])
})

it('saves the full selected set from a filtered editor and resets filters on reopening', async () => {
  authorize(['role:read', 'role:update', ...catalog.map((permission) => permission.key)])
  const bodies: Record<string, unknown>[] = []
  let stored = { ...role, permissions: ['user:read', 'flow:read'] }
  fakeApi((path, init) => {
    if (path === '/permissions') return catalog
    if (path === '/auth/me') return identity.value
    if (init.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      bodies.push(body)
      stored = { ...stored, ...body }
      return stored
    }
    return [stored]
  })
  const wrapper = mount(RolesView, { attachTo: document.body })
  await flushPromises()
  await button(wrapper, '配置权限').trigger('click')
  const editor = wrapper.findComponent(PermissionPicker)
  await chooseSelect(editor, 'file', '角色权限模块')
  await editor.get('input[aria-label="搜索角色权限"]').setValue('元数据')
  await button(editor, '全选当前筛选').trigger('click')
  await wrapper.get('[role=dialog] form').trigger('submit')
  await flushPromises()
  expect(bodies).toEqual([
    { name: 'Reader', description: '', permissions: ['user:read', 'flow:read', 'file:inspect'] },
  ])
  await button(wrapper, '配置权限').trigger('click')
  expect(wrapper.getComponent(PermissionPicker).findAll('tbody tr')).toHaveLength(catalog.length)
  expect((wrapper.get('input[aria-label="搜索角色权限"]').element as HTMLInputElement).value).toBe(
    '',
  )
})
