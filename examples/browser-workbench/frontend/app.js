const element = (id) => document.getElementById(id)
const backupDate = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
const stateText = {
  starting: '正在启动',
  ready: '可连接',
  stopping: '正在停止',
  review: '待核对',
  stopped: '已停止',
  failed: '启动失败',
}
let identity
let workspaces = []
let selectedId
let busy = false
let viewer
let connecting = false
let screenEpoch = 0
let clipboardSent = false

async function request(path, method = 'GET', body) {
  const response = await fetch('/api/v1' + path, {
    method,
    cache: 'no-store',
    credentials: 'same-origin',
    signal: AbortSignal.timeout(/^\/browser-workspaces\/.+\/(open|stop|backup)$/.test(path) ? 220000 : 30000),
    headers: {
      'X-Keystone-Request': '1',
      ...(identity?.csrf_token ? { 'X-CSRF-Token': identity.csrf_token } : {}),
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
  if (response.status === 204) return
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw Object.assign(new Error(data.detail || '暂时无法访问 Keystone。'), {
      status: response.status,
      code: data.code,
    })
  }
  return data
}

const selected = () => workspaces.find(({ id }) => id === selectedId)
const workspacePath = (workspace) => '/browser-workspaces/' + encodeURIComponent(workspace.id)
const runPath = (workspace) =>
  workspacePath(workspace) + '/runs/' + encodeURIComponent(workspace.run.id)

function showNotice(message = '', tone = 'error') {
  element('notice').textContent = message
  element('notice').hidden = !message
  element('notice').dataset.tone = tone
}

function drawWorkspaces() {
  const list = element('workspaces')
  list.replaceChildren()
  for (const workspace of workspaces) {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'workspace-item'
    button.textContent = workspace.name
    button.setAttribute('aria-pressed', String(workspace.id === selectedId))
    button.disabled = busy
    button.onclick = () => {
      if (selectedId === workspace.id) return
      disconnect()
      selectedId = workspace.id
      render()
    }
    list.append(button)
  }
}

function render() {
  const workspace = selected()
  const run = workspace?.run
  const state = run?.state
  const passwordRequired = identity?.user.password_change_required
  element('login').hidden = Boolean(identity)
  element('change-password').hidden = !passwordRequired
  element('workbench').hidden = !identity || passwordRequired
  element('account').hidden = !identity
  element('account-name').textContent = identity?.user.display_name ?? ''
  element('owner-id').textContent = identity?.user.id ?? ''
  element('empty-workspaces').hidden = workspaces.length > 0
  element('workspace-name').textContent = workspace?.name ?? '浏览器'
  element('run-status').textContent = workspace
    ? (clipboardSent ? '文本已发送 · 在远程浏览器按 Ctrl+V 粘贴' : stateText[state] || '未启动')
    : '登记浏览器空间后即可打开浏览器'
  element('run-status').dataset.state = state ?? ''
  element('open').hidden = !workspace || Boolean(viewer) || connecting ||
    ['starting', 'stopping', 'review'].includes(state)
  element('open').textContent = state === 'ready' ? '连接画面' : '打开浏览器'
  element('check').hidden = !['starting', 'stopping', 'review'].includes(state)
  element('stop').hidden = !run?.can_stop || !identity?.permissions.includes('browser:stop')
  element('backup').hidden = !workspace?.backup_enabled ||
    !identity?.permissions.includes('browser:stop') ||
    (state != null && !['stopped', 'failed'].includes(state))
  const backupStatus = element('backup-status')
  backupStatus.hidden = !workspace?.backup_enabled
  backupStatus.textContent = workspace?.backup_at
    ? `最近备份：${backupDate.format(new Date(workspace.backup_at))}${workspace.backup_failed ? ' · 最近尝试失败' : ''}`
    : (workspace?.backup_failed ? '备份未成功' : '还没有备份')
  element('paste').hidden = !viewer || connecting
  element('disconnect').hidden = !viewer && !connecting
  for (const button of document.querySelectorAll('button')) button.disabled = busy
  drawWorkspaces()
}

function disconnect(message = '尚未连接画面') {
  screenEpoch += 1
  const active = viewer
  viewer = undefined
  connecting = false
  clipboardSent = false
  if (element('paste-dialog').open) element('paste-dialog').close()
  active?.disconnect()
  element('viewer').replaceChildren()
  element('viewer').hidden = true
  element('placeholder').hidden = false
  element('screen-status').textContent = message
}

function setPasteError(message = '') {
  element('paste-error').textContent = message
  element('paste-error').hidden = !message
}

element('paste').onclick = () => {
  if (!viewer || connecting) return
  clipboardSent = false
  render()
  element('paste-dialog').showModal()
}
element('paste-cancel').onclick = () => element('paste-dialog').close()
element('paste-dialog').addEventListener('close', () => {
  element('paste-form').reset()
  setPasteError()
})
element('paste-form').onsubmit = (event) => {
  event.preventDefault()
  if (!viewer || connecting) return
  const text = element('paste-text').value
  // noVNC has no public flag for negotiated extended clipboard; legacy RFB replaces non-Latin-1 characters.
  const unicode = viewer._clipboardServerCapabilitiesFormats?.[1] &&
    viewer._clipboardServerCapabilitiesActions?.[1 << 27]
  if (!unicode && /[^\u0000-\u00ff]/u.test(text)) {
    setPasteError('当前浏览器不支持中文等字符。请先停止并重新打开浏览器，再粘贴文本。')
    return
  }
  viewer.clipboardPasteFrom(text)
  element('paste-dialog').close()
  clipboardSent = true
  render()
  viewer.focus({ preventScroll: true })
}

async function connect(workspace, retry = true) {
  disconnect('正在连接浏览器画面…')
  const epoch = screenEpoch
  connecting = true
  render()
  try {
    const { default: RFB } = await import('./novnc/core/rfb.js')
    if (epoch !== screenEpoch) return
    const endpoint = new URL('/api/v1' + runPath(workspace) + '/screen', location.origin)
    endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:'
    const screen = element('viewer')
    screen.hidden = false
    const connection = new RFB(screen, endpoint.href, { shared: false })
    viewer = connection
    connection.scaleViewport = true
    connection.resizeSession = false
    connection.addEventListener('connect', () => {
      if (epoch !== screenEpoch) return
      connecting = false
      element('placeholder').hidden = true
      render()
    })
    connection.addEventListener('disconnect', () => {
      if (epoch !== screenEpoch) return
      if (retry && connecting) {
        // A reload can reconnect while the previous viewer is still releasing input control.
        disconnect('正在重新连接浏览器画面…')
        const retryEpoch = screenEpoch
        connecting = true
        render()
        setTimeout(() => {
          if (retryEpoch === screenEpoch) void connect(workspace, false).catch(() => render())
        }, 1250)
        return
      }
      disconnect('画面已断开，可以重新连接。')
      render()
    })
  } catch (error) {
    if (epoch !== screenEpoch) return
    disconnect('无法连接画面，请重试。')
    throw error
  }
}

async function refresh() {
  const previous = selected()?.run
  const next = await request('/browser-workspaces')
  workspaces = next
  if (!selected()) selectedId = workspaces[0]?.id
  const current = selected()?.run
  if ((viewer || connecting) && (previous?.id !== current?.id || current?.state !== 'ready')) {
    disconnect()
  }
}

async function loadIdentity() {
  identity = await request('/auth/me')
  if (!identity.user.password_change_required) await refresh()
}

async function run(action) {
  if (busy) return
  busy = true
  showNotice()
  render()
  try {
    await action()
  } catch (error) {
    if (error.status === 401 && error.code !== 'invalid_login') {
      identity = undefined
      workspaces = []
      selectedId = undefined
      disconnect()
    }
    if (error.status !== 401 || error.code === 'invalid_login') {
      showNotice(error.message || '操作未完成，请检查当前状态后重试。')
    }
  } finally {
    busy = false
    element('loading').hidden = true
    render()
  }
}

element('login').onsubmit = (event) => {
  event.preventDefault()
  const form = event.currentTarget
  const values = Object.fromEntries(new FormData(form))
  void run(async () => {
    try {
      await request('/auth/login', 'POST', values)
      await loadIdentity()
    } finally {
      form.reset()
    }
  })
}
element('change-password').onsubmit = (event) => {
  event.preventDefault()
  const form = event.currentTarget
  const values = Object.fromEntries(new FormData(form))
  void run(async () => {
    try {
      await request('/auth/password', 'POST', values)
      identity = undefined
    } finally {
      form.reset()
    }
  })
}
element('logout').onclick = () => void run(async () => {
  await request('/auth/logout', 'POST')
  identity = undefined
  selectedId = undefined
  workspaces = []
  disconnect()
})
element('open').onclick = () => void run(async () => {
  const workspace = selected()
  try {
    workspace.run = await request(workspacePath(workspace) + '/open', 'POST')
  } catch (error) {
    await refresh()
    throw error
  }
  if (workspace.run.restored_from) {
    showNotice(`已从 ${backupDate.format(new Date(workspace.run.restored_from))} 的备份恢复浏览器空间。`, 'success')
  }
  if (workspace.run.state === 'ready') await connect(workspace)
})
element('backup').onclick = () => void run(async () => {
  try {
    await request(workspacePath(selected()) + '/backup', 'POST')
  } finally {
    await refresh()
  }
})
element('disconnect').onclick = () => {
  disconnect()
  render()
}
element('stop').onclick = () => {
  if (!window.confirm('停止会关闭浏览器窗口并保留浏览器空间。确定停止？')) return
  void run(async () => {
    const workspace = selected()
    workspace.run = await request(runPath(workspace) + '/stop', 'POST')
    if (workspace.run.state !== 'ready') disconnect()
    await refresh()
  })
}
element('check').onclick = () => void run(async () => {
  const workspace = selected()
  workspace.run = await request(runPath(workspace) + '/check', 'POST')
  if (workspace.run.state === 'review') {
    showNotice('暂时无法确认原运行，请稍后重新检查或联系部署管理员。')
  }
})
element('refresh').onclick = () => void run(refresh)
window.addEventListener('pagehide', () => disconnect())
window.addEventListener('pageshow', (event) => {
  if (event.persisted) void run(loadIdentity)
})
void run(loadIdentity)
