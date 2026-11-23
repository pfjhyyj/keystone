import { request } from './client'
export type MessageProvider = 'feishu_webhook' | 'feishu_app'
export interface MessageChannel {
  id: string
  name: string
  provider: MessageProvider
  app_id: string | null
  is_active: boolean
  has_credential: boolean
  destination: { id: string; key: string; is_active: boolean } | null
  created_at: string
  updated_at: string
}
export interface MessageDestination {
  id: string
  key: string
  channel_id: string
  external_id: string | null
  name: string
  is_active: boolean
  created_at: string
  updated_at: string
}
export interface MessageChat {
  chat_id: string
  name: string
  external: boolean
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
  error_hint: string | null
  expires_at: string | null
  created_at: string
  sent_at: string | null
}
export const messagingApi = {
  channels: (offset: number) =>
    request<MessageChannel[]>(`/messaging/channels?offset=${offset}&limit=50`),
  // Group bot keys live on their channel; only app channels list destinations separately.
  appDestinations: (offset: number) =>
    request<MessageDestination[]>(
      `/messaging/destinations?provider=feishu_app&offset=${offset}&limit=50`,
    ),
  chats: (channelId: string, pageToken?: string) =>
    request<{ items: MessageChat[]; page_token: string | null }>(
      `/messaging/channels/${channelId}/chats${
        pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : ''
      }`,
    ),
  deliveries: (offset: number) =>
    request<MessageDelivery[]>(`/messaging/deliveries?offset=${offset}&limit=50`),
}
