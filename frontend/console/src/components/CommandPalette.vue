<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter, type RouteLocationRaw } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { canNavigate, navigation } from '../router'
import { can } from '../stores/session'

interface PaletteCommand {
  id: string
  group: string
  label: string
  hint: string
  keywords: string
  to?: RouteLocationRaw
  action?: 'toggle-sidebar'
}

const props = defineProps<{
  modelValue: boolean
  collapsed: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  'toggle-sidebar': []
}>()

const router = useRouter()
const query = ref('')
const activeIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const dialogRef = ref<HTMLElement | null>(null)
let origin: HTMLElement | null = null
let restoreFocus = true

const pageMeta: Record<string, { hint: string; keywords: string }> = {
  '/': {
    hint: '查看平台异常与近 30 天用量汇总',
    keywords: 'overview dashboard home 总览 首页 概览',
  },
  '/environments': { hint: '访问个人交互环境与浏览器会话', keywords: 'env browser 环境 浏览器' },
  '/platform': {
    hint: '检查 API、数据库、存储与 Worker 状态',
    keywords: 'status health worker 平台 状态 健康',
  },
  '/users': { hint: '维护账号、重置密码与分配角色', keywords: 'user account 用户 账号' },
  '/roles': { hint: '管理角色与功能权限范围', keywords: 'role permission rbac 角色 权限' },
  '/audit': { hint: '追溯登录、授权与配置变更记录', keywords: 'audit log 操作 审计 日志' },
  '/dictionary': { hint: '维护业务字典分组与条目', keywords: 'dict dictionary 字典' },
  '/models': {
    hint: '管理渠道凭据、模型条目与价格',
    keywords: 'model channel provider price key 模型 渠道 接入 价格',
  },
  '/model-usage': {
    hint: '统计调用次数、趋势与预估/实扣费用',
    keywords: 'usage cost billing fee token 用量 费用 账单',
  },
  '/flows': { hint: '查看在途与历史 Flow 运行记录', keywords: 'flow run workflow 运行 流程' },
  '/flow-model-defaults': {
    hint: '按 Flow 定义与角色设定默认模型',
    keywords: 'default flow role 模型 用途 默认',
  },
  '/messaging': {
    hint: '管理外部消息渠道与推送配置',
    keywords: 'message messaging channel webhook 消息 渠道',
  },
  '/messaging/records': {
    hint: '查询消息投递状态与历史记录',
    keywords: 'delivery record 消息 记录 投递',
  },
}

const commands = computed<PaletteCommand[]>(() => {
  const items: PaletteCommand[] = []

  if (can('model_invocation:read')) {
    items.push(
      {
        id: 'shortcut-usage-pending',
        group: '高频直达',
        label: '查看等待核对的调用',
        hint: '用量与费用 · 实扣口径 · 等待核对',
        keywords: 'pending reconcile cost usage 等待 核对 实扣 费用 调用',
        to: {
          path: '/model-usage',
          query: { cost_basis: 'actual', cost_status: 'pending', tab: 'details' },
        },
      },
      {
        id: 'shortcut-usage-unknown',
        group: '高频直达',
        label: '查看费用未知的调用',
        hint: '用量与费用 · 实扣口径 · 费用未知',
        keywords: 'unknown cost fee usage 费用 未知 账单 调用',
        to: {
          path: '/model-usage',
          query: { cost_basis: 'actual', cost_known: 'false', tab: 'details' },
        },
      },
    )
  }

  items.push(
    {
      id: 'shortcut-workers',
      group: '高频直达',
      label: '查看 Worker 实例状态',
      hint: '平台状态 · Worker 实例列表',
      keywords: 'worker instance status 状态 实例',
      to: '/platform#worker-instances',
    },
    {
      id: 'shortcut-maintenance',
      group: '高频直达',
      label: '查看共享维护状态',
      hint: '平台状态 · 定时清理与自动查费',
      keywords: 'maintenance schedule cron 维护 清理 巡检',
      to: '/platform#shared-maintenance',
    },
  )

  for (const item of navigation) {
    if (!canNavigate(item)) continue
    const meta = pageMeta[item.path]
    items.push({
      id: `nav:${item.path}`,
      group: '页面跳转',
      label: item.label,
      hint: meta?.hint ?? item.path,
      keywords: `${item.path} ${meta?.keywords ?? ''}`,
      to: item.path,
    })
  }

  items.push(
    {
      id: 'account-profile',
      group: '账户与界面',
      label: '个人中心 · 个人资料',
      hint: '查看账户信息与当前权限',
      keywords: 'profile account 个人 资料 账户',
      to: { path: '/account', query: { tab: 'profile' } },
    },
    {
      id: 'account-password',
      group: '账户与界面',
      label: '修改密码',
      hint: '个人中心 · 更新登录密码',
      keywords: 'password security 修改 密码',
      to: { path: '/account', query: { tab: 'password' } },
    },
    {
      id: 'account-tokens',
      group: '账户与界面',
      label: '个人令牌 (PAT)',
      hint: '个人中心 · 管理 API 访问令牌',
      keywords: 'pat token api 个人 令牌',
      to: { path: '/account', query: { tab: 'tokens' } },
    },
    {
      id: 'ui-toggle-sidebar',
      group: '账户与界面',
      label: props.collapsed ? '展开左侧导航栏' : '折叠左侧导航栏',
      hint: '切换侧栏宽度',
      keywords: 'sidebar collapse expand 折叠 展开 侧栏 导航',
      action: 'toggle-sidebar',
    },
  )

  return items
})

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return commands.value
  const tokens = q.split(/\s+/).filter(Boolean)
  return commands.value.filter((item) => {
    const haystack = `${item.label} ${item.hint} ${item.keywords}`.toLowerCase()
    return tokens.every((token) => haystack.includes(token))
  })
})

const grouped = computed(() => {
  const sections: { title: string; items: { item: PaletteCommand; index: number }[] }[] = []
  filtered.value.forEach((item, index) => {
    let section = sections.find((entry) => entry.title === item.group)
    if (!section) {
      section = { title: item.group, items: [] }
      sections.push(section)
    }
    section.items.push({ item, index })
  })
  return sections
})

function close(shouldRestoreFocus = true) {
  restoreFocus = shouldRestoreFocus
  emit('update:modelValue', false)
}

async function execute(command: PaletteCommand | undefined) {
  if (!command) return
  if (command.action === 'toggle-sidebar') {
    emit('toggle-sidebar')
    close(true)
    return
  }
  if (command.to) {
    close(false)
    await router.push(command.to)
  }
}

function move(delta: number) {
  const count = filtered.value.length
  if (!count) return
  activeIndex.value = (activeIndex.value + delta + count) % count
  void nextTick(() => {
    const active = listRef.value?.querySelector<HTMLElement>('[aria-selected="true"]')
    active?.scrollIntoView?.({ block: 'nearest' })
  })
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close(true)
    return
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    void execute(filtered.value[activeIndex.value])
    return
  }
  if (event.key === 'Tab') {
    const controls = dialogRef.value?.querySelectorAll<HTMLElement>('input,button:not([disabled])')
    if (!controls?.length) return
    const first = controls[0]!
    const last = controls[controls.length - 1]!
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
}

watch(filtered, (items) => {
  if (activeIndex.value >= items.length) activeIndex.value = 0
})

watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      origin = document.activeElement instanceof HTMLElement ? document.activeElement : null
      restoreFocus = true
      query.value = ''
      activeIndex.value = 0
      await nextTick()
      inputRef.value?.focus()
    } else if (restoreFocus) {
      await nextTick()
      origin?.focus()
    }
  },
)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="modelValue"
      class="cmdk-backdrop"
      data-testid="command-palette-backdrop"
      @mousedown.self="close(true)"
    >
      <div
        ref="dialogRef"
        class="cmdk-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="快捷命令栏"
        data-testid="command-palette"
        @keydown="onKeydown"
      >
        <div class="cmdk-search">
          <el-icon class="cmdk-search-icon" aria-hidden="true"><Search /></el-icon>
          <input
            ref="inputRef"
            v-model="query"
            type="text"
            class="cmdk-input"
            autocomplete="off"
            aria-label="搜索页面或快捷操作"
            placeholder="跳转页面、筛选用量或执行界面操作…"
            data-testid="command-palette-input"
          />
          <button type="button" class="cmdk-esc" aria-label="关闭命令栏" @click="close(true)">
            ESC
          </button>
        </div>

        <div
          ref="listRef"
          class="cmdk-list"
          role="listbox"
          aria-label="可用命令"
          data-testid="command-palette-list"
        >
          <template v-if="grouped.length">
            <div v-for="section in grouped" :key="section.title" class="cmdk-group">
              <div class="cmdk-group-title">{{ section.title }}</div>
              <button
                v-for="entry in section.items"
                :key="entry.item.id"
                type="button"
                role="option"
                class="cmdk-option"
                :class="{ 'is-active': entry.index === activeIndex }"
                :aria-selected="entry.index === activeIndex"
                :data-command-id="entry.item.id"
                @mouseenter="activeIndex = entry.index"
                @click="execute(entry.item)"
              >
                <span class="cmdk-option-main">
                  <span class="cmdk-option-label">{{ entry.item.label }}</span>
                  <span class="cmdk-option-hint">{{ entry.item.hint }}</span>
                </span>
                <span v-if="entry.index === activeIndex" class="cmdk-enter" aria-hidden="true"
                  >↵</span
                >
              </button>
            </div>
          </template>
          <p v-else class="cmdk-empty" role="status">没有匹配的页面或快捷操作</p>
        </div>

        <div class="cmdk-footer">
          <span>仅显示当前账户有权访问的入口</span>
          <span class="cmdk-keys"> <kbd>↑↓</kbd> 选择 <kbd>↵</kbd> 执行 </span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cmdk-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2100;
  background: rgba(35, 43, 54, 0.32);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: min(12vh, 96px) 16px 24px;
}
.cmdk-dialog {
  width: min(520px, 100%);
  background: #ffffff;
  border: 1px solid #dce2e9;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(29, 43, 60, 0.16);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.cmdk-search {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 48px;
  padding: 0 14px;
  border-bottom: 1px solid #edf0f4;
}
.cmdk-search-icon {
  color: #8b6f47;
  font-size: 16px;
  flex-shrink: 0;
}
.cmdk-input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: #1d2b3c;
  font-size: 14px;
  outline: none;
}
.cmdk-input::placeholder {
  color: #6b7788;
}
.cmdk-esc {
  border: 1px solid #dce2e9;
  background: #f7f8fa;
  color: #617084;
  border-radius: 5px;
  padding: 2px 6px;
  font-size: 11px;
  font-family: 'Cascadia Code', Consolas, monospace;
  cursor: pointer;
}
.cmdk-esc:hover {
  color: #1d2b3c;
  border-color: #c4a989;
}
.cmdk-list {
  max-height: min(360px, 52vh);
  overflow-y: auto;
  padding: 6px 0;
}
.cmdk-group + .cmdk-group {
  margin-top: 4px;
}
.cmdk-group-title {
  padding: 6px 14px 4px;
  font-size: 12px;
  font-weight: 500;
  color: #617084;
}
.cmdk-option {
  width: 100%;
  border: 0;
  background: transparent;
  padding: 8px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  text-align: left;
  cursor: pointer;
  color: #263445;
}
.cmdk-option:hover {
  background: #f7f8fa;
}
.cmdk-option.is-active {
  background: #f3ede3;
}
.cmdk-option-main {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.cmdk-option-label {
  font-size: 13px;
  font-weight: 500;
  color: #1d2b3c;
}
.cmdk-option.is-active .cmdk-option-label {
  color: #74603c;
}
.cmdk-option-hint {
  font-size: 12px;
  color: #617084;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cmdk-enter {
  font-size: 12px;
  font-family: 'Cascadia Code', Consolas, monospace;
  color: #8b6f47;
  flex-shrink: 0;
}
.cmdk-empty {
  margin: 0;
  padding: 28px 16px;
  text-align: center;
  font-size: 13px;
  color: #617084;
}
.cmdk-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 14px;
  background: #f7f8fa;
  border-top: 1px solid #edf0f4;
  font-size: 12px;
  color: #617084;
}
.cmdk-keys {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.cmdk-keys kbd {
  padding: 1px 5px;
  border-radius: 4px;
  border: 1px solid #dce2e9;
  background: #ffffff;
  color: #526173;
  font-size: 11px;
  font-family: 'Cascadia Code', Consolas, monospace;
}
</style>
