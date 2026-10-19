import { request } from './auth'
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
  list: (offset: number) => request<Definition[]>(`/dictionaries?offset=${offset}&limit=50`),
  items: (id: string, offset: number) =>
    request<DictionaryItem[]>(`/dictionaries/${id}/items?offset=${offset}&limit=50`),
}
