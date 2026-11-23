<script setup lang="ts">
import { ElCollapse, ElCollapseItem } from 'element-plus'
// The one place operators read how a Feishu self-built app must be set up for Keystone.
const scopes = [
  {
    name: '以应用的身份发消息',
    code: 'im:message:send_as_bot',
    use: '发送消息和卡片，点击后更新卡片',
    need: '必需',
  },
  {
    name: '获取群组信息',
    code: 'im:chat:readonly',
    use: '“发送目标”里列出机器人所在的群',
    need: '发到群时需要',
  },
  {
    name: '读取用户发给机器人的单聊消息',
    code: 'im:message.p2p_msg:readonly',
    use: '用户私聊机器人发送绑定码，自助绑定飞书账号',
    need: '自助绑定需要',
  },
  {
    name: '通过手机号或邮箱获取用户 ID',
    code: 'contact:user.id:readonly',
    use: '管理员按手机号或邮箱查找并绑定账号',
    need: '可选',
  },
  {
    name: '获取用户基本信息',
    code: 'contact:user.base:readonly',
    use: '管理员绑定时显示对方姓名',
    need: '可选',
  },
]
const subscriptions = [
  {
    place: '事件配置',
    name: '接收消息 v2.0',
    code: 'im.message.receive_v1',
    use: '收到用户私聊发来的绑定码',
  },
  {
    place: '回调配置',
    name: '卡片回传交互',
    code: 'card.action.trigger',
    use: '用户点击卡片上的按钮',
  },
]
</script>
<template>
  <ElCollapse class="feishu-guide">
    <ElCollapseItem title="飞书自建应用配置说明" name="guide">
      <p>
        在飞书开发者后台按下面配置同一个企业自建应用。只发消息时完成第 1、2、5
        步即可；要让用户点击卡片按钮或自助绑定，还需完成第 3、4 步。
      </p>
      <ol>
        <li>在“添加应用能力”中启用<strong>机器人</strong>。</li>
        <li>
          在“权限管理”中开通下列权限：
          <el-table :data="scopes" size="small" :fit="false" table-layout="auto" class="data-table">
            <el-table-column label="权限" min-width="190"
              ><template #default="{ row }"
                >{{ row.name }}<small>{{ row.code }}</small></template
              ></el-table-column
            >
            <el-table-column label="用途" prop="use" min-width="220" />
            <el-table-column label="是否需要" prop="need" min-width="110" />
          </el-table>
        </li>
        <li>
          在“事件与回调”中，事件配置和回调配置的订阅方式都选<strong>使用长连接接收</strong>，并添加：
          <el-table
            :data="subscriptions"
            size="small"
            :fit="false"
            table-layout="auto"
            class="data-table"
          >
            <el-table-column label="位置" prop="place" min-width="100" />
            <el-table-column label="名称" min-width="190"
              ><template #default="{ row }"
                >{{ row.name }}<small>{{ row.code }}</small></template
              ></el-table-column
            >
            <el-table-column label="用途" prop="use" min-width="200" />
          </el-table>
          长连接由 Keystone 的 Worker 主动连向飞书，不需要公网地址或回调
          URL。飞书保存长连接方式时会检查是否有在线连接：先在这里新建并启用渠道，保持 Worker
          运行，约 30 秒内会连上，再回到开发者后台保存。
        </li>
        <li>
          一个应用只接入一套 Keystone
          环境。飞书把每个事件随机交给一个在线连接，多套环境共用一个应用时，事件会被其他环境收走。开发、测试和生产请使用不同的应用。
        </li>
        <li>
          在“版本管理与发布”中创建版本并发布；权限和订阅在发布后才生效。应用的可用范围须包含要接收消息和点击卡片的人。
        </li>
      </ol>
    </ElCollapseItem>
  </ElCollapse>
</template>
<style scoped>
.feishu-guide {
  margin-bottom: 16px;
}
.feishu-guide ol {
  display: grid;
  gap: 12px;
  padding-left: 20px;
}
.feishu-guide .data-table {
  margin: 8px 0;
}
</style>
