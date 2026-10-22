import { vi } from 'vitest'

export class FakeWorker {
  static last: FakeWorker
  onmessage?: (event: { data: unknown }) => void
  onerror?: () => void
  terminate = vi.fn()
  postMessage = vi.fn()
  constructor() {
    FakeWorker.last = this
  }
}
export class FakeXHR {
  static last: FakeXHR
  upload: { onprogress?: ((event: { loaded: number }) => void) | null } = {}
  onload?: (() => void) | null
  onabort?: (() => void) | null
  onerror?: (() => void) | null
  status = 201
  responseText = '{"id":"file-1","status":"active"}'
  timeout = 999
  withCredentials = false
  url = ''
  body?: unknown
  headers: Record<string, string> = {}
  constructor() {
    FakeXHR.last = this
  }
  open(_method: string, url: string) {
    this.url = url
  }
  setRequestHeader(key: string, value: string) {
    this.headers[key] = value
  }
  send(body: unknown) {
    this.body = body
  }
  abort = vi.fn(() => this.onabort?.())
}
export function installTransferFakes(automatic = false) {
  class AutoWorker extends FakeWorker {
    postMessage = vi.fn(() => {
      queueMicrotask(() => this.onmessage?.({ data: { checksum: 'a'.repeat(64) } }))
    })
  }
  class AutoXHR extends FakeXHR {
    send(body: File) {
      super.send(body)
      void fetch(this.url, { method: 'POST', headers: this.headers, body })
        .then(async (response) => {
          this.status = response.status
          this.responseText = await response.text()
          this.onload?.()
        })
        .catch(() => this.onerror?.())
    }
  }
  vi.stubGlobal('Worker', automatic ? AutoWorker : FakeWorker)
  vi.stubGlobal('XMLHttpRequest', automatic ? AutoXHR : FakeXHR)
}
