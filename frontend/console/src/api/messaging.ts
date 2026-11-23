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
export interface MessageAccount {
  id: string
  channel_id: string
  user: { id: string; label: string | null }
  external_user_id: string
  created_at: string
}
export interface MessageLookup {
  external_user_id: string
  name: string | null
  name_hint: string | null
  bound_to: { id: string; label: string } | null
}
export interface MessageDelivery {
  id: string
  channel_id: string
  destination_key: string | null
  recipient: { id: string; label: string | null } | null
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
// A value with "@" is looked up as an email, anything else as a mobile number.
export const contactField = (value: string) =>
  value.includes('@') ? { email: value } : { mobile: value }
// Only complete values are sent to Feishu: an email, a mainland mobile or one with "+".
export const completeContact = (value: string) =>
  /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value) || /^(1\d{10}|\+\d{6,20})$/.test(value)
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
  accounts: (offset: number) =>
    request<MessageAccount[]>(`/messaging/accounts?offset=${offset}&limit=50`),
  // POST keeps the email or mobile out of URLs and request logs.
  lookup: (channelId: string, contact: string) =>
    request<MessageLookup>('/messaging/accounts/lookup', 'POST', {
      channel_id: channelId,
      ...contactField(contact),
    }),
  deliveries: (offset: number) =>
    request<MessageDelivery[]>(`/messaging/deliveries?offset=${offset}&limit=50`),
}
