import { afterEach, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ListPager from '../components/ListPager.vue'
import FlowRunDiagnostics from '../views/flows/components/FlowRunDiagnostics.vue'
import InvocationDialog from '../views/models/components/InvocationDialog.vue'
import { authorize, chooseSelect, deferred, fakeApi } from './support'
const runId = '11111111-1111-4111-8111-111111111111',
  invocationId = '22222222-2222-4222-8222-222222222222',
  fileId = '33333333-3333-4333-8333-333333333333'
const props = { id: runId, refresh: 1, steps: null }
const page = <T>(items: T[], total = items.length) => ({
  available: true,
  items,
  truncated: false,
  next_offset: null,
  total,
})
afterEach(() => localStorage.clear())
const invocation = (id: string) => ({
  invocation_id: id,
  operation: 'text.generate',
  status: 'succeeded',
  provider_model: 'provider-model',
  model_name: '文稿模型',
  channel_name: '测试渠道',
  duration_ms: 18300,
  model_role: 'writer',
  node_key: 'draft',
  agent_key: 'author',
  agent_call_id: invocationId,
  workflow_id: 'original-workflow',
  step_id: 3,
  error_code: null,
})
it('keeps an opened invocation through polling and clears it when authority changes', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  fakeApi(defaults)
  const wrapper = mount(FlowRunDiagnostics, {
    props,
    global: {
      stubs: { InvocationDialog: { props: ['id'], template: '<div data-invocation>{{id}}</div>' } },
    },
  })
  await flushPromises()
  const summary = wrapper.get('.invocation-summary')
  expect(summary.text()).toContain('文稿模型')
  expect(summary.text()).toContain('文本生成 · 测试渠道 · 耗时 18 秒')
  expect(summary.get('details').attributes('open')).toBeUndefined()
  expect(wrapper.get('.flow-invocation-link').text()).toBe('查看调用')
  await wrapper.get('.flow-invocation-link').trigger('click')
  expect(wrapper.findComponent(InvocationDialog).props('id')).toBe(invocationId)
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  expect(wrapper.findComponent(InvocationDialog).props('id')).toBe(invocationId)
  authorize(['flow:read'])
  await flushPromises()
  expect(wrapper.findComponent(InvocationDialog).exists()).toBe(false)
})
const button = (wrapper: VueWrapper, text: string) =>
  wrapper.findAll('button').find((item) => item.text() === text)!
const query = (path: string) => new URLSearchParams(path.split('?')[1])
const section = (path: string) => query(path).get('section')
function defaults(path: string) {
  if (section(path) === 'models') return { run_id: runId, models: page([invocation(invocationId)]) }
  return {
    run_id: runId,
    files: page([{ file_id: fileId, slot: 'artifact.report', invocation_id: null }]),
  }
}
it('isolates failed model diagnostics from files and supports retry', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  let failed = true
  fakeApi((path) =>
    section(path) === 'models' && failed
      ? new Response(JSON.stringify({ detail: '调用诊断暂不可用' }), { status: 503 })
      : defaults(path),
  )
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain('调用诊断暂不可用')
  expect(wrapper.text()).toContain(fileId)
  expect(wrapper.text()).not.toContain('下载文件')
  failed = false
  await button(wrapper, '重试调用诊断').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain(invocationId)
  expect(wrapper.text()).not.toContain('调用诊断暂不可用')
})
it('correlates exact workflow and step, agent, role and generated file source', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  fakeApi((path) =>
    section(path) === 'files'
      ? {
          run_id: runId,
          files: page([
            {
              file_id: fileId,
              slot: `artifact.model.${invocationId}.0`,
              invocation_id: invocationId,
            },
          ]),
        }
      : defaults(path),
  )
  const wrapper = mount(FlowRunDiagnostics, {
    props: {
      ...props,
      steps: {
        available: true,
        truncated: false,
        items: [
          {
            workflow_id: 'other-workflow',
            step_id: 3,
            name: 'wrong-step',
            started_at_epoch_ms: null,
            completed_at_epoch_ms: null,
          },
          {
            workflow_id: 'original-workflow',
            step_id: 3,
            name: 'write-draft',
            started_at_epoch_ms: null,
            completed_at_epoch_ms: null,
          },
        ],
      },
    },
  })
  await flushPromises()
  expect(wrapper.text()).toContain('步骤：write-draft')
  expect(wrapper.text()).not.toContain('wrong-step')
  const technical = wrapper.get('.invocation-summary details')
  expect(technical.text()).toContain('original-workflow / 3')
  expect(technical.text()).toContain('Agent')
  expect(technical.text()).toContain(`author / ${invocationId}`)
  expect(technical.text()).toContain('writer')
  const file = wrapper.get('[aria-label="关联文件"] details')
  expect(file.get('summary').text()).toBe('模型产物')
  expect(file.text()).toContain(invocationId)
})
it('polls both sections on their current pages and ignores a superseded page', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  const oldPage = deferred<unknown>(),
    paths: string[] = []
  let refreshed = false
  fakeApi((path) => {
    paths.push(path)
    if (section(path) === 'models') {
      const next = query(path).get('offset') === '50'
      if (next && !refreshed) return oldPage.promise
      return {
        run_id: runId,
        models: page([invocation(next ? 'new-result' : 'original-result')], 130),
      }
    }
    return defaults(path)
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  const pagers = wrapper.findAllComponents(ListPager)
  // Single-page files section hides its pager; multi-page models section keeps it.
  expect(pagers).toHaveLength(1)
  const models = pagers[0]!
  expect(models.text()).toContain('共 3 页')
  await button(models, '下一页').trigger('click')
  await flushPromises()
  expect(models.props('loading')).toBe(true)
  refreshed = true
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  oldPage.resolve({ run_id: runId, models: page([invocation('late-result')], 130) })
  await flushPromises()
  expect(paths.map((path) => [section(path), query(path).get('offset')])).toEqual([
    ['models', '0'],
    ['files', '0'],
    ['models', '50'],
    ['models', '50'],
    ['files', '0'],
  ])
  expect(paths.every((path) => query(path).get('include_total') === 'true')).toBe(true)
  expect(models.text()).toContain('第 2 页')
  expect(wrapper.text()).toContain('new-result')
  expect(wrapper.text()).not.toContain('late-result')
  expect(wrapper.text()).not.toContain('original-result')
})
it('jumps file pages, clamps after totals shrink, and resets independently on size or Run change', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  const requests: string[] = []
  let total = 130
  fakeApi((path) => {
    requests.push(path)
    if (section(path) === 'models') return defaults(path)
    const offset = Number(query(path).get('offset'))
    const items =
      offset < total ? [{ file_id: `file-${offset}`, slot: 'input', invocation_id: null }] : []
    return { run_id: runId, files: page(items, total) }
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  const filePager = () => wrapper.findAllComponents(ListPager)[0]!
  await filePager().get('input[aria-label="跳转页码"]').setValue('3')
  await filePager().get('form').trigger('submit')
  await flushPromises()
  expect(filePager().props('page')).toBe(2)
  expect(wrapper.text()).toContain('file-100')
  expect(wrapper.text()).not.toContain('file-0')

  total = 75
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  expect(filePager().props('page')).toBe(1)
  expect(wrapper.text()).toContain('file-50')
  expect(
    requests
      .filter((path) => section(path) === 'files')
      .slice(-2)
      .map((path) => query(path).get('offset')),
  ).toEqual(['100', '50'])

  await chooseSelect(filePager(), '20', '每页条数')
  expect(filePager().props('page')).toBe(0)
  expect(filePager().props('size')).toBe(20)
  expect(query(requests.at(-1)!).get('limit')).toBe('20')
  await button(filePager(), '下一页').trigger('click')
  await flushPromises()
  expect(filePager().props('page')).toBe(1)

  await wrapper.setProps({ id: 'another-run' })
  await flushPromises()
  expect(filePager().props('page')).toBe(0)
  expect(filePager().props('size')).toBe(20)
  const newRun = requests.slice(-2)
  expect(newRun.every((path) => path.startsWith('/flow-runs/another-run/diagnostics?'))).toBe(true)
  expect(newRun.map((path) => query(path).get('offset'))).toEqual(['0', '0'])
})
it('does not fetch unauthorized resource panels', async () => {
  authorize(['flow:read'])
  const paths: string[] = []
  fakeApi((path) => {
    paths.push(path)
    return defaults(path)
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(paths.map(section).sort()).toEqual(['files'])
  expect(wrapper.text()).toContain('无查看模型调用权限')
  expect(wrapper.text()).toContain(fileId)
})
it('clears revoked panels and ignores their outstanding results', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  const oldPage = deferred<unknown>()
  fakeApi((path) => (section(path) === 'models' ? oldPage.promise : defaults(path)))
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain(fileId)
  authorize(['flow:read'])
  await flushPromises()
  oldPage.resolve(defaults('/diagnostics?section=models'))
  await flushPromises()
  expect(wrapper.text()).not.toContain(invocationId)
  expect(wrapper.text()).toContain(fileId)
  expect(wrapper.text()).toContain('无查看模型调用权限')
})
it('shows missing Step positions explicitly and clears all data on Run access loss', async () => {
  authorize(['flow:read', 'model_invocation:read'])
  let lost = false
  const lateFiles = deferred<unknown>()
  fakeApi((path) => {
    if (lost)
      return section(path) === 'files'
        ? lateFiles.promise
        : new Response(JSON.stringify({ code: 'flow_not_found', detail: '流程不可访问' }), {
            status: 404,
          })
    if (section(path) === 'models')
      return {
        run_id: runId,
        models: page([{ ...invocation(invocationId), workflow_id: null, step_id: null }]),
      }
    return defaults(path)
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain('历史 Step 位置未记录')
  lost = true
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  expect(wrapper.emitted('unavailable')).toEqual([['流程不可访问']])
  lateFiles.resolve(defaults('/diagnostics?section=files'))
  await flushPromises()
  expect(wrapper.text()).not.toContain(invocationId)
  expect(wrapper.text()).not.toContain(fileId)
  expect(wrapper.findAllComponents(ListPager)).toHaveLength(0)
})
