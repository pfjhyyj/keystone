import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import FileLibrary from '../FileLibrary.vue'
import FileGrantsDialog from '../components/FileGrantsDialog.vue'
import FileReferencesDialog from '../components/FileReferencesDialog.vue'
import SubjectPicker from '../components/SubjectPicker.vue'
import DictionaryManager from '../DictionaryManager.vue'
import DictionaryItems from '../components/DictionaryItems.vue'
import { authorize, deferred, fakeApi, chooseSelect, selectControl } from './support'
const id = '11111111-1111-4111-8111-111111111111',
  target = '22222222-2222-4222-8222-222222222222'
const stamp = '2026-01-01T00:00:00Z'
const file = {
  id,
  name: 'report.txt',
  owner_id: target,
  status: 'active',
  size_bytes: 12,
  created_at: stamp,
  updated_at: stamp,
}
const grant = {
  id: 'grant',
  subject_type: 'user',
  user_id: target,
  role_id: null,
  action: 'manage',
  created_at: stamp,
  created_by: target,
}
const definition = {
  id,
  key: 'source',
  name: '来源',
  description: '原说明',
  is_active: true,
  created_at: stamp,
  updated_at: stamp,
}
const item = {
  id: target,
  dictionary_id: id,
  value: 'online',
  label: '线上',
  sort_order: 12,
  is_active: true,
  created_at: stamp,
  updated_at: stamp,
}
const button = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('button').find((node) => node.text() === label)!
const failure = (detail: string) =>
  new Response(JSON.stringify({ code: 'denied', detail }), { status: 404 })

describe('file grants and references', () => {
  it('uploads to the selected source and preserves the request key on retry', async () => {
    authorize(['file:create'])
    const writes: URLSearchParams[] = []
    vi.stubGlobal('crypto', {
      randomUUID: () => 'source-upload-key',
      subtle: { digest: async () => new Uint8Array(32).buffer },
    })
    fakeApi((path, init) => {
      if (path === '/files/options')
        return { max_file_bytes: 1024, storage_keys: ['local', 'cloud'], default_storage: 'local' }
      writes.push(new URLSearchParams(path.split('?')[1]))
      return new Response(JSON.stringify({ detail: '暂不可用' }), { status: 503 })
    })
    const wrapper = mount(FileLibrary)
    await flushPromises()
    expect(selectControl(wrapper).props('modelValue')).toBe('local')
    const chosen = new File(['hello'], 'report.txt', { type: 'text/plain' })
    Object.defineProperty(chosen, 'arrayBuffer', { value: async () => new ArrayBuffer(5) })
    Object.defineProperty(wrapper.get('input[type=file]').element, 'files', { value: [chosen] })
    await wrapper.get('input[type=file]').trigger('change')
    await chooseSelect(wrapper, 'cloud')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(writes).toHaveLength(2)
    expect(writes.map((params) => params.get('storage_key'))).toEqual(['cloud', 'cloud'])
    expect(writes[0]!.get('request_key')).toBe(writes[1]!.get('request_key'))
  })
  it('keeps readers out of grants and reference queries', async () => {
    authorize(['file:inspect'])
    const paths: string[] = []
    fakeApi((path) => {
      paths.push(path)
      return [file]
    })
    const wrapper = mount(FileLibrary)
    await flushPromises()
    expect(wrapper.text()).toContain('report.txt')
    expect(wrapper.text()).not.toContain('文件授权')
    expect(wrapper.text()).not.toContain('查看引用')
    expect(paths).toEqual(['/files?offset=0&limit=50'])
  })
  it('supports a known ID without requesting inspect or identity directories', async () => {
    authorize(['file:manage'])
    const paths: string[] = [],
      bodies: unknown[] = []
    fakeApi((path, init) => {
      paths.push(path)
      if (init.method === 'POST') bodies.push(JSON.parse(String(init.body)))
      return []
    })
    const wrapper = mount(FileLibrary)
    await flushPromises()
    await wrapper.get('.known-file input').setValue(id)
    await wrapper.get('.known-file form').trigger('submit')
    await flushPromises()
    await wrapper.get('[role=dialog] input[placeholder="对象 UUID"]').setValue(target)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(bodies).toEqual([
      { subject_type: 'user', user_id: target, role_id: null, action: 'inspect' },
    ])
    expect(paths.every((path) => path.startsWith(`/files/${id}/grants`))).toBe(true)
  })
  it('clears subjects and disallows authenticated management; prevents double grants', async () => {
    authorize(['file:manage'])
    const result = deferred<unknown>(),
      bodies: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'POST') {
        bodies.push(JSON.parse(String(init.body)))
        return result.promise
      }
      return []
    })
    const wrapper = mount(FileGrantsDialog, { props: { file } })
    await flushPromises()
    await wrapper.get('input[placeholder="对象 UUID"]').setValue(target)
    await chooseSelect(wrapper, 'manage', 1)
    await chooseSelect(wrapper, 'authenticated', 0)
    expect(wrapper.find('input[placeholder="对象 UUID"]').exists()).toBe(false)
    expect(
      selectControl(wrapper, 1)
        .findAll('[role=option]')
        .map((option) => option.text()),
    ).not.toContain('管理与授权')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('form').trigger('submit')
    expect(bodies).toEqual([
      { subject_type: 'authenticated', user_id: null, role_id: null, action: 'inspect' },
    ])
    result.resolve({})
    await flushPromises()
  })
  it('clears grants when self-revocation removes management access', async () => {
    authorize(['file:manage'])
    let revoked = false
    fakeApi((path, init) => {
      if (init.method === 'DELETE') {
        revoked = true
        return new Response(null, { status: 204 })
      }
      return revoked ? failure('文件不存在或不可访问') : [grant]
    })
    const wrapper = mount(FileGrantsDialog, { props: { file } })
    await flushPromises()
    await button(wrapper, '撤销授权').trigger('click')
    expect(revoked).toBe(false)
    await wrapper.get('.confirmation').trigger('submit')
    await flushPromises()
    expect(wrapper.findAll('.grant-list li')).toHaveLength(0)
    expect(wrapper.get('[role=alert]').text()).toBe('文件不存在或不可访问')
    expect(button(wrapper, '添加授权').attributes('disabled')).toBeDefined()
  })
  it('paginates reference facts without links, mutations or active HTML', async () => {
    authorize(['file:manage'])
    const paths: string[] = []
    fakeApi((path) => {
      paths.push(path)
      return path.includes('offset=50')
        ? [{ owner: '<img src=x>', slot: 'output' }]
        : Array.from({ length: 50 }, (_, i) => ({ owner: `flow:${i}`, slot: 'input' }))
    })
    const wrapper = mount(FileReferencesDialog, { props: { file } })
    await flushPromises()
    await button(wrapper, '下一页').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('<img src=x>')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('删除引用')
    expect(paths[1]).toBe(`/files/${id}/references?offset=50&limit=50`)
  })
  it('does not allow mutations while a file is being deleted', async () => {
    authorize(['file:manage'])
    fakeApi(() => [grant])
    const wrapper = mount(FileGrantsDialog, { props: { file: { ...file, status: 'deleting' } } })
    await flushPromises()
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('撤销授权')
  })
  it('clears subject selection when changing directory pages', async () => {
    authorize(['role:read'])
    const paths: string[] = []
    fakeApi((path) => {
      paths.push(path)
      return Array.from({ length: 50 }, (_, i) => ({ id: `${i}`, name: `Role ${i}`, key: `r${i}` }))
    })
    const wrapper = mount(SubjectPicker, {
      props: { kind: 'role', disabled: false, modelValue: '0' },
    })
    await flushPromises()
    await button(wrapper, '下一页').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([''])
    expect(paths[1]).toBe('/roles?offset=50&limit=50')
  })
})

describe('dictionary administration', () => {
  it('supports read-only browsing and drops late items from a previous dictionary', async () => {
    authorize(['dictionary:read'])
    const old = deferred<unknown>()
    fakeApi((path) =>
      path.startsWith(`/dictionaries/${id}/items`)
        ? old.promise
        : path.includes('/items')
          ? [{ ...item, label: '第二字典选项' }]
          : [definition, { ...definition, id: target, name: '第二字典', key: 'second' }],
    )
    const wrapper = mount(DictionaryManager)
    await flushPromises()
    await wrapper.findAll('tbody button')[0]!.trigger('click')
    await flushPromises()
    await wrapper.findAll('tbody button')[1]!.trigger('click')
    await flushPromises()
    old.resolve([item])
    await flushPromises()
    expect(wrapper.get('.dictionary-items').text()).toContain('第二字典选项')
    expect(wrapper.get('.dictionary-items').text()).not.toContain('线上')
    expect(wrapper.text()).not.toContain('新建字典')
    expect(wrapper.text()).not.toContain('编辑选项')
  })
  it('sends complete editable fields, keeps the key immutable and updates parent status', async () => {
    authorize(['dictionary:read', 'dictionary:manage'])
    let row = definition
    const bodies: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'PATCH') {
        const body = JSON.parse(String(init.body))
        bodies.push(body)
        row = { ...row, ...body }
        return row
      }
      return path.includes('/items') ? [item] : [row]
    })
    const wrapper = mount(DictionaryManager)
    await flushPromises()
    await button(wrapper, '查看选项').trigger('click')
    await flushPromises()
    await button(wrapper, '编辑字典').trigger('click')
    expect(wrapper.get('[role=dialog] input').attributes('disabled')).toBeDefined()
    await wrapper.get('[role=dialog] textarea').setValue('保留说明')
    await wrapper.get('[role=dialog] input[type=checkbox]').setValue(false)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(bodies).toEqual([{ name: '来源', description: '保留说明', is_active: false }])
    expect(wrapper.get('.dictionary-items').text()).toContain('已启用，字典停用中')
  })
  it('retains item edits on rejection and never sends stable values in PATCH', async () => {
    authorize(['dictionary:read', 'dictionary:manage'])
    const bodies: unknown[] = []
    fakeApi((path, init) => {
      if (init.method === 'PATCH') {
        bodies.push(JSON.parse(String(init.body)))
        return failure('选项已不可编辑')
      }
      return [item]
    })
    const wrapper = mount(DictionaryItems, { props: { dictionary: definition } })
    await flushPromises()
    await button(wrapper, '编辑选项').trigger('click')
    expect(wrapper.get('[role=dialog] input').attributes('disabled')).toBeDefined()
    await wrapper.get('[role=dialog] input[type=number]').setValue(-5)
    await wrapper.get('[role=dialog] input[type=checkbox]').setValue(false)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(bodies).toEqual([{ label: '线上', sort_order: -5, is_active: false }])
    expect(wrapper.get('[role=dialog]').text()).toContain('选项已不可编辑')
    expect(wrapper.get('tbody').text()).toContain('可选')
  })
  it('resets a new item after closing an edit and rejects fractional ordering', async () => {
    authorize(['dictionary:read', 'dictionary:manage'])
    const writes: string[] = []
    fakeApi((path, init) => {
      if (init.method !== 'GET') writes.push(path)
      return [item]
    })
    const wrapper = mount(DictionaryItems, { props: { dictionary: definition } })
    await flushPromises()
    await button(wrapper, '编辑选项').trigger('click')
    await wrapper.get('[role=dialog] button[aria-label]').trigger('click')
    await button(wrapper, '添加选项').trigger('click')
    expect((wrapper.get('[role=dialog] input').element as HTMLInputElement).value).toBe('')
    await wrapper.get('[role=dialog] input[type=number]').setValue(1.5)
    await wrapper.get('[role=dialog] form').trigger('submit')
    await flushPromises()
    expect(writes).toEqual([])
    expect(wrapper.get('[role=alert]').text()).toContain('32 位整数')
  })
})
