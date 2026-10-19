import { request } from './client'
export interface StoredFile {
  id: string
  name: string
  owner_id: string
  status: string
  content_type: string
  storage_key: string
  size_bytes: number | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}
export interface FileGrant {
  id: string
  subject_type: 'user' | 'role' | 'authenticated'
  user_id: string | null
  role_id: string | null
  action: 'inspect' | 'content_read' | 'manage'
  created_at: string
  created_by: string | null
}
export interface FileReference {
  owner: string
  slot: string
}
export const fileApi = {
  list: (offset: number) => request<StoredFile[]>(`/files?offset=${offset}&limit=50`),
  grants: (id: string, offset: number) =>
    request<FileGrant[]>(`/files/${id}/grants?offset=${offset}&limit=50`),
  references: (id: string, offset: number) =>
    request<FileReference[]>(`/files/${id}/references?offset=${offset}&limit=50`),
}
export const fileStatus: Record<string, string> = {
  uploading: '上传中',
  active: '可用',
  disabled: '已停用',
  failed: '上传失败',
  deleting: '正在删除',
  deleted: '已删除',
}
export const grantActions = { inspect: '查看信息', content_read: '读取内容', manage: '管理与授权' }
export const uuidPattern =
  '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
