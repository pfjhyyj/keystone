import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import UsersView from '../views/users/UsersView.vue'
import RolesView from '../views/roles/RolesView.vue'
import AuditView from '../views/audit/AuditView.vue'
import UserRolesDialog from '../views/users/components/UserRolesDialog.vue'
import { identity } from '../stores/session'
import { authorize, deferred, fakeApi, role, user } from './support'

describe('user administration', () => {
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
})

describe('roles and audit', () => {
  it('locks built-in role and prevents granting absent permissions', async () => {
    authorize(['role:read', 'role:create', 'role:update', 'role:delete', 'user:read'])
    fakeApi((path) =>
      path === '/permissions'
        ? [
            { key: 'user:read', name: '读取用户' },
            { key: 'audit:read', name: '读取审计' },
          ]
        : [{ ...role, key: 'admin' }, role],
    )
    const wrapper = mount(RolesView)
    await flushPromises()
    expect(
      wrapper
        .findAll('tbody tr')[0]!
        .findAll('button')
        .every((button) => button.attributes('disabled') !== undefined),
    ).toBe(true)
    await wrapper.get('.heading button').trigger('click')
    const checks = wrapper.findAll('input[type=checkbox]')
    expect(checks[0]!.attributes('disabled')).toBeUndefined()
    expect(checks[1]!.attributes('disabled')).toBeDefined()
  })
  it('shows role reference conflict without optimistic removal', async () => {
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
    await wrapper.get('tbody button').trigger('click')
    await wrapper
      .findAll('[role=dialog] button')
      .find((button) => button.text() === '确认删除')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.get('[role=dialog]').text()).toContain('角色仍被文件授权引用')
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
    expect(wrapper.findAll('[role=dialog] button')).toHaveLength(1)
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
    const dates = wrapper.findAll('input[type=datetime-local]')
    await dates[0]!.setValue('2020-01-01T00:00')
    await dates[1]!.setValue('2026-01-01T00:00')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('不超过 366 天')
    expect(vi.mocked(fetch).mock.calls.length).toBe(calls)
  })
})
