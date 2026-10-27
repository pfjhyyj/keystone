# 接入一个最小业务

以下从已经能登录的开发环境接入本人笔记，不使用模型或 Flow。完整边界与参数见[模块扩展](extending.md)，日常启动见[Quickstart](../README.md#quickstart)。

## 1. 生成与装配

在仓库根目录执行：

```bash
backend/.venv/bin/python scripts/new_business.py notes --entity note --label 笔记
```

Windows 使用 `backend/.venv/Scripts/python.exe`。脚手架创建业务目录和下一条迁移，采用服务端固定 `owner_id` 的本人范围、部分更新及审计摘要。先评审业务字段和对象策略，再按生成命令的提示完成三处显式装配：

```python
# backend/src/keystone/composition.py
from keystone.businesses.notes.catalog import CATALOG as NOTES
BUSINESS_CATALOGS = (("notes", NOTES),)

# backend/src/keystone/model_registry.py 的 load_models() 内
from keystone.businesses.notes import models as notes_models  # noqa: F401

# backend/src/keystone/bootstrap.py 的 create_app() 内
from keystone.businesses.notes.controllers import router as notes_router
app.include_router(notes_router)
```

已有声明时追加，不覆盖其他业务。启动会拒绝权限或路由冲突。停止旧开发进程后，在 `backend/` 执行：

```bash
uv run --locked keystone init
uv run --locked keystone check
uv run --locked keystone-api
```

初始管理员自动获得新功能权限；普通角色需要授予 `notes.note:read`、`notes.note:write`，已有 PAT 需要重新签发所需范围。管理员也不自动获得他人的笔记。

## 2. 加入控制台

创建 `frontend/console/src/views/notes/NotesView.vue`：

```vue
<script setup lang="ts">
import { ref } from 'vue'
import { request } from '../../api/client'
import { useAction } from '../../composables/useAction'
import { usePagedList } from '../../composables/usePagedList'
import { can } from '../../stores/session'

type Note = { id: string; title: string }
const title = ref('')
const notes = usePagedList<Note>('notes.note:read', (offset) =>
  request<Note[]>(`/notes/notes?offset=${offset}`),
)
const { rows, loading, error, reload } = notes
const { busy, error: saveError, run } = useAction()
async function save() {
  await run(async () => {
    await request('/notes/notes', 'POST', { title: title.value })
    title.value = ''
    await reload()
  })
}
</script>

<template>
  <section aria-label="我的笔记">
    <h1>我的笔记</h1>
    <p v-if="error || saveError" role="alert">{{ error || saveError }}</p>
    <form v-if="can('notes.note:write')" @submit.prevent="save">
      <label>标题 <input v-model="title" required maxlength="200" /></label>
      <button :disabled="busy" type="submit">保存</button>
    </form>
    <p v-if="loading">正在加载</p>
    <ul><li v-for="row in rows" :key="row.id">{{ row.title }}</li></ul>
  </section>
</template>
```

在 `src/router/index.ts` 的 routes 中增加路由，在 navigation 中增加菜单：

```ts
// routes
{
  path: '/notes',
  component: () => import('../views/notes/NotesView.vue'),
  meta: { title: '我的笔记', permissions: ['notes.note:read'] },
},
// navigation
{ path: '/notes', label: '我的笔记', permissions: ['notes.note:read'] },
```

未列入平台固定分组的菜单会进入“业务”。详情页可以只注册路由，并用 meta 声明标题和页面权限；登录跳转使用已注册路由，不维护第三份路径白名单。后端仍负责每次授权，页面隐藏不能代替它。

## 3. 验证业务闭环

登录控制台访问 `/console/notes`，创建笔记并刷新确认保存；以另一个有同样功能权限的用户登录，确认看不到或修改不了原笔记。API 为 `/api/v1/notes/notes`，PATCH 省略字段保持原值，显式 null 返回 422。

为自己的模块编写集成用例，至少核对本人范围、功能权限、PAT 交集、部分更新、审计摘要和回滚。可按 `backend/tests/integration/test_scaffold_business.py` 的完整生成与装配方式编写；测试只使用[专用随机库](../backend/tests/README.md)，不指向刚才的开发库。

```bash
backend/.venv/bin/python scripts/check.py --scope backend
backend/.venv/bin/python scripts/check.py --scope frontend
```

需要后台阶段时再按[Flow 开发](flow-development.md)增加 Flow；人工决定和业务结论继续由业务 Service 保存。
