import { expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import FlowRunDiagnostics from '../views/flows/components/FlowRunDiagnostics.vue'
import InvocationDialog from '../views/models/components/InvocationDialog.vue'
import { authorize, deferred, fakeApi } from './support'
const runId = '11111111-1111-4111-8111-111111111111',
  invocationId = '22222222-2222-4222-8222-222222222222',
  fileId = '33333333-3333-4333-8333-333333333333'
const props = { id: runId, refresh: 1, steps: null }
const limits = {
  model_requests: 2,
  tool_calls: 3,
  iterations: 4,
  file_count: 5,
  file_bytes: 100,
  input_tokens: 20,
  output_tokens: 10,
  concurrent_operations: 2,
  temporary_bytes: 100,
}
const counters = {
  model_requests: 1,
  tool_calls: 1,
  iterations: 1,
  file_count: 1,
  file_bytes: 5,
  input_tokens: 0,
  output_tokens: 10,
  unreported_text_requests: 1,
}
const page = <T>(items: T[], next: number | null = null) => ({
  available: true,
  items,
  truncated: next !== null,
  next_offset: next,
})
const invocation = (id: string) => ({
  invocation_id: id,
  operation: 'text.generate',
  status: 'succeeded',
  model_role: 'writer',
  node_key: 'draft',
  agent_key: 'author',
  agent_call_id: invocationId,
  workflow_id: 'original-workflow',
  step_id: 3,
  error_code: null,
})
const budget = { recorded: true, limits, counters }
it('keeps an opened invocation through polling and clears it when authority changes', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
  fakeApi(defaults)
  const wrapper = mount(FlowRunDiagnostics, {
    props,
    global: {
      stubs: { InvocationDialog: { props: ['id'], template: '<div data-invocation>{{id}}</div>' } },
    },
  })
  await flushPromises()
  await wrapper.get('.flow-invocation-link').trigger('click')
  expect(wrapper.findComponent(InvocationDialog).props('id')).toBe(invocationId)
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  expect(wrapper.findComponent(InvocationDialog).props('id')).toBe(invocationId)
  authorize(['flow:read', 'file:inspect'])
  await flushPromises()
  expect(wrapper.findComponent(InvocationDialog).exists()).toBe(false)
})
const button = (wrapper: VueWrapper, text: string) =>
  wrapper.findAll('button').find((item) => item.text() === text)!
const section = (path: string) => new URLSearchParams(path.split('?')[1]).get('section')
function defaults(path: string) {
  if (section(path) === 'budget') return { run_id: runId, budget }
  if (section(path) === 'models') return { run_id: runId, models: page([invocation(invocationId)]) }
  return {
    run_id: runId,
    files: page([{ file_id: fileId, slot: 'artifact.report', invocation_id: null }]),
  }
}
it('isolates failed model diagnostics from budget and files and supports retry', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
  let failed = true
  fakeApi((path) =>
    section(path) === 'models' && failed
      ? new Response(JSON.stringify({ detail: '调用诊断暂不可用' }), { status: 503 })
      : defaults(path),
  )
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain('调用诊断暂不可用')
  expect(wrapper.text()).toContain('输出 token · 已预留')
  expect(wrapper.text()).toContain('未结清文本请求：1')
  expect(wrapper.text()).toContain(fileId)
  expect(wrapper.text()).not.toContain('下载文件')
  failed = false
  await button(wrapper, '重试调用诊断').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain(invocationId)
  expect(wrapper.text()).not.toContain('调用诊断暂不可用')
})
it('correlates exact workflow and step, agent, role and generated file source', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
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
  expect(wrapper.text()).toContain('write-draft · original-workflow / 3')
  expect(wrapper.text()).not.toContain('wrong-step')
  expect(wrapper.text()).toContain('Agent author')
  expect(wrapper.text()).toContain('角色 writer')
  expect(wrapper.text()).toContain(`来源调用 ${invocationId}`)
})
it('paginates at the returned offset and rejects late pages after refresh', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
  const oldPage = deferred<unknown>(),
    paths: string[] = []
  let refreshed = false
  fakeApi((path) => {
    paths.push(path)
    if (section(path) === 'models') {
      if (path.includes('offset=50')) return oldPage.promise
      return {
        run_id: runId,
        models: page([invocation(refreshed ? 'new-result' : 'original-result')], 50),
      }
    }
    return defaults(path)
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  await button(wrapper, '加载更多调用').trigger('click')
  await flushPromises()
  refreshed = true
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  oldPage.resolve({ run_id: runId, models: page([invocation('late-result')]) })
  await flushPromises()
  expect(paths.some((path) => path.includes('section=models&offset=50&limit=50'))).toBe(true)
  expect(wrapper.text()).toContain('new-result')
  expect(wrapper.text()).not.toContain('late-result')
  expect(wrapper.text()).not.toContain('original-result')
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
  expect(paths).toHaveLength(1)
  expect(section(paths[0]!)).toBe('budget')
  expect(wrapper.text()).toContain('无查看模型调用权限')
  expect(wrapper.text()).toContain('无查看文件元数据权限')
})
it('clears revoked panels and ignores their outstanding results', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
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
  expect(wrapper.text()).not.toContain(fileId)
  expect(wrapper.text()).toContain('无查看模型调用权限')
})
it('shows historical omissions explicitly and clears all data on Run access loss', async () => {
  authorize(['flow:read', 'model_invocation:read', 'file:inspect'])
  let lost = false
  fakeApi((path) => {
    if (lost)
      return new Response(JSON.stringify({ code: 'flow_not_found', detail: '流程不可访问' }), {
        status: 404,
      })
    if (section(path) === 'budget')
      return { run_id: runId, budget: { recorded: false, limits: null, counters: null } }
    if (section(path) === 'models')
      return {
        run_id: runId,
        models: page([{ ...invocation(invocationId), workflow_id: null, step_id: null }]),
      }
    return defaults(path)
  })
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain('历史运行未记录此版本累计预算')
  expect(wrapper.text()).toContain('历史 Step 位置未记录')
  lost = true
  await wrapper.setProps({ refresh: 2 })
  await flushPromises()
  expect(wrapper.emitted('unavailable')).toBeTruthy()
  expect(wrapper.text()).not.toContain(invocationId)
  expect(wrapper.text()).not.toContain(fileId)
})

it('keeps budget counts and remaining exact above the JavaScript safe integer range', async () => {
  authorize(['flow:read'])
  fakeApi(() => ({
    run_id: runId,
    budget: {
      recorded: true,
      limits: { ...limits, input_tokens: '9007199254740994' },
      counters: { ...counters, input_tokens: '9007199254740993' },
    },
  }))
  const wrapper = mount(FlowRunDiagnostics, { props })
  await flushPromises()
  expect(wrapper.text()).toContain('9007199254740993 / 9007199254740994；剩余 1')
})
