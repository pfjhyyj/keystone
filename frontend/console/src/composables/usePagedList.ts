import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue'
import type { PageResult } from '../api/client'
import { can } from '../stores/session'

export const PAGE_SIZES = [20, 50, 100] as const
export const MAX_LIST_OFFSET = 100_000

export function usePageSize(
  key: string,
  options: readonly number[] = PAGE_SIZES,
  defaultSize = 50,
) {
  const storageKey = `keystone.console.page-size.${key}`
  const size = ref(options.includes(defaultSize) ? defaultSize : (options[0] ?? 50))
  try {
    const saved = Number(localStorage.getItem(storageKey))
    if (options.includes(saved)) size.value = saved
  } catch {
    // Some browser modes disable storage; paging still works for the current view.
  }
  function setSize(value: number) {
    if (!options.includes(value) || size.value === value) return
    size.value = value
    try {
      localStorage.setItem(storageKey, String(value))
    } catch {
      // The current view retains its preference even when storage is unavailable.
    }
  }
  return { size, setSize }
}

export function usePagedList<T>(
  key: string,
  permission: string | (() => boolean),
  loader: (offset: number, limit: number) => Promise<PageResult<T>>,
) {
  const { size, setSize: storeSize } = usePageSize(key)
  const rows = shallowRef<T[]>([]),
    total = ref<number | null>(null),
    page = ref(0),
    loading = ref(false),
    error = ref('')
  const allowed = computed(() => (typeof permission === 'string' ? can(permission) : permission()))
  let generation = 0
  let disposed = false
  async function load(preserve = false, fallbackPage = Math.max(0, page.value - 1)) {
    if (disposed) return
    const current = ++generation
    if (!preserve || !allowed.value) {
      rows.value = []
      total.value = null
    }
    error.value = ''
    loading.value = false
    if (!allowed.value) return
    loading.value = true
    try {
      const result = await loader(page.value * size.value, size.value)
      if (current !== generation || !allowed.value) return
      const last =
        result.total === null ? null : Math.max(0, Math.ceil(result.total / size.value) - 1)
      if (last !== null && page.value > last) page.value = last
      else if (!result.items.length && page.value > 0) page.value = fallbackPage
      else {
        rows.value = result.items
        total.value = result.total
      }
    } catch (reason) {
      if (current === generation)
        error.value = reason instanceof Error ? reason.message : '查询失败'
    } finally {
      if (current === generation) loading.value = false
    }
  }
  function reload() {
    return load()
  }
  function refresh() {
    if (!loading.value) return load(true)
  }
  function search() {
    if (page.value) page.value = 0
    else void reload()
  }
  function setSize(value: number) {
    const previous = size.value
    storeSize(value)
    if (previous !== size.value) search()
  }
  watch(
    [allowed, page],
    ([, target], [, previous = 0]) => {
      // A forward jump can land past the end. Retry the page the user came from.
      void load(false, target > previous ? previous : 0)
    },
    { immediate: true, flush: 'sync' },
  )
  onScopeDispose(() => {
    disposed = true
    generation++
  })
  return { rows, page, size, total, loading, error, reload, refresh, search, setSize }
}
