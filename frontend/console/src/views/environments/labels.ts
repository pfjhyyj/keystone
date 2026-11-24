import type { Environment, SessionState } from '../../api/environments'

export const stateText: Record<SessionState, string> = {
  starting: '启动中',
  ready: '可连接',
  stopping: '结束中',
  review: '待核对',
  stopped: '已结束',
  failed: '打开失败',
}
export const stateTag: Record<SessionState, string> = {
  starting: 'pending',
  ready: 'active',
  stopping: 'pending',
  review: 'unknown',
  stopped: 'disabled',
  failed: 'failed',
}
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
export const formatTime = (value: string) => dateTime.format(new Date(value))
// Sessions in flight or unconfirmed are only rechecked, never opened over.
export const unsettled = (state?: SessionState) =>
  state === 'starting' || state === 'stopping' || state === 'review'
export const live = (item: Environment) =>
  item.session?.purpose === 'interactive' && item.session.state === 'ready'
export function backupText(item: Environment) {
  if (item.backup_at)
    return `${formatTime(item.backup_at)}${item.backup_failed ? '（最近一次失败）' : ''}`
  return item.backup_failed ? '未成功' : '还没有备份'
}
export const canBackup = (item: Environment) =>
  !!item.id &&
  item.backup_enabled &&
  (!item.session || item.session.state === 'stopped' || item.session.state === 'failed')
