import { request } from './client'
export interface ManagedUser {
  id: string
  username: string
  display_name: string
  is_active: boolean
  password_change_required: boolean
  role_ids: string[]
  created_at: string
  updated_at: string
}
export interface Role {
  id: string
  key: string
  name: string
  description: string
  permissions: string[]
}
export interface Permission {
  key: string
  name: string
  description: string
}
export interface AuditLog {
  id: string
  occurred_at: string
  actor_type: 'user' | 'system' | 'anonymous'
  actor_id: string | null
  action: string
  target_type: string | null
  target_id: string | null
  outcome: 'success' | 'failure' | 'denied'
  correlation_id: string | null
  details: unknown
}
export const adminApi = {
  users: (offset: number, q = '') =>
    request<ManagedUser[]>(
      `/users?${new URLSearchParams({ offset: String(offset), limit: '50', q })}`,
    ),
  roles: (offset: number) => request<Role[]>(`/roles?offset=${offset}&limit=50`),
  permissions: () => request<Permission[]>('/permissions'),
  audit: (offset: number, filters: Record<string, string>) =>
    request<AuditLog[]>(
      `/audit-logs?${new URLSearchParams({ ...filters, offset: String(offset), limit: '50' })}`,
    ),
}
