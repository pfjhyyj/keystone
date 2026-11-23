<script setup lang="ts">
import type { MessageChannel } from '../../../api/messaging'
import StatusTag from '../../../components/StatusTag.vue'
import { connectionText } from '../labels'

// An app channel's long connection; a disabled channel is not connected on purpose.
defineProps<{ channel: MessageChannel; detail?: boolean }>()
</script>
<template>
  <span v-if="!channel.is_active">—</span>
  <span v-else-if="channel.connection" class="connection-status"
    ><StatusTag :status="connectionText[channel.connection.state][1]">{{
      connectionText[channel.connection.state][0]
    }}</StatusTag
    ><small v-if="detail && channel.connection.state === 'connected'"
      >{{ channel.connection.workers }} 个 Worker 在线</small
    ><small v-else-if="channel.connection.state === 'offline'">没有运行中的 Worker 连接</small
    ><small v-else-if="channel.connection.error_hint">{{ channel.connection.error_hint }}</small
    ><small v-else-if="channel.connection.error_code">{{
      channel.connection.error_code
    }}</small></span
  >
</template>
<style scoped>
.connection-status {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
}
.connection-status small {
  color: #64748b;
}
</style>
