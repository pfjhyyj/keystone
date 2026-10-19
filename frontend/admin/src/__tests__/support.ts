import { vi } from 'vitest'
import { identity } from '../access'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { ElOption } from 'element-plus'
import AppSelect from '../components/AppSelect.vue'

export function selectControl(wrapper: VueWrapper, labelOrIndex: string | number = 0) {
  const controls = wrapper.findAllComponents(AppSelect)
  const control =
    typeof labelOrIndex === 'number'
      ? controls[labelOrIndex]
      : controls.find((item) => item.vm.$attrs['aria-label'] === labelOrIndex)
  if (!control) throw new Error(`Select control not found: ${labelOrIndex}`)
  return control
}
export async function chooseSelect(
  wrapper: VueWrapper,
  value: string,
  labelOrIndex: string | number = 0,
) {
  const control = selectControl(wrapper, labelOrIndex)
  await control.get('[role=combobox]').trigger('click')
  await flushPromises()
  const option = control.findAllComponents(ElOption).find((item) => item.props('value') === value)
  if (!option) throw new Error(`Select option not found: ${value}`)
  await option.trigger('click')
  await flushPromises()
}
export function authorize(permissions: string[]) {
  identity.value = {
    user: {
      id: 'admin',
      username: 'admin',
      display_name: '管理员',
      password_change_required: false,
    },
    permissions,
    is_administrator: true,
    credential_kind: 'session',
    csrf_token: 'test-csrf',
  }
}
export function fakeApi(handler: (path: string, init: RequestInit) => unknown | Promise<unknown>) {
  return vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const value = await handler(url.replace('/api/v1', ''), init)
      return value instanceof Response
        ? value
        : new Response(JSON.stringify(value), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
    }),
  )
}
export const user = {
  id: 'user-1',
  username: 'alice',
  display_name: 'Alice',
  is_active: true,
  password_change_required: true,
  role_ids: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}
export const role = {
  id: 'role-1',
  key: 'reader',
  name: 'Reader',
  description: '',
  permissions: ['user:read'],
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
