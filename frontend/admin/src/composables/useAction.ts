import { onScopeDispose, ref } from 'vue'
export function useAction() {
  const busy = ref(false), error = ref(''), notice = ref('')
  let alive = true
  onScopeDispose(() => { alive = false })
  async function run(work: () => Promise<void>) {
    if (busy.value || !alive) return
    busy.value = true; error.value = ''; notice.value = ''
    try { await work() }
    catch (reason) { if (alive) error.value = reason instanceof Error ? reason.message : '操作失败' }
    finally { if (alive) busy.value = false }
  }
  return { busy, error, notice, run }
}
