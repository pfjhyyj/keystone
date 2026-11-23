import { request } from './client'
export type MessageProvider = 'feishu_webhook' | 'feishu_app'
export interface MessageConnection {
  state: 'connected' | 'connecting' | 'refused' | 'lost' | 'offline'
  workers: number
  error_code?: string | null
  error_hint?: string | null
}
export interface MessageChannel {
  id: string
  name: string
  provider: MessageProvider
  app_id: string | null
  is_active: boolean
  has_credential: boolean
  destination: { id: string; key: string; is_active: boolean } | null
  // App channels only.
  connection?: MessageConnection
  groups?: number
  members?: number
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
export interface MessageInbound {
  id: string
  channel_id: string
  kind: 'card_action' | 'bind'
  delivery_id: string | null
  action_key: string | null
  external_user_id: string
  actor: { id: string; label: string | null } | null
  status: 'received' | 'processed' | 'rejected' | 'expired' | 'failed'
  attempts: number
  error_code: string | null
  error_hint: string | null
  received_at: string
  processed_at: string | null
}
export interface BindCode {
  code: string
  expires_at: string
}
export interface MyBinding {
  channel_id: string
  channel_name: string
  account: { id: string; external_user_id: string; created_at: string } | null
}
// A value with "@" is looked up as an email, anything else as a mobile number.
export const contactField = (value: string) =>
  value.includes('@') ? { email: value } : { mobile: value }
// Only complete values are sent to Feishu: an email, a mainland mobile or one with "+".
export const completeContact = (value: string) =>
  /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value) || /^(1\d{10}|\+\d{6,20})$/.test(value)
export interface RecordFilter {
  channelId?: string
  status?: string
}
const recordQuery = (offset: number, filter: RecordFilter) =>
  new URLSearchParams({
    offset: String(offset),
    limit: '50',
    ...(filter.channelId ? { channel_id: filter.channelId } : {}),
    ...(filter.status ? { status: filter.status } : {}),
  }).toString()
export const messagingApi = {
  channels: (offset: number) =>
    request<MessageChannel[]>(`/messaging/channels?offset=${offset}&limit=50`),
  channel: (id: string) => request<MessageChannel>(`/messaging/channels/${id}`),
  // Group bot keys live on their channel; only app channels list their groups separately.
  groups: (channelId: string, offset: number) =>
    request<MessageDestination[]>(
      `/messaging/destinations?channel_id=${channelId}&offset=${offset}&limit=50`,
    ),
  chats: (channelId: string, pageToken?: string) =>
    request<{ items: MessageChat[]; page_token: string | null }>(
      `/messaging/channels/${channelId}/chats${
        pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : ''
      }`,
    ),
  members: (channelId: string, offset: number) =>
    request<MessageAccount[]>(
      `/messaging/accounts?channel_id=${channelId}&offset=${offset}&limit=50`,
    ),
  // POST keeps the email or mobile out of URLs and request logs.
  lookup: (channelId: string, contact: string) =>
    request<MessageLookup>('/messaging/accounts/lookup', 'POST', {
      channel_id: channelId,
      ...contactField(contact),
    }),
  deliveries: (offset: number, filter: RecordFilter = {}) =>
    request<MessageDelivery[]>(`/messaging/deliveries?${recordQuery(offset, filter)}`),
  inbounds: (offset: number, filter: RecordFilter = {}) =>
    request<MessageInbound[]>(`/messaging/inbounds?${recordQuery(offset, filter)}`),
  // The signed-in user's own code; it is shown once and only its digest is stored.
  bindCode: () => request<BindCode>('/messaging/bind-codes', 'POST'),
  myBindings: () => request<MyBinding[]>('/messaging/me/accounts'),
  unbindMine: (id: string) => request<null>(`/messaging/me/accounts/${id}`, 'DELETE'),
}
