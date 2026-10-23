// Request generation and disposal rules, using the console's 50-row pagination.
import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue'
import { can } from '../stores/session'
export function usePagedList<T>(permission: string, loader: (offset: number) => Promise<T[]>) {
  const rows = shallowRef<T[]>([]),
    page = ref(0),
    loading = ref(false),
    error = ref('')
  const allowed = computed(() => can(permission))
  let generation = 0
  let disposed = false
  async function load(preserve = false) {
    if (disposed) return
    const current = ++generation
    if (!preserve || !allowed.value) rows.value = []
    error.value = ''
    loading.value = false
    if (!allowed.value) return
    loading.value = true
    try {
      const result = await loader(page.value * 50)
      if (current !== generation || !allowed.value) return
      if (!result.length && page.value > 0) page.value--
      else rows.value = result
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
  watch([allowed, page], reload, { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    disposed = true
    generation++
  })
  return { rows, page, loading, error, reload, refresh, search }
}
