import { computed, inject, onBeforeUnmount, onMounted, type Ref } from 'vue'
import { matchedRouteKey, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { confirmAction } from '../utils/confirm'
import { sessionVersion } from '../stores/session'

export function useUnsavedChanges(dirty: Ref<boolean>) {
  const version = sessionVersion.value
  const active = computed(() => dirty.value && version === sessionVersion.value)
  async function confirmLeave() {
    if (!active.value) return true
    return confirmAction('尚有未保存的修改，离开后将丢失。', {
      title: '放弃修改？',
      confirmText: '放弃修改',
      cancelText: '继续编辑',
    })
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (!active.value) return
    event.preventDefault()
    event.returnValue = ''
  }
  if (inject(matchedRouteKey, undefined)?.value) {
    onBeforeRouteLeave(confirmLeave)
    onBeforeRouteUpdate((to, from) => to.query.tab === from.query.tab || confirmLeave())
  }
  onMounted(() => window.addEventListener('beforeunload', beforeUnload))
  onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
  return { confirmLeave }
}
