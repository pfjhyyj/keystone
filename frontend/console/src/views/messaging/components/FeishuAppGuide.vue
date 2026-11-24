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
    use: '用户私聊机器人发送绑定码或业务命令',
    need: '自助绑定或私聊命令需要',
  },
  {
    name: '获取群组中所有@机器人的消息',
    code: 'im:message.group_at_msg:readonly',
    use: '群成员 @机器人发送业务命令',
    need: '群内命令需要',
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
    use: '收到私聊的绑定码和命令、群里 @机器人的命令',
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
    <ElCollapseItem name="guide">
      <template #title>
        <span class="guide-title"
          ><strong>飞书自建应用配置说明</strong
          ><span>需要在飞书开发者后台开通的能力、权限和订阅</span></span
        >
      </template>
      <p class="guide-lead">
        只发消息时完成第 1、2、5 步；要接收卡片按钮点击和成员自助绑定，还需完成第 3、4 步。
      </p>
      <ol class="guide-steps">
        <li>
          <strong>启用机器人</strong>
          <p>在“添加应用能力”中添加机器人能力。</p>
        </li>
        <li>
          <strong>开通权限</strong>
          <p>在“权限管理”中开通：</p>
          <el-table :data="scopes" size="small" table-layout="auto" class="guide-table">
            <el-table-column label="权限" min-width="200"
              ><template #default="{ row }"
                >{{ row.name }}<small>{{ row.code }}</small></template
              ></el-table-column
            >
            <el-table-column label="用途" prop="use" min-width="220" />
            <el-table-column label="是否需要" prop="need" min-width="110" />
          </el-table>
        </li>
        <li>
          <strong>订阅事件与回调</strong>
          <p>在“事件与回调”中，事件配置和回调配置的订阅方式都选“使用长连接接收”，并添加：</p>
          <el-table :data="subscriptions" size="small" table-layout="auto" class="guide-table">
            <el-table-column label="位置" prop="place" min-width="100" />
            <el-table-column label="名称" min-width="200"
              ><template #default="{ row }"
                >{{ row.name }}<small>{{ row.code }}</small></template
              ></el-table-column
            >
            <el-table-column label="用途" prop="use" min-width="200" />
          </el-table>
          <p class="guide-note">
            长连接由 Keystone 的 Worker 主动连向飞书，不需要公网地址或回调
            URL。飞书保存长连接方式时会检查是否有在线连接：先在 Keystone 新建并启用渠道、保持 Worker
            运行，等“接收点击”显示已连接，再回到开发者后台保存。
          </p>
        </li>
        <li>
          <strong>一个应用只接一套环境</strong>
          <p>
            飞书把每个事件随机交给一个在线连接，多套环境共用一个应用时，事件会被其他环境收走。开发、测试和生产请使用不同的应用。
          </p>
        </li>
        <li>
          <strong>发布版本</strong>
          <p>
            在“版本管理与发布”中创建版本并发布，权限和订阅在发布后才生效；应用的可用范围须包含要接收消息和点击卡片的人。
          </p>
        </li>
      </ol>
    </ElCollapseItem>
  </ElCollapse>
</template>
<style scoped>
.feishu-guide {
  margin: 0 0 16px;
  background: #fff;
  border: 1px solid #e4e8ed;
  border-radius: 8px;
  overflow: hidden;
}
.feishu-guide :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 56px;
  padding: 12px 20px;
  border-bottom: none;
  line-height: 1.4;
}
.feishu-guide :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}
.feishu-guide :deep(.el-collapse-item__content) {
  padding: 0 20px 20px;
  color: #334155;
}
.guide-title {
  display: grid;
  gap: 2px;
}
.guide-title span {
  color: #64748b;
  font-size: 13px;
  font-weight: normal;
}
.guide-lead {
  margin: 0 0 16px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 6px;
}
.guide-steps {
  display: grid;
  gap: 18px;
  margin: 0;
  padding-left: 22px;
}
.guide-steps li::marker {
  font-weight: 600;
}
.guide-steps p {
  margin: 4px 0 0;
}
.guide-table {
  margin-top: 8px;
  width: 100%;
}
.guide-note {
  color: #64748b;
  font-size: 13px;
}
</style>
