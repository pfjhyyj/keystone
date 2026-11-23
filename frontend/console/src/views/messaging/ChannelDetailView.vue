<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { messagingApi, type MessageChannel } from '../../api/messaging'
import { can } from '../../stores/session'
import StatusTag from '../../components/StatusTag.vue'
import ChannelDialog from './components/ChannelDialog.vue'
import ConnectionStatus from './components/ConnectionStatus.vue'
import FeishuAppGuide from './components/FeishuAppGuide.vue'
import GroupsPanel from './components/GroupsPanel.vue'
import MembersPanel from './components/MembersPanel.vue'
import { time } from './labels'

const props = defineProps<{ id: string }>()
const route = inject(routeLocationKey, undefined),
  router = inject(routerKey, undefined)
const channel = ref<MessageChannel | null>(null),
  error = ref(''),
  notice = ref(''),
  editing = ref(false)
const TABS = ['connection', 'groups', 'members']
const localTab = ref('')
const tab = computed({
  get: () => {
    const wanted = String(route?.query.tab ?? localTab.value)
    return TABS.includes(wanted) ? wanted : 'connection'
  },
  set: (value: string) => {
    localTab.value = value
    if (router) void router.replace({ query: { ...route?.query, tab: value } })
  },
})
let generation = 0
async function load() {
  const current = ++generation
  error.value = ''
  try {
    const found = await messagingApi.channel(props.id)
    if (current === generation) channel.value = found
  } catch (reason) {
    if (current === generation) error.value = reason instanceof Error ? reason.message : '读取失败'
  }
}
watch(() => props.id, load, { immediate: true })
function saved() {
  editing.value = false
  notice.value = '渠道已保存'
  void load()
}
</script>
<template>
  <section aria-label="渠道详情">
    <RouterLink to="/messaging" class="back-link">← 消息渠道</RouterLink>
    <el-alert v-if="error" role="alert" type="error" show-icon :closable="false">{{
      error
    }}</el-alert>
    <template v-if="channel">
      <div class="heading">
        <h1>{{ channel.name }}</h1>
        <div class="row-actions">
          <StatusTag :status="channel.is_active ? 'active' : 'disabled'">{{
            channel.is_active ? '启用' : '停用'
          }}</StatusTag>
          <el-button native-type="button" @click="editing = true">编辑渠道</el-button>
        </div>
      </div>
      <el-alert v-if="notice" role="status" type="success" show-icon :closable="false">{{
        notice
      }}</el-alert>
      <el-alert v-if="channel.provider !== 'feishu_app'" type="info" show-icon :closable="false"
        >群机器人只有一个群，在渠道列表里编辑和测试即可。</el-alert
      >
      <el-tabs v-else v-model="tab">
        <el-tab-pane label="连接与凭据" name="connection">
          <el-descriptions :column="1" border
            ><el-descriptions-item label="App ID"
              ><code>{{ channel.app_id }}</code></el-descriptions-item
            ><el-descriptions-item label="App Secret">{{
              channel.has_credential ? '已设置（加密保存，不再显示）' : '未设置'
            }}</el-descriptions-item
            ><el-descriptions-item label="接收点击">
              <ConnectionStatus :channel="channel" detail />
              <p class="hint">
                Worker 通过长连接接收卡片按钮点击和绑定码，每 30 秒同步一次渠道配置。
              </p></el-descriptions-item
            ><el-descriptions-item label="群与成员"
              >{{ channel.groups ?? 0 }} 个群 ·
              {{ channel.members ?? 0 }} 位绑定成员</el-descriptions-item
            ><el-descriptions-item label="更新时间">{{
              time(channel.updated_at)
            }}</el-descriptions-item></el-descriptions
          >
          <el-button class="refresh" native-type="button" @click="load">刷新状态</el-button>
          <FeishuAppGuide />
        </el-tab-pane>
        <el-tab-pane label="群" name="groups">
          <GroupsPanel v-if="tab === 'groups'" :channel="channel" @changed="load" />
        </el-tab-pane>
        <el-tab-pane label="成员绑定" name="members">
          <MembersPanel v-if="tab === 'members'" :channel="channel" @changed="load" />
        </el-tab-pane>
      </el-tabs>
      <p v-if="can('message_delivery:read')">
        <RouterLink :to="{ path: '/messaging/records', query: { channel: channel.id } }"
          >查看这个渠道的消息记录 →</RouterLink
        >
      </p>
    </template>
    <ChannelDialog
      v-if="editing && channel"
      :channel="channel"
      @close="editing = false"
      @saved="saved"
    />
  </section>
</template>
<style scoped>
.back-link {
  display: inline-block;
  margin-bottom: 8px;
}
.hint {
  color: var(--el-text-color-secondary);
  margin: 4px 0 0;
}
.refresh {
  margin: 12px 0 16px;
}
</style>
