export function hashFile(
  file: File,
  signal: AbortSignal,
  progress: (loaded: number) => void,
): Promise<string> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted()
    const worker = new Worker(new URL('./hash.worker.ts', import.meta.url), { type: 'module' })
    let finished = false
    const finish = (error?: Error, checksum?: string) => {
      if (finished) return
      finished = true
      worker.terminate()
      signal.removeEventListener('abort', abort)
      if (error) reject(error)
      else resolve(checksum!)
    }
    const abort = () => finish(new DOMException('已取消', 'AbortError'))
    signal.addEventListener('abort', abort, { once: true })
    worker.onerror = () => finish(new Error('校验线程失败，请重试'))
    worker.onmessage = ({ data }) => {
      if (finished || signal.aborted) return
      if (data.error) finish(new Error(data.error))
      else if (data.checksum) finish(undefined, data.checksum)
      else progress(data.loaded)
    }
    try {
      worker.postMessage(file)
    } catch {
      finish(new Error('无法启动文件校验，请重试'))
    }
  })
}
