<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: unknown }>()

const fieldLabels: Record<string, string> = {
  prompt: '提示词',
  text: '文本内容',
  raw_text: '模型原始回复',
  messages: '消息列表',
  parts: '模型回复',
  n: '生成张数',
  resolution: '分辨率',
  aspect_ratio: '比例',
  quality: '质量',
  output_format: '格式',
  max_tokens: '输出上限 Token',
  output_schema: '结构化契约',
  reasoning: '思考设置',
  images: '图片数',
  vectors: '向量条数',
  dimensions: '向量维度',
  count: '数量',
  state: '状态',
  scope: '处理范围',
  preserve: '保留部位',
  notes: '补充要求',
  summary: '摘要',
  template_version: '模板版本',
}

const roleLabels: Record<string, string> = {
  system: '系统指令',
  user: '用户输入',
  assistant: '模型回复',
  request: '请求消息',
  response: '模型回复',
  'system-prompt': '系统指令',
  'user-prompt': '用户输入',
  text: '文本输出',
  thinking: '思考过程',
  'tool-call': '工具调用',
  'tool-return': '工具返回',
  'retry-prompt': '修正提示',
}

interface MessageItem {
  role: string
  text?: string
  parts?: { kind: string; content?: unknown; tool?: string; args?: unknown }[]
}

const isRecord = computed(
  () => props.value != null && typeof props.value === 'object' && !Array.isArray(props.value),
)

const record = computed(() => (isRecord.value ? (props.value as Record<string, unknown>) : null))

const messages = computed<MessageItem[]>(() => {
  if (!record.value || !Array.isArray(record.value.messages)) return []
  return record.value.messages
    .filter((item): item is Record<string, unknown> => item != null && typeof item === 'object')
    .map((item) => ({
      role: String(item.role ?? 'message'),
      text: typeof item.content === 'string' ? item.content : undefined,
      parts: Array.isArray(item.parts)
        ? (item.parts as { kind: string; content?: unknown; tool?: string; args?: unknown }[])
        : undefined,
    }))
})

const responseParts = computed(() => {
  if (!record.value || !Array.isArray(record.value.parts)) return []
  return record.value.parts as {
    kind: string
    content?: unknown
    tool?: string
    args?: unknown
  }[]
})

const textBlocks = computed(() => {
  if (!record.value) return []
  return Object.entries(record.value)
    .filter(
      ([key, val]) =>
        typeof val === 'string' &&
        (val.includes('\n') ||
          val.length > 60 ||
          ['prompt', 'text', 'raw_text', 'scope', 'preserve', 'notes', 'summary'].includes(key)),
    )
    .map(([key, val]) => ({ key, label: fieldLabels[key] ?? key, text: val as string }))
})

const textKeys = computed(() => new Set(textBlocks.value.map((item) => item.key)))

const scalarFields = computed(() => {
  if (!record.value) return []
  return Object.entries(record.value)
    .filter(
      ([key, val]) =>
        !textKeys.value.has(key) &&
        !/(?:^|_)ids?$/i.test(key) &&
        val != null &&
        ['string', 'number', 'boolean'].includes(typeof val),
    )
    .map(([key, val]) => ({
      key,
      label: fieldLabels[key] ?? key,
      value: typeof val === 'boolean' ? (val ? '是' : '否') : String(val),
    }))
})

const listFields = computed(() => {
  if (!record.value) return []
  return Object.entries(record.value)
    .filter(
      ([key, val]) =>
        key !== 'messages' &&
        key !== 'parts' &&
        Array.isArray(val) &&
        val.length > 0 &&
        val.every((item) => typeof item === 'string'),
    )
    .map(([key, val]) => ({
      key,
      label: fieldLabels[key] ?? key,
      items: val as string[],
    }))
})

const listKeys = computed(() => new Set(listFields.value.map((item) => item.key)))

const complexFields = computed(() => {
  if (!record.value) return []
  return Object.entries(record.value)
    .filter(([key, val]) => {
      if (key === 'messages' && messages.value.length) return false
      if (key === 'parts' && responseParts.value.length) return false
      if (textKeys.value.has(key) || listKeys.value.has(key)) return false
      if (val == null || ['string', 'number', 'boolean'].includes(typeof val)) return false
      return true
    })
    .map(([key, val]) => ({
      key,
      label: fieldLabels[key] ?? key,
      json: JSON.stringify(val, null, 2),
    }))
})

function formatPartContent(val: unknown) {
  if (typeof val === 'string') return val
  return JSON.stringify(val, null, 2)
}
</script>

<template>
  <div class="payload-view">
    <p v-if="typeof value === 'string'" class="text-block">{{ value }}</p>
    <template v-else-if="record">
      <div v-if="scalarFields.length" class="scalar-pills">
        <span v-for="item in scalarFields" :key="item.key" class="scalar-pill">
          <span class="pill-label">{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
        </span>
      </div>

      <div v-for="block in textBlocks" :key="block.key" class="readable-block">
        <div class="block-label">{{ block.label }}</div>
        <div class="text-block">{{ block.text }}</div>
      </div>

      <div v-if="messages.length" class="message-list">
        <div v-for="(msg, idx) in messages" :key="idx" class="message-item">
          <div class="block-label">{{ roleLabels[msg.role] ?? msg.role }}</div>
          <div v-if="msg.text != null" class="text-block">{{ msg.text }}</div>
          <div v-else-if="msg.parts?.length" class="part-list">
            <div v-for="(part, pIdx) in msg.parts" :key="pIdx" class="part-item">
              <span class="part-tag">{{ roleLabels[part.kind] ?? part.kind }}</span>
              <span v-if="part.tool" class="part-tool">{{ part.tool }}</span>
              <div v-if="part.content != null" class="text-block">
                {{ formatPartContent(part.content) }}
              </div>
              <pre v-if="part.args != null" class="json-block">{{
                formatPartContent(part.args)
              }}</pre>
            </div>
          </div>
        </div>
      </div>

      <div v-if="responseParts.length" class="part-list">
        <div v-for="(part, pIdx) in responseParts" :key="pIdx" class="part-item">
          <span class="part-tag">{{ roleLabels[part.kind] ?? part.kind }}</span>
          <span v-if="part.tool" class="part-tool">{{ part.tool }}</span>
          <div v-if="part.content != null" class="text-block">
            {{ formatPartContent(part.content) }}
          </div>
          <pre v-if="part.args != null" class="json-block">{{ formatPartContent(part.args) }}</pre>
        </div>
      </div>

      <div v-for="list in listFields" :key="list.key" class="readable-block">
        <div class="block-label">{{ list.label }}</div>
        <ul class="readable-list">
          <li v-for="(entry, idx) in list.items" :key="idx">{{ entry }}</li>
        </ul>
      </div>

      <div v-for="item in complexFields" :key="item.key" class="readable-block">
        <div class="block-label">{{ item.label }}</div>
        <pre class="json-block">{{ item.json }}</pre>
      </div>

      <pre
        v-if="
          !scalarFields.length &&
          !textBlocks.length &&
          !listFields.length &&
          !messages.length &&
          !responseParts.length &&
          !complexFields.length
        "
        class="json-block"
        >{{ JSON.stringify(value, null, 2) }}</pre>
    </template>
    <pre v-else class="json-block">{{ JSON.stringify(value, null, 2) }}</pre>
  </div>
</template>

<style scoped>
.payload-view {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.scalar-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.scalar-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
}
.pill-label {
  color: var(--el-text-color-secondary);
}
.readable-block,
.message-item,
.part-item {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.message-list,
.part-list {
  display: grid;
  gap: 8px;
}
.message-item {
  padding: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-bg-color);
}
.block-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
.part-tag {
  display: inline-block;
  width: fit-content;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--el-fill-color);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.part-tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  color: var(--el-color-primary);
}
.text-block {
  margin: 0;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
  max-height: 260px;
  overflow: auto;
  font-size: 13px;
}
.readable-list {
  margin: 0;
  padding: 6px 10px 6px 24px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  display: grid;
  gap: 4px;
  font-size: 13px;
  line-height: 1.5;
}
.json-block {
  margin: 0;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow: auto;
  font-size: 12px;
}
</style>
