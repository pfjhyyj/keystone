import { flushPromises, mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import UpstreamModelPicker from '../views/models/components/UpstreamModelPicker.vue'
import { authorize, chooseSelect, fakeApi, selectControl } from './support'

const channelId = '22222222-2222-4222-8222-222222222222'

it('loads the Ark catalog, refreshes it and keeps a manually configured endpoint ID', async () => {
  authorize(['model:manage'])
  const requests: string[] = []
  fakeApi((path, init) => {
    requests.push(path)
    expect(init.method).toBe('GET')
    expect(path).toBe(`/model-channels/${channelId}/models`)
    return { items: [{ id: requests.length === 1 ? 'doubao-seed-test' : 'doubao-seed-next' }] }
  })
  const wrapper = mount(UpstreamModelPicker, {
    props: { channelId, disabled: false, modelValue: 'ep-existing' },
  })
  await flushPromises()
  const control = selectControl(wrapper, '上游模型')
  expect(control.text()).toContain('doubao-seed-test')
  expect(control.props('modelValue')).toBe('ep-existing')
  expect(wrapper.text()).toContain('当前渠道返回的列表不包含此模型')
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper
    .findAll('button')
    .find((b) => b.text() === '刷新上游模型')!
    .trigger('click')
  await flushPromises()
  expect(control.text()).toContain('doubao-seed-next')
  expect(control.text()).not.toContain('doubao-seed-test')
  expect(control.props('modelValue')).toBe('ep-existing')
  await chooseSelect(wrapper, 'doubao-seed-next', '上游模型')
  expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['doubao-seed-next'])
  expect(requests).toHaveLength(2)
})

it('shows Ark query permission errors and preserves the endpoint without retrying', async () => {
  authorize(['model:manage'])
  let calls = 0
  fakeApi(() => {
    calls++
    return new Response(
      JSON.stringify({ code: 'model_access_denied', detail: '渠道 Key 无权读取模型列表' }),
      {
        status: 502,
        headers: { 'Content-Type': 'application/json' },
      },
    )
  })
  const wrapper = mount(UpstreamModelPicker, {
    props: { channelId, disabled: false, modelValue: 'ep-existing' },
  })
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('手动输入模型 ID')
  expect(selectControl(wrapper, '上游模型').props('modelValue')).toBe('ep-existing')
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  expect(calls).toBe(1)
})
