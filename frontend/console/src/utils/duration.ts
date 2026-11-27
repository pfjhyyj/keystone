export function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null
  if (ms < 1000) return ms ? '不到 1 秒' : '0 秒'
  const total = Math.floor(ms / 1000)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  return [hours && `${hours} 小时`, minutes && `${minutes} 分`, seconds && `${seconds} 秒`]
    .filter(Boolean)
    .join(' ')
}
