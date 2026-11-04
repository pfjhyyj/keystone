import { ApiError, failure, headers, sessionWork } from './client'
import { hashFile } from '../services/upload/hash'

export interface UploadProgress {
  phase: 'hashing' | 'uploading' | 'confirming'
  loaded: number
  total: number
}
interface UploadReceipt {
  id: string
  status?: string
}
interface TransferOptions {
  signal?: AbortSignal
  onProgress?: (progress: UploadProgress) => void
}
export async function uploadFile(
  path: string,
  file: File,
  requestKey: string,
  storageKey?: string,
  options: TransferOptions = {},
): Promise<UploadReceipt> {
  const work = sessionWork(options.signal)
  const progress = (phase: UploadProgress['phase'], loaded: number) => {
    work.assert()
    work.signal.throwIfAborted()
    options.onProgress?.({ phase, loaded, total: file.size })
  }
  try {
    progress('hashing', 0)
    const checksum = await hashFile(file, work.signal, (loaded) => progress('hashing', loaded))
    work.assert()
    work.signal.throwIfAborted()
    progress('uploading', 0)
    return await new Promise<UploadReceipt>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      const params = new URLSearchParams({
        name: file.name,
        expected_size: String(file.size),
        content_type: file.type || 'application/octet-stream',
        request_key: requestKey,
        checksum_sha256: checksum,
      })
      if (storageKey) params.set('storage_key', storageKey)
      xhr.open('POST', `/api/v1${path}?${params}`)
      xhr.timeout = 0 // The server owns the upload deadline, including its final copy.
      xhr.withCredentials = true
      Object.entries({ ...headers(), 'Content-Type': 'application/octet-stream' }).forEach(
        ([key, value]) => xhr.setRequestHeader(key, value),
      )
      let finished = false
      const finish = (error?: unknown, result?: UploadReceipt) => {
        if (finished) return
        finished = true
        work.signal.removeEventListener('abort', abort)
        xhr.onload = xhr.onerror = xhr.onabort = null
        xhr.upload.onprogress = null
        if (error !== undefined) reject(error)
        else resolve(result!)
      }
      const abort = () => {
        finish(
          new DOMException('已取消；服务端可能已受理，请用同一文件重试确认结果。', 'AbortError'),
        )
        xhr.abort()
      }
      work.signal.addEventListener('abort', abort, { once: true })
      xhr.onabort = () => finish(new DOMException('上传已取消', 'AbortError'))
      xhr.onerror = () => finish(new Error('上传连接中断，请用同一文件重试确认结果。'))
      xhr.upload.onprogress = (event) => {
        if (finished || work.signal.aborted) return
        progress(event.loaded >= file.size ? 'confirming' : 'uploading', event.loaded)
      }
      xhr.onload = () => {
        if (finished) return
        try {
          work.assert()
          work.signal.throwIfAborted()
          let data: unknown
          try {
            data = JSON.parse(xhr.responseText)
          } catch {
            /* Preserve HTTP failures. */
          }
          if (xhr.status < 200 || xhr.status >= 300) throw failure(xhr.status, data)
          if (
            !data ||
            typeof data !== 'object' ||
            !('id' in data) ||
            typeof data.id !== 'string' ||
            !data.id
          )
            throw new ApiError(
              502,
              'invalid_response',
              '上传响应不完整，请用同一文件重试确认结果。',
            )
          finish(undefined, data as UploadReceipt)
        } catch (error) {
          finish(error)
        }
      }
      try {
        work.signal.throwIfAborted()
        xhr.send(file)
      } catch (error) {
        finish(error)
      }
    })
  } catch (error) {
    work.assert()
    // The XHR is already settled before invalidation can synchronously abort it.
    if (error instanceof ApiError) work.fail(error)
    throw error
  } finally {
    work.close()
  }
}

export async function downloadFile(path: string, name: string, signal?: AbortSignal) {
  const work = sessionWork(signal, 65000)
  const url = `/api/v1${path}`
  let response: Response | undefined
  try {
    // Check access without accumulating the file in JS memory. Native GET reauthorizes.
    response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: work.signal,
      headers: { Range: 'bytes=0-0' },
    })
    if (response.status === 416 && response.headers.get('content-range') === 'bytes */0') {
      await response.body?.cancel()
      response = await fetch(url, {
        method: 'HEAD',
        credentials: 'same-origin',
        cache: 'no-store',
        signal: work.signal,
      })
    }
    work.assert()
    if (!response.ok)
      work.fail(failure(response.status, await response.json().catch(() => undefined)))
    await response.body?.cancel()
    work.assert()
    work.signal.throwIfAborted()
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.hidden = true
    document.body.append(link)
    try {
      link.click()
    } finally {
      link.remove()
    }
  } catch (error) {
    work.rethrow(error)
  } finally {
    await response?.body?.cancel().catch(() => {})
    work.close()
  }
}
