import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DictionaryManager from '../views/dictionary/DictionaryView.vue'
import DictionaryItems from '../views/dictionary/components/DictionaryItems.vue'
import { authorize, deferred, fakeApi, chooseSelect, selectControl } from './support'
const id = '11111111-1111-4111-8111-111111111111',
  target = '22222222-2222-4222-8222-222222222222'
const stamp = '2026-01-01T00:00:00Z'
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
