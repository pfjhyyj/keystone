---
name: Keystone Console
description: 中文企业管理控制中心，优先呈现待处理事项、可信状态与可追踪费用。
colors:
  primary: "#246a56"
  primary-hover: "#2f7b62"
  primary-active: "#1e5d49"
  primary-soft: "#eaf1ee"
  primary-border: "#d4e3de"
  graphite: "#232b36"
  nav-hover: "#303b49"
  nav-selected: "#35463f"
  nav-text: "#d1d8e2"
  nav-selected-text: "#edf7f1"
  workspace: "#f7f8fa"
  surface: "#ffffff"
  text: "#263445"
  heading: "#1d2b3c"
  regular: "#526173"
  muted: "#617084"
  placeholder: "#6b7788"
  border: "#dce2e9"
  border-light: "#e8ecf1"
  panel-border: "#e4e8ed"
  divider: "#edf0f4"
  success: "#2f7655"
  success-hover: "#367f5c"
  success-soft: "#eef6f0"
  success-border: "#d8e9df"
  warning: "#8b5f18"
  warning-hover: "#96691e"
  danger: "#b13943"
  danger-hover: "#bd4650"
  info: "#5e6b7e"
  info-hover: "#65748a"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', Arial, sans-serif"
    fontSize: "24px"
    fontWeight: 650
    lineHeight: 1.45
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', Arial, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', Arial, sans-serif"
    fontSize: "14px"
    lineHeight: 1.7
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', Arial, sans-serif"
    fontSize: "13px"
    lineHeight: 1.4
  small:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', Arial, sans-serif"
    fontSize: "12px"
  mono:
    fontFamily: "'Cascadia Code', Consolas, monospace"
    fontSize: "12px"
    lineHeight: 1.7
rounded:
  control: "6px"
  panel: "8px"
  dialog: "10px"
  chart: "12px"
  status: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  control-gap: "12px"
  md: "16px"
  panel-gap: "20px"
  lg: "24px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "8px 15px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.surface}"
  button-primary-active:
    backgroundColor: "{colors.primary-active}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.regular}"
    rounded: "{rounded.control}"
    padding: "8px 15px"
    height: "32px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.regular}"
    rounded: "{rounded.control}"
    height: "32px"
  nav-item:
    textColor: "{colors.nav-text}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "42px"
  nav-item-selected:
    backgroundColor: "{colors.nav-selected}"
    textColor: "{colors.nav-selected-text}"
  status-success:
    backgroundColor: "{colors.success-soft}"
    textColor: "{colors.success}"
    rounded: "{rounded.status}"
    padding: "0 7px"
    height: "20px"
  work-panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
  row-more:
    textColor: "{colors.regular}"
    padding: "2px 6px"
    height: "26px"
  usage-trend:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.chart}"
    padding: "18px"
  detail-summary:
    textColor: "{colors.text}"
    padding: "20px 0 18px"
---

# Design System: Keystone Console

## Overview

**Creative North Star: "管理控制中心"**

以管理员的处理顺序组织界面：先识别值得关注的状态，再定位资源、调用或运行，最后在权限范围内执行操作。石墨侧栏提供稳定定位，冷白工作区承载紧凑的中文信息，青绿强调当前选择与主要动作。

界面依靠文字层级、细分隔线和克制的状态色建立秩序。费用、Token、身份与运行状态必须能逐项核对；未知、未报告和无权限都需要明确文案。页面保留真实业务约束，示例数据不能被呈现成生产事实。

**Key Characteristics:**

- 石墨导航与冷白工作区，白色工作面板。
- 中文系统无衬线字体、紧凑表格、等宽数字。
- 先摘要与可执行动作，再展开技术详情。
- 以真实权限、状态与原单位费用约束展示。

本规范以已实现的 Vue 与 Element Plus 组件为依据；数值原语以文档 frontmatter 为准。

## Colors

主色是沉稳青绿，配合石墨导航、冷灰边界和白色工作面。

### Primary

- **管理青绿**（primary）：主要提交、文本链接、输入焦点与交互选择。悬停和按下使用现有 primary-hover、primary-active 变体，不自行换成另一种强调色。
- **浅青绿**（primary-soft、primary-border）：次要按钮交互与轻量强调背景、边界。

### Neutral

- **石墨**（graphite）：固定侧栏；nav-hover 与 nav-selected 区分悬停、当前页，当前页同时使用文字加粗和标记。
- **冷白与工作白**（workspace、surface）：分别用于画布与主要工作面；白色面板以 panel-border 分离。
- **墨色文字**（text、heading）：正文与页标题；regular 用于字段内容。
- **可读灰**（muted）：说明、表头、辅助数据；placeholder 保留字段占位的区分。不得用更浅装饰灰替换正文说明。
- **细规则线**（border、border-light、panel-border、divider）：控件轮廓、容器及行分隔，不加厚形成卡片网格。

### Semantic

- **确认绿**（success）：成功、启用等已确认状态。
- **待处理赭色**（warning）：运行中、待处理、未知等需进一步判断的状态。
- **错误红**（danger）：失败、拒绝、错误与危险确认。
- **中性状态灰**（info）：停用或其他中性状态。状态标签必须带可读文本。语义按钮的悬停使用 success-hover、warning-hover、danger-hover、info-hover，保持白字可读性；不回退到组件库默认浅色悬停。

**The Evidence Rule.** 颜色表达已有事实；查询失败显示无法确认，未知费用与未报告用量不能显示为零，权限不足不能显示为空结果。

## Typography

**Body Font:** 采用 frontmatter 的中文系统无衬线字体栈。无下载字体，系统按自身可用字体回退。
**Label/Mono Font:** 技术代码与原始字段使用 mono；业务名称、中文说明保持系统字体。

文字精确、平静，允许中文和英文技术标识混排。全局启用 `font-variant-numeric: tabular-nums`，金额、调用次数和时间便于纵向核对。

- **Headline:** 页面标题；窄屏降至（20px）。
- **Title:** 面板标题；细节抽屉对象名使用（21px / 650），不把长 ID 当标题。
- **Body:** 简短说明与段落，保留现有行距。
- **Label / Small:** 表格正文（13px）、表头和辅助信息（12px）；避免让所有信息同等加粗。
- **Metric:** 已知金额与详情费用（28px / 600），Token 事实值（18px / 600）。不增加装饰性大号数字。

**The Readable Detail Rule.** 长业务标识可在表格截断并保留完整标题提示；详情与原始字段允许换行，不靠缩小字体隐藏内容。

## Layout

桌面固定侧栏（208px），折叠后（72px），顶部栏（54px）吸顶；工作区内边距（24px）。侧栏内部独立滚动，主体最小宽度为零，避免横向溢出撑大整个页面。面板通常采用（20–24px）内边距，块间距（20–24px）。

总览用紧凑状态条、待关注列表、用量汇总和渠道表格建立优先级。宽屏双列的总览与模型编辑预览在（1150px）以下合并单列；模型预览由吸顶改为普通文流。工作区在（1100px）以下收至（18px）。

在（800px）以下，侧栏改成（220px）宽的遮罩导航，主体不预留侧栏宽度，工作区收至（14px），普通页面面板内边距（16px），表单双列合并。移动导航具备焦点循环、Escape 关闭、关闭后返回触发点和主体 inert 处理。

用量筛选使用自适应网格，常用条件在前，自定义时间及次要维度折叠。在（600px）以下，筛选面板默认收起，用明确按钮控制；已应用条件标签仍保留。查询范围、分页、分析维度、抽屉对象和模型配置标签与 URL 同步，关闭详情返回既有范围。

表格保持原生水平滚动与列最小宽度，最后的操作列在滚动时固定在右侧并使用不透明背景。保留表格语义与分页，不将窄屏数据表硬挤到视口。总览原生 HTML 表也在自身容器内水平滚动。

## Elevation & Depth

工作面默认平整，通过画布色、白色容器与单像素边界形成层级。日常面板无额外阴影；抽屉、对话框与行操作浮层沿用 Element Plus 的结构性层级。移动导航遮罩使用代码中的半透明墨色，保持导航与底页的关系。

行操作浮层沿用 Element Plus 的轻阴影（`0px 0px 12px #0000001f`）；不要把浮层阴影复制到所有面板。控件状态沿用现有短过渡，移动导航使用（0.2s）位移；`prefers-reduced-motion: reduce` 时全局关闭过渡与动画，不增加持续装饰运动。

**The Flat Workspace Rule.** 常驻内容用边界与间距分层；阴影服务临时浮层，不能成为每个工作区块的默认外观。

## Shapes

控件与轻量摘要使用 control 圆角；主要工作面使用 panel 圆角；对话框使用 dialog 圆角；小状态标签为胶囊。分隔线、表格与表单分区保持直线秩序，避免将每个字段包成独立大圆角卡片。表单分区使用标题和底部分隔线，输入边界负责点击区域提示。

## Components

### Buttons and inputs

标准按钮高度、内边距与圆角见 frontmatter；主提交使用青绿底白字，次要操作白底细边界，行内操作使用紧凑文本按钮。保留 Element Plus 的 hover、active、disabled、loading；按钮和链接键盘焦点有青绿（2px）轮廓与（3px）偏移。输入使用白底、细轮廓，焦点切换到主色；错误靠文本与错误色共同表达。字段标签始终可见，提示不能替代标签。

### Navigation

侧栏以“工作台 / 运行与消费 / 资源 / 访问与审计”分组。只呈现当前账户可访问入口，空分组消失。当前页同时具有选中背景、文字强调与 `aria-current`；折叠导航保留可访问名称与标题提示。Keystone 字标与 K 标记来自文字和 CSS，不引入推测的服务商 logo。

### Status and applied filters

状态标签为小号、浅底、文本明确的胶囊；success、danger、warning、info 使用既有映射。筛选标签描述已生效条件，可移除；折叠筛选不隐藏当前范围。运行状态、费用核对状态与启用状态不能混成一个“正常”标记。

### Tables and row actions

表头（44px）高、浅工作区背景；常规行正文（13px），上下内边距（10px），模型配置行（12px）。业务名称领先，ID 与元信息次级。常用编辑／查看操作直接可见；修改名称、删除等次要操作收进“更多”。触发器带对象名称与展开状态，浮层底端右对齐（190px），选项点击后关闭。无权限操作保持既有显隐或禁用及原因。

### Usage trend

趋势用青绿柱形表示调用数或指定原单位的已知费用，配套最大值／半量值／零值数量级及中间时间刻度。上海时间范围与未知费用数量保持明确；柱形可以点击或键盘 Enter／Space 下钻。在（600px）以下隐藏中间刻度，保留两端时间。图表只统计真实已有数据，不添加费用预测。

### Full-page model editor

模型创建／编辑采用完整页面，顺序为模型来源（渠道、上游模型）、基本信息（key、显示名称）、实际操作能力。右侧是实时配置预览，展示将要保存的名称、渠道、上游、能力和启停状态。桌面预览吸顶，窄屏落入正文。取消与保存位于视口底部固定页脚，起点遵循侧栏宽度；正文预留（88px）底部空间。保护未保存内容；编辑后的固定 key、真实协议能力和不可删除引用约束必须保留。测试消息仍不持久化。

### Summary drawers and dialogs

详情抽屉宽度为 `min(620px, 100vw)`，先对象名与状态、费用或最近执行确认，再给刷新、补查或取消等真实可用动作。调用详情明确“未知不代表未计费，补查不重新发模型请求”；Token 先以事实列表呈现。Flow 结果先显示可读标量与步骤历史，技术标识、输入、原始字段折叠披露；取消受理与执行确认分开。抽屉只读错误、部分数据不可用与权限失败分别呈现。需要保存的表单保留未保存确认，忙碌时不能关闭；模态框背景点击不关闭。

### Role assignment

角色分配抽屉逐项显示名称、权限数量、当前分配状态与不可授予原因。点击“分配角色”或“解除角色”立即保存，不增加误导性的统一保存按钮。操作请求中禁用重复交互，权限与自身会话变化继续约束界面。

## Do's and Don'ts

### Do:

- **Do** 使用中文系统字体、等宽数字与可读灰说明，保持紧凑的信息层级。
- **Do** 将常用操作保留在表格行中，次要操作收进带对象名称的“更多”。
- **Do** 保留原单位金额，并分别说明已知、未知、待核对、账户余额与 Key 额度。
- **Do** 让筛选、分页和详情关闭延续 URL 范围，表单退出保护未保存内容。
- **Do** 以真实权限约束入口、字段和动作，给不可用状态可理解的说明。
- **Do** 将方案图与合成预览标为设计证据，运行时以真实组件构建。

### Don't:

- **Don't** 把未知费用、未报告用量、查询失败或无权限改成零值或空记录。
- **Don't** 把调用或 Flow 的原始 JSON 作为首要信息，也不添加不存在的操作。
- **Don't** 为服务商创造 logo、插图或其他未经提供的身份资产。
- **Don't** 将设计证据栅格图嵌入生产界面，或据合成预览宣称生产可用与像素级一致。
- **Don't** 用额外阴影、渐变与大指标卡削弱状态、事项与工作表格的层次。
