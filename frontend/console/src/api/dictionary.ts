import { requestPage } from './client'
export interface Definition {
  id: string
  key: string
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  updated_at: string
}
export interface DictionaryItem {
  id: string
  dictionary_id: string
  value: string
  label: string
  sort_order: number
  is_active: boolean
  created_at: string
  updated_at: string
}
export const dictionaryApi = {
  listPage: (offset: number, limit: number) =>
    requestPage<Definition>('/dictionaries', offset, limit),
  itemsPage: (id: string, offset: number, limit: number) =>
    requestPage<DictionaryItem>(`/dictionaries/${id}/items`, offset, limit),
}
