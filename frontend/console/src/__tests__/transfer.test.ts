import { flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { uploadFile, downloadFile } from '../api/files'
import { hashFile } from '../services/upload/hash'
import { clearSession } from '../stores/session'
import { authorize, deferred, fakeApi } from './support'
import { FakeXHR, FakeWorker, installTransferFakes } from './transfer-support'

beforeEach(() => {
  installTransferFakes()
  authorize(['notes.attachment:write', 'notes.attachment:read'])
})
const file = () => new File(['hello'], 'report.txt', { type: 'text/plain' })
async function start(signal?: AbortSignal, progress = vi.fn()) {
  const pending = uploadFile('/notes/attachments', file(), 'stable-key', 'local', {
    signal,
    onProgress: progress,
  })
  FakeWorker.last.onmessage?.({ data: { loaded: 5 } })
  FakeWorker.last.onmessage?.({ data: { checksum: 'a'.repeat(64) } })
  await flushPromises()
  return { pending, xhr: FakeXHR.last, progress }
}
it('hashes off-thread, uploads the native File with progress, CSRF and no short timeout', async () => {
  const { pending, xhr, progress } = await start()
  expect(xhr.body).toBeInstanceOf(File)
  expect(xhr.timeout).toBe(0)
  expect(xhr.withCredentials).toBe(true)
  expect(xhr.headers['X-CSRF-Token']).toBe('test-csrf')
  const params = new URL(xhr.url, 'http://local').searchParams
  expect(params.get('storage_key')).toBe('local')
  expect(params.get('request_key')).toBe('stable-key')
  expect(params.get('checksum_sha256')).toBe('a'.repeat(64))
  xhr.upload.onprogress?.({ loaded: 5 })
  expect(progress).toHaveBeenLastCalledWith({ phase: 'confirming', loaded: 5, total: 5 })
  xhr.onload?.()
  expect(await pending).toEqual({ id: 'file-1', status: 'active' })
  expect(FakeWorker.last.terminate).toHaveBeenCalledOnce()
  clearSession()
  expect(xhr.abort).not.toHaveBeenCalled()
})
it.each(['caller', 'session'])('stops hashing immediately on %s cancellation', async (kind) => {
  const controller = new AbortController()
  const pending = uploadFile('/notes/attachments', file(), 'stable', 'local', {
    signal: controller.signal,
  })
  const result = pending.catch((error) => error)
  if (kind === 'caller') controller.abort()
  else clearSession()
  expect(FakeWorker.last.terminate).toHaveBeenCalledOnce()
  expect(await result).toMatchObject(
    kind === 'caller' ? { name: 'AbortError' } : { code: 'identity_changed' },
  )
})
it.each(['caller', 'session'])(
  'settles and aborts XHR on %s cancellation without an abort event',
  async (kind) => {
    const controller = new AbortController()
    const { pending, xhr, progress } = await start(controller.signal)
    const lateLoad = xhr.onload,
      lateProgress = xhr.upload.onprogress
    const before = progress.mock.calls.length
    xhr.abort.mockImplementation(() => {})
    const result = pending.catch((error) => error)
    if (kind === 'caller') controller.abort()
    else clearSession()
    lateLoad?.()
    lateProgress?.({ loaded: 5 })
    expect(await result).toMatchObject(
      kind === 'caller' ? { name: 'AbortError' } : { code: 'identity_changed' },
    )
    expect(xhr.abort).toHaveBeenCalledOnce()
    expect(progress).toHaveBeenCalledTimes(before)
  },
)
it.each(['null', '{}', '{"id":2}', '{"id":""}', '{"id":'])(
  'rejects an incomplete successful upload receipt %s',
  async (body) => {
    const { pending, xhr } = await start()
    xhr.responseText = body
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({ code: 'invalid_response', status: 502 })
  },
)
it('keeps a non-JSON HTTP failure, and cleans up before invalidating the session', async () => {
  const { pending, xhr } = await start()
  xhr.status = 401
  xhr.responseText = '<html>unauthenticated</html>'
  xhr.onload?.()
  await expect(pending).rejects.toMatchObject({ status: 401 })
  expect(xhr.abort).not.toHaveBeenCalled()
})
it('keeps the original request key across network failure and a manual retry', async () => {
  const first = await start()
  first.xhr.onerror?.()
  await expect(first.pending).rejects.toThrow('同一文件重试')
  const second = await start()
  second.xhr.onload?.()
  await second.pending
  expect(new URL(first.xhr.url, 'http://local').searchParams.get('request_key')).toBe(
    new URL(second.xhr.url, 'http://local').searchParams.get('request_key'),
  )
})
it('releases hash workers on startup failure, execution failure and cancellation', async () => {
  const controller = new AbortController()
  const pending = hashFile(file(), controller.signal, vi.fn())
  controller.abort()
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  expect(FakeWorker.last.terminate).toHaveBeenCalledOnce()
  const failed = hashFile(file(), new AbortController().signal, vi.fn())
  FakeWorker.last.onerror?.()
  await expect(failed).rejects.toThrow('校验线程失败')
  expect(FakeWorker.last.terminate).toHaveBeenCalledOnce()
  class BrokenWorker extends FakeWorker {
    postMessage = vi.fn(() => {
      throw new Error('cannot clone')
    })
  }
  vi.stubGlobal('Worker', BrokenWorker)
  await expect(hashFile(file(), new AbortController().signal, vi.fn())).rejects.toThrow('无法启动')
  expect(FakeWorker.last.terminate).toHaveBeenCalledOnce()
})
it('hands downloads to the browser without buffering a Blob and preserves filenames', async () => {
  const cancel = vi.fn()
  fakeApi(() => new Response(new ReadableStream({ cancel }), { status: 206 }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    expect(this.getAttribute('href')).toBe('/api/v1/notes/attachments/id/content')
    expect(this.download).toBe('报告.mp4')
  })
  await downloadFile('/notes/attachments/id/content', '报告.mp4')
  expect(click).toHaveBeenCalledOnce()
  expect(cancel).toHaveBeenCalledOnce()
  expect(document.querySelector('a')).toBeNull()
})
it('supports empty-file downloads and retains download authorization errors', async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fakeApi((_path, init) =>
    init.method === 'HEAD'
      ? new Response(null, { status: 200 })
      : new Response(null, { status: 416, headers: { 'Content-Range': 'bytes */0' } }),
  )
  await downloadFile('/notes/attachments/empty/content', 'empty.txt')
  expect(click).toHaveBeenCalledOnce()
  fakeApi(
    () =>
      new Response('{"code":"file_not_found","detail":"文件不存在或不可访问"}', { status: 404 }),
  )
  await expect(downloadFile('/notes/attachments/id/content', 'name')).rejects.toMatchObject({
    status: 404,
    code: 'file_not_found',
  })
  expect(click).toHaveBeenCalledOnce()
})
it('does not start a native download after a session switch during preflight', async () => {
  const late = deferred<unknown>()
  fakeApi(() => late.promise)
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const pending = downloadFile('/notes/attachments/id/content', 'name').catch((error) => error.code)
  clearSession()
  late.resolve(new Response('x', { status: 206 }))
  expect(await pending).toBe('identity_changed')
  expect(click).not.toHaveBeenCalled()
})
