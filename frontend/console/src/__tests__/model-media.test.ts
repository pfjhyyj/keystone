import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { ElUpload } from 'element-plus'
import ModelProbe from '../views/models/components/ModelProbe.vue'
import ModelPicker from '../components/models/ModelPicker.vue'
import { modelApi } from '../api/models'
import * as transport from '../api/client'
import * as files from '../api/files'
import { authorize, chooseSelect, deferred, fakeApi } from './support'

const permissions = ['model:invoke', 'file:create', 'file:content_read', 'model_invocation:read']
const fileId = '11111111-1111-4111-8111-111111111111'
function probe() {
  return mount(ModelProbe, { global: { stubs: { RouterLink: RouterLinkStub } } })
}
async function operation(wrapper: ReturnType<typeof probe>, label: string) {
  await wrapper.get(`[role=tab][id$="-${label}"]`).trigger('click')
  await flushPromises()
}
function catalog(parameters: string[] = []) {
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return {
      items: [
        {
          key: 'image-model',
          operations: ['image.generate', 'image.edit', 'image.understand'],
          image_parameters: parameters,
          max_reference_images: path.includes('image.understand') ? 4 : 9,
          max_input_bytes: (path.includes('image.understand') ? 8 : 32) * 1024 * 1024,
        },
      ],
    }
  })
  return paths
}
function attach(
  wrapper: ReturnType<typeof probe>,
  raw = new File(['image'], 'reference.png', { type: 'image/png' }),
) {
  wrapper.findComponent(ElUpload).vm.$emit('update:fileList', [{ name: raw.name, uid: 1, raw }])
}
afterEach(() => vi.restoreAllMocks())

it('filters models by operation and sends supported generation controls only', async () => {
  authorize(permissions)
  const paths = catalog(['aspect_ratio', 'quality'])
  const call = vi
    .spyOn(modelApi, 'media')
    .mockResolvedValue({ invocation_id: 'generation-1', files: [{ file_id: fileId }] })
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.generate')
  expect(paths.at(-1)).toContain('operation=image.generate')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('a blue dress')
  await chooseSelect(wrapper, '3:4', '图片比例')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(call).toHaveBeenCalledTimes(1)
  expect(call.mock.calls[0]!.slice(0, 2)).toEqual([
    'image.generate',
    {
      model_key: 'image-model',
      prompt: 'a blue dress',
      n: 1,
      resolution: '1K',
      output_format: 'png',
      aspect_ratio: '3:4',
      quality: 'medium',
    },
  ])
  expect(wrapper.find('[aria-label="生成图片"]').exists()).toBe(true)
  expect(wrapper.text()).toContain('generation-1')
  expect(wrapper.getComponent(RouterLinkStub).props('to')).toMatchObject({
    query: { invocation: 'generation-1' },
  })
})

it('does not send unsupported optional controls and does not reuse a model across modes', async () => {
  authorize(permissions)
  catalog()
  const call = vi.spyOn(modelApi, 'media').mockResolvedValue({ invocation_id: 'one', files: [] })
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.generate')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('blue')
  expect(wrapper.find('[aria-label="图片比例"]').exists()).toBe(false)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(call.mock.calls[0]![1]).not.toHaveProperty('quality')
  expect(call.mock.calls[0]![1]).not.toHaveProperty('aspect_ratio')
  await operation(wrapper, 'image.understand')
  expect(wrapper.text()).not.toContain('本次调用：one')
  await wrapper.get('form').trigger('submit')
  expect(call).toHaveBeenCalledTimes(1)
})

it('uploads references through Files and reuses the upload on an explicit edit resubmission', async () => {
  authorize(permissions)
  catalog()
  const upload = vi.spyOn(files, 'uploadFile').mockResolvedValue({ id: fileId })
  const call = vi
    .spyOn(modelApi, 'media')
    .mockResolvedValue({ invocation_id: 'edit-1', files: [{ file_id: fileId }] })
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.edit')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('make it blue')
  await wrapper.get('form').trigger('submit')
  expect(call).not.toHaveBeenCalled()
  attach(wrapper)
  await flushPromises()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(upload).toHaveBeenCalledTimes(1)
  expect(call.mock.calls[0]![1]).toMatchObject({
    images: [{ file_id: fileId }],
    prompt: 'make it blue',
  })
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(upload).toHaveBeenCalledTimes(1)
  expect(call).toHaveBeenCalledTimes(2)
})

it('uses typed vision messages and shows the returned text', async () => {
  authorize(permissions)
  catalog()
  vi.spyOn(files, 'uploadFile').mockResolvedValue({ id: fileId })
  const call = vi
    .spyOn(modelApi, 'media')
    .mockResolvedValue({ invocation_id: 'vision-1', content: '蓝色服装' })
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.understand')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('describe')
  attach(wrapper)
  await flushPromises()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(call.mock.calls[0]!.slice(0, 2)).toEqual([
    'image.understand',
    {
      model_key: 'image-model',
      messages: [{ role: 'user', content: 'describe' }],
      images: [{ file_id: fileId }],
      max_tokens: 1024,
    },
  ])
  expect(wrapper.get('pre').text()).toBe('蓝色服装')
})

it('blocks missing file permissions before invoking', async () => {
  authorize(['model:invoke'])
  catalog()
  const call = vi.spyOn(modelApi, 'media')
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.generate')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('blue')
  await wrapper.get('form').trigger('submit')
  expect(call).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('当前操作需要模型调用权限')
})

it.each(['text.generate', 'image.understand'])(
  'explains empty %s output, retains its invocation and does not resend',
  async (mode) => {
    authorize(permissions)
    catalog()
    vi.spyOn(files, 'uploadFile').mockResolvedValue({ id: fileId })
    const call = vi
      .spyOn(modelApi, 'media')
      .mockResolvedValue({ invocation_id: 'empty-result', content: '' })
    const text = vi.spyOn(transport, 'invokeModel').mockImplementation(async (_body, onEvent) => {
      onEvent({ type: 'done', invocation_id: 'empty-result', content: ' \n' })
    })
    const wrapper = probe()
    await flushPromises()
    await operation(wrapper, mode)
    await chooseSelect(wrapper, 'image-model', '模型')
    await wrapper.get('textarea').setValue('describe')
    if (mode === 'image.understand') attach(wrapper)
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('本次调用未返回可显示的文本')
    expect(wrapper.text()).toContain('输出上限包含推理 Token')
    expect(wrapper.text()).toContain('empty-result')
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toMatchObject({
      query: { invocation: 'empty-result' },
    })
    expect(call).toHaveBeenCalledTimes(mode === 'image.understand' ? 1 : 0)
    expect(text).toHaveBeenCalledTimes(mode === 'text.generate' ? 1 : 0)
  },
)

it('uses catalog reference limits and rejects oversized vision input before uploading', async () => {
  authorize(permissions)
  catalog()
  const upload = vi.spyOn(files, 'uploadFile')
  const call = vi.spyOn(modelApi, 'media')
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.edit')
  await chooseSelect(wrapper, 'image-model', '模型')
  expect(wrapper.findComponent(ElUpload).props('limit')).toBe(9)
  await operation(wrapper, 'image.understand')
  await chooseSelect(wrapper, 'image-model', '模型')
  expect(wrapper.findComponent(ElUpload).props('limit')).toBe(4)
  await wrapper.get('textarea').setValue('describe')
  wrapper.findComponent(ElUpload).vm.$emit(
    'update:fileList',
    Array.from({ length: 3 }, (_, uid) => ({
      name: `reference-${uid}.png`,
      uid,
      raw: new File([new Uint8Array(3 * 1024 * 1024)], `reference-${uid}.png`, {
        type: 'image/png',
      }),
    })),
  )
  await flushPromises()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('参考图总大小不能超过 8 MiB')
  expect(upload).not.toHaveBeenCalled()
  expect(call).not.toHaveBeenCalled()
})

it('aborts the direct request on disposal, suppresses late results and never auto-retries', async () => {
  authorize(permissions)
  catalog()
  const pending = deferred<{ invocation_id: string; files: { file_id: string }[] }>()
  const call = vi.spyOn(modelApi, 'media').mockReturnValue(pending.promise)
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.generate')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('blue')
  await wrapper.get('form').trigger('submit')
  await wrapper.get('form').trigger('submit')
  expect(call).toHaveBeenCalledTimes(1)
  const signal = call.mock.calls[0]![2]
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  pending.resolve({ invocation_id: 'late', files: [] })
  await flushPromises()
  expect(wrapper.emitted('completed')).toBeUndefined()
})

it('retains selected model parameters across catalog pagination', async () => {
  authorize(permissions)
  fakeApi((path) => ({
    items: path.includes('offset=50')
      ? [{ key: 'other', operations: [] }]
      : Array.from({ length: 50 }, (_, index) => ({
          key: index ? `model-${index}` : 'chosen',
          operations: ['image.generate'],
          image_parameters: ['quality'],
        })),
  }))
  const wrapper = mount(ModelPicker, {
    props: { modelValue: 'chosen', operation: 'image.generate' },
  })
  await flushPromises()
  await wrapper
    .findAll('button')
    .find((button) => button.text() === '下一页')!
    .trigger('click')
  await flushPromises()
  expect(wrapper.emitted('selected')?.at(-1)?.[0]).toMatchObject({
    key: 'chosen',
    image_parameters: ['quality'],
  })
})

it('does not generate after an upload failure and retains the upload key for manual retry', async () => {
  authorize(permissions)
  catalog()
  const upload = vi
    .spyOn(files, 'uploadFile')
    .mockRejectedValueOnce(new Error('upload interrupted'))
    .mockResolvedValue({ id: fileId })
  const call = vi.spyOn(modelApi, 'media').mockResolvedValue({ invocation_id: 'edit-2', files: [] })
  const wrapper = probe()
  await flushPromises()
  await operation(wrapper, 'image.edit')
  await chooseSelect(wrapper, 'image-model', '模型')
  await wrapper.get('textarea').setValue('blue')
  attach(wrapper)
  await flushPromises()
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(call).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('upload interrupted')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(upload.mock.calls[0]![1]).toBe(upload.mock.calls[1]![1])
  expect(call).toHaveBeenCalledTimes(1)
})
