import * as confirmation from '../utils/confirm'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { ElDatePicker } from 'element-plus'
import UsersView from '../views/users/UsersView.vue'
import RolesView from '../views/roles/RolesView.vue'
import AuditView from '../views/audit/AuditView.vue'
import UserRolesDialog from '../views/users/components/UserRolesDialog.vue'
import { identity } from '../stores/session'
import { canNavigate, navigation } from '../router'
import { authorize, deferred, fakeApi, role, user } from './support'

describe('user administration', () => {
  it('keeps role assignment accessible without unrelated user mutation permissions', async () => {
    authorize(['user:read', 'role:read', 'role:assign'])
    fakeApi((path) => (path.startsWith('/users') ? [user] : []))
    const wrapper = mount(UsersView)
    await flushPromises()
    const assign = wrapper.findAll('button').find((node) => node.text() === '分配角色')!
    expect(assign.attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.row-more-trigger').exists()).toBe(false)
    await assign.trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(UserRolesDialog).props('user').id).toBe(user.id)
  })
  it('confirms account changes and shows a rejected operation on the page', async () => {
    authorize(['user:read', 'user:update'])
    const confirm = vi
      .spyOn(confirmation, 'confirmAction')
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const writes: string[] = []
    fakeApi((path, init) => {
      if (init.method === 'PUT') {
        writes.push(path)
        return new Response(JSON.stringify({ detail: '账户状态变更被拒绝' }), { status: 409 })
      }
      return [user]
    })
    const wrapper = mount(UsersView)
    await flushPromises()
    async function deactivate() {
      await wrapper.get('.row-more-trigger').trigger('click')
      await flushPromises()
      await new DOMWrapper(document.body)
        .findAll('button')
        .find((button) => button.text() === '停用')!
        .trigger('click')
      await flushPromises()
    }
    await deactivate()
    expect(writes).toEqual([])
    await deactivate()
    expect(writes).toEqual([`/users/${user.id}/active`])
    expect(wrapper.text()).toContain('账户状态变更被拒绝')
    expect(wrapper.get('.el-tag').text()).toBe('启用')
    expect(confirm).toHaveBeenCalledTimes(2)
  })
  it('hides mutation controls from readers and never requests role data', async () => {
    authorize(['user:read'])
    const paths: string[] = []
    fakeApi((path) => {
      paths.push(path)
      return [user]
    })
    const wrapper = mount(UsersView)
    await flushPromises()
    expect(wrapper.text()).toContain('Alice')
    expect(wrapper.get('.el-tag').text()).toBe('启用')
    expect(wrapper.text()).not.toContain('新建用户')
    expect(wrapper.text()).not.toContain('重置密码')
    expect(paths.every((path) => path.startsWith('/users?'))).toBe(true)
  })
  it('jumps to the requested user page and returns directly from an empty page', async () => {
    authorize(['user:read'])
    const offsets: number[] = []
    fakeApi((path) => {
      const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset'))
      offsets.push(offset)
      return offset < 150
        ? Array.from({ length: 50 }, (_, index) => ({
            ...user,
            id: String(offset + index + 1),
            display_name: `用户 ${offset + index + 1}`,
          }))
        : []
    })
    const wrapper = mount(UsersView)
    try {
      await flushPromises()
      const pager = wrapper.get('.list-pager')
      const input = pager.get<HTMLInputElement>('input[aria-label="跳转页码"]')
      const jump = async (value: string) => {
        await input.setValue(value)
        await pager.get('form').trigger('submit')
        await flushPromises()
      }

      for (const invalid of ['0', '2.5', '2002']) {
        await jump(invalid)
        expect(input.attributes('aria-invalid')).toBe('true')
      }
      expect(offsets).toEqual([0])

      await jump('3')
      expect(offsets).toEqual([0, 100])
      expect(pager.text()).toContain('第 3 页')
      expect(wrapper.text()).toContain('用户 101')
      expect(input.element.value).toBe('')

      await jump('2001')
      expect(offsets).toEqual([0, 100, 100000, 100])
      expect(pager.text()).toContain('第 3 页')
      expect(wrapper.text()).toContain('用户 101')
    } finally {
      wrapper.unmount()
    }
  })
  it('creates only model fields, prevents repeated submit and clears password on failure', async () => {
    authorize(['user:read', 'user:create'])
    const result = deferred<Response>(),
      bodies: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        bodies.push(JSON.parse(String(init.body)))
        return result.promise
      }
      return []
    })
    const wrapper = mount(UsersView)
    await flushPromises()
    await wrapper.get('.heading button').trigger('click')
    const inputs = wrapper.findAll('[role=dialog] input')
    for (const input of inputs.slice(2)) expect(input.attributes('minlength')).toBe('8')
    for (const [index, value] of ['bob', 'Bob', '12345678', '12345678'].entries())
      await inputs[index]!.setValue(value)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await wrapper.get('[role=dialog] form').trigger('submit')
    expect(bodies).toEqual([{ username: 'bob', display_name: 'Bob', password: '12345678' }])
    result.resolve(
      new Response(JSON.stringify({ code: 'conflict', detail: '记录已存在' }), { status: 409 }),
    )
    await flushPromises()
    expect(wrapper.get('[role=dialog]').text()).toContain('记录已存在')
    expect((inputs[2]!.element as HTMLInputElement).value).toBe('')
  })
  it('uses individual role assignment and leaves blocked changes unchanged', async () => {
    authorize(['role:read', 'role:assign', 'user:read'])
    const bodies: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'PUT') {
        bodies.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ code: 'last_admin', detail: '请保留管理员' }), {
          status: 409,
        })
      }
      return [role]
    })
    const wrapper = mount(UserRolesDialog, { props: { user } })
    await flushPromises()
    await wrapper.get('.selection-list button').trigger('click')
    await flushPromises()
    expect(bodies).toEqual([{ assigned: true }])
    expect(wrapper.text()).toContain('请保留管理员')
    expect(wrapper.get('.selection-list button').text()).toBe('分配角色')
  })
  it('explains assignment scope, shows assigned roles and changes only the selected assignment', async () => {
    authorize(['role:read', 'role:assign', 'user:read'])
    const writes: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'PUT') {
        writes.push([path, JSON.parse(String(init.body))])
        return {}
      }
      return [
        {
          ...role,
          id: 'administrator',
          key: 'admin',
          name: '管理员',
          all_permissions: true,
          permissions: ['audit:read', 'user:read'],
        },
        { ...role, id: 'auditor', name: '审计员', permissions: ['audit:read'] },
        role,
      ]
    })
    const wrapper = mount(UserRolesDialog, { props: { user: { ...user, role_ids: [role.id] } } })
    await flushPromises()
    const items = wrapper.findAll('.selection-list li')
    expect(items[0]!.text()).toContain('超出当前账号可授予的权限范围')
    expect(items[0]!.get('button').attributes('disabled')).toBeDefined()
    expect(items[1]!.text()).toContain('超出当前账号可授予的权限范围')
    expect(items[1]!.get('button').attributes('disabled')).toBeDefined()
    expect(items[2]!.text()).toContain('1 项权限')
    expect(items[2]!.text()).toContain('已分配')
    await items[2]!.get('button').trigger('click')
    await flushPromises()
    expect(writes).toEqual([[`/users/${user.id}/roles/${role.id}`, { assigned: false }]])
    expect(items[2]!.text()).toContain('未分配')
  })
  it('allows retrying a failed role directory and distinguishes an empty directory', async () => {
    authorize(['role:read', 'role:assign'])
    let attempts = 0
    fakeApi(() =>
      ++attempts === 1
        ? new Response(JSON.stringify({ detail: '角色目录暂不可用' }), { status: 500 })
        : [],
    )
    const wrapper = mount(UserRolesDialog, { props: { user } })
    await flushPromises()
    expect(wrapper.text()).toContain('角色目录暂不可用')
    await wrapper
      .findAll('button')
      .find((node) => node.text() === '重试角色目录')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('暂无角色，可先在角色管理中新建。')
    expect(wrapper.text()).not.toContain('角色目录暂不可用')
    expect(attempts).toBe(2)
  })
})

describe('roles and audit', () => {
  it('gates audit navigation and requests by audit:read alone', async () => {
    authorize([])
    fakeApi(() => [])
    const wrapper = mount(AuditView)
    await flushPromises()
    const audit = navigation.find((item) => item.path === '/audit')!
    expect(canNavigate(audit)).toBe(false)
    expect(globalThis.fetch).not.toHaveBeenCalled()
    authorize(['audit:read'])
    await flushPromises()
    expect(canNavigate(audit)).toBe(true)
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('discards audit rows when audit:read is revoked', async () => {
    authorize(['audit:read'])
    const late = deferred<unknown>()
    fakeApi(() => late.promise)
    const wrapper = mount(AuditView)
    identity.value = { ...identity.value!, permissions: [] }
    late.resolve([{ id: 'private-audit', action: 'private-action' }])
    await flushPromises()
    expect(wrapper.text()).not.toContain('private-action')
    expect(wrapper.find('tbody tr').exists()).toBe(false)
  })
  it.each(['default', 'edited'])(
    'keeps the %s audit time range fixed across paging and refresh',
    async (boundary) => {
      authorize(['audit:read'])
      const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-20T00:00:00Z'))
      try {
        const paths: string[] = []
        fakeApi((path) => {
          paths.push(path)
          return Array.from({ length: 50 }, (_, index) => ({
            id: `audit-${index}`,
            occurred_at: '2026-10-19T00:00:00Z',
            actor_type: 'system',
            actor_id: null,
            action: 'identity.role.update',
            target_type: 'role',
            target_id: null,
            outcome: 'success',
            correlation_id: null,
            details: {},
          }))
        })
        const wrapper = mount(AuditView)
        await flushPromises()
        if (boundary === 'edited') {
          wrapper
            .getComponent(ElDatePicker)
            .vm.$emit('update:modelValue', ['2026-10-01T00:00', '2026-10-20T00:00'])
          await flushPromises()
          await wrapper.get('form').trigger('submit')
          await flushPromises()
        }
        const first = new URLSearchParams(paths.at(-1)!.split('?')[1])
        expect(first.get('since')).toBeTruthy()
        expect(first.get('until')).toBeTruthy()
        clock.mockReturnValue(Date.parse('2026-10-27T00:00:00Z'))
        await wrapper
          .findAll('label')
          .find((node) => node.text() === '操作代码')!
          .get('input')
          .setValue('not-applied')
        await wrapper
          .findAll('button')
          .find((node) => node.text() === '下一页')!
          .trigger('click')
        await flushPromises()
        const next = new URLSearchParams(paths.at(-1)!.split('?')[1])
        expect(next.get('offset')).toBe(first.get('limit'))
        expect(next.get('since')).toBe(first.get('since'))
        expect(next.get('until')).toBe(first.get('until'))
        expect(next.get('action')).toBeNull()
        await wrapper
          .findAll('button')
          .find((node) => node.text() === '刷新')!
          .trigger('click')
        await flushPromises()
        expect(new URLSearchParams(paths.at(-1)!.split('?')[1]).get('until')).toBe(
          first.get('until'),
        )
      } finally {
        clock.mockRestore()
      }
    },
  )
  it('keeps the built-in role undeletable and prevents granting absent permissions', async () => {
    authorize(['role:read', 'role:create', 'role:update', 'role:delete', 'user:read'])
    fakeApi((path) =>
      path === '/permissions'
        ? [
            { key: 'user:read', name: '读取用户' },
            { key: 'audit:read', name: '读取审计' },
          ]
        : [{ ...role, key: 'admin', all_permissions: true }, role],
    )
    const wrapper = mount(RolesView)
    await flushPromises()
    const [edit, remove] = wrapper.findAll('tbody tr')[0]!.findAll('.row-actions button')
    expect(wrapper.findAll('tbody tr')[0]!.text()).toContain('全部权限')
    expect(edit!.attributes('disabled')).toBeUndefined()
    expect(remove!.attributes('disabled')).toBeDefined()
    await wrapper.get('.heading button').trigger('click')
    const checks = wrapper.findAll('.permission-table input[type=checkbox]')
    expect(checks[0]!.attributes('disabled')).toBeUndefined()
    expect(checks[1]!.attributes('disabled')).toBeDefined()
  })
  it('shows role reference conflict without optimistic removal', async () => {
    vi.spyOn(confirmation, 'confirmAction').mockResolvedValue(true)
    authorize(['role:read', 'role:delete', 'user:read'])
    fakeApi((path, init) =>
      init.method === 'DELETE'
        ? new Response(JSON.stringify({ code: 'role_in_use', detail: '角色仍被文件授权引用' }), {
            status: 409,
          })
        : path === '/permissions'
          ? []
          : [role],
    )
    const wrapper = mount(RolesView)
    await flushPromises()
    await wrapper
      .findAll('tbody button')
      .find((button) => button.text() === '删除角色')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('角色仍被文件授权引用')
    expect(wrapper.get('tbody').text()).toContain('Reader')
  })
  it('accepts string target IDs, distinguishes anonymous actors, escapes details', async () => {
    authorize(['audit:read'])
    const paths: string[] = []
    fakeApi((path) => {
      paths.push(path)
      return [
        {
          id: 'audit-1',
          occurred_at: '2026-01-01T00:00:00Z',
          actor_type: 'anonymous',
          actor_id: null,
          action: 'identity.login',
          outcome: 'denied',
          target_type: 'sample',
          target_id: 'record:one',
          correlation_id: null,
          details: { value: '<script>bad()</script>' },
        },
      ]
    })
    const wrapper = mount(AuditView)
    await flushPromises()
    const target = wrapper.findAll('label').find((label) => label.text() === '目标 ID')!
    await target.get('input').setValue('record:one')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(paths.at(-1)).toContain('target_id=record%3Aone')
    expect(wrapper.get('tbody').text()).toContain('匿名')
    await wrapper.get('tbody button').trigger('click')
    expect(wrapper.get('pre').text()).toContain('<script>bad()</script>')
    expect(wrapper.find('[role=dialog] script').exists()).toBe(false)
  })
  it('discards a stale list response after revocation', async () => {
    authorize(['user:read'])
    const late = deferred<unknown>()
    fakeApi(() => late.promise)
    const wrapper = mount(UsersView)
    identity.value = { ...identity.value!, permissions: [] }
    await flushPromises()
    late.resolve([user])
    await flushPromises()
    expect(wrapper.text()).not.toContain('Alice')
  })
  it('rejects invalid audit time windows before fetching', async () => {
    authorize(['audit:read'])
    fakeApi(() => [])
    const wrapper = mount(AuditView)
    await flushPromises()
    const calls = vi.mocked(fetch).mock.calls.length
    wrapper
      .getComponent(ElDatePicker)
      .vm.$emit('update:modelValue', ['2020-01-01T00:00', '2026-01-01T00:00'])
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('不超过 366 天')
    expect(vi.mocked(fetch).mock.calls.length).toBe(calls)
  })
})
