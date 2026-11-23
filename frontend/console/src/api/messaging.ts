import { request } from './client'
export interface MessageChannel {
  id: string
  name: string
  provider: 'feishu_webhook'
  is_active: boolean
  has_credential: boolean
  destination: { id: string; key: string; is_active: boolean } | null
  created_at: string
  updated_at: string
}
export interface MessageDelivery {
  id: string
  channel_id: string
  destination_key: string | null
  source_module: string
  source_type: string
  source_id: string
  kind: 'text' | 'card'
  status: 'pending' | 'cancelled' | 'sending' | 'succeeded' | 'failed' | 'unknown' | 'expired'
  attempts: number
  error_code: string | null
  expires_at: string | null
  created_at: string
  sent_at: string | null
}
export const messagingApi = {
  channels: (offset: number) =>
    request<MessageChannel[]>(`/messaging/channels?offset=${offset}&limit=50`),
  deliveries: (offset: number) =>
    request<MessageDelivery[]>(`/messaging/deliveries?offset=${offset}&limit=50`),
}
