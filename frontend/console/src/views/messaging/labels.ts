import type {
  MessageConnection,
  MessageDelivery,
  MessageInbound,
  MessageProvider,
} from '../../api/messaging'

export const providerText: Record<MessageProvider, string> = {
  feishu_webhook: '飞书群机器人',
  feishu_app: '飞书自建应用',
}
export const deliveryText: Record<MessageDelivery['status'], string> = {
  pending: '待发送',
  cancelled: '已取消',
  sending: '发送中',
  succeeded: '已送达',
  failed: '失败',
  unknown: '结果未知',
  expired: '已过期',
}
// [label, StatusTag status]
export const inboundText: Record<MessageInbound['status'], [string, string]> = {
  received: ['处理中', 'pending'],
  processed: ['已处理', 'succeeded'],
  rejected: ['已拒绝', 'denied'],
  expired: ['已过期', 'expired'],
  failed: ['失败', 'failed'],
}
export const connectionText: Record<MessageConnection['state'], [string, string]> = {
  connected: ['已连接', 'active'],
  connecting: ['连接中', 'pending'],
  refused: ['连接被拒', 'failed'],
  lost: ['连接断开', 'pending'],
  offline: ['未连接', 'disabled'],
}
export const time = (value: string | null) => (value ? new Date(value).toLocaleString() : '—')
