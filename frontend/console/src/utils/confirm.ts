import { ElMessageBox } from 'element-plus'

let confirming = false

export async function confirmAction(
  message: string,
  opts: {
    title?: string
    confirmText?: string
    cancelText?: string
    danger?: boolean
    signal?: AbortSignal
  } = {},
): Promise<boolean> {
  if (confirming || opts.signal?.aborted) return false
  confirming = true
  let cancel!: () => void
  const cancelled = new Promise<false>((resolve) => {
    cancel = () => resolve(false)
  })
  const abort = () => {
    ElMessageBox.close()
    cancel()
  }
  opts.signal?.addEventListener('abort', abort, { once: true })
  try {
    const accepted = await Promise.race([
      ElMessageBox.confirm(message, opts.title ?? '请确认', {
        type: 'warning',
        customClass: 'action-confirm',
        confirmButtonText: opts.confirmText ?? '确定',
        cancelButtonText: opts.cancelText ?? '取消',
        confirmButtonType: opts.danger ? 'danger' : 'primary',
      }).then(() => true),
      cancelled,
    ])
    return accepted && !opts.signal?.aborted
  } catch {
    return false
  } finally {
    opts.signal?.removeEventListener('abort', abort)
    confirming = false
  }
}
