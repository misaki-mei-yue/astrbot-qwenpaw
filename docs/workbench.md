# 组合工作台

登录 AstrBot 后台，在 **AstrBot + QwenPaw 组合工作台** 插件的页面入口打开 **组合工作台**。它使用 AstrBot 4.25.1 原生 Plugin Pages，自动发现 `pages/workbench/index.html`。已有 AstrBot HTTPS 域名继续作为入口，无需给工作台增加公开端口。

## 可以做什么

- **运行概览**：桥接状态、目标智能体是否已加载、模型是否配置、自动记忆状态和已建立会话。模型已配置不代表实际连接测试通过；会话记录不代表微信 / QQ 当前在线。
- **主动任务**：选择已允许并建立过的微信 / QQ 会话，创建单次、每天或工作日任务。提醒直接发送文字；智能任务让 QwenPaw 执行描述的事情，最终结果发送到所选会话。每日 / 工作日使用北京时间，单次时间按当前设备时区输入并转换为明确时间点。
- **长期记忆**：按日常记忆和整理摘要浏览真实目录，纯文本查看内容。当前是只读查看，避免覆盖正在由智能体整理的记忆。该目录属于当前智能体，并非按微信 / QQ 用户隔离的私人档案。
- **工具能力**：查询 QwenPaw 实际登记的工具及启用状态，支持搜索。AstrBot 导出工具仍受桥接白名单和消息事件的插件范围控制。

页面加载不会调用模型、创建任务、执行工具或发送消息。创建并启用任务是明确的保存操作；暂停 / 恢复会改变后续调度。暂停不会撤销已经开始的执行。

## 身份与配置

`OWNER_USER_IDS` 和 `TOOL_ALLOWLIST` 非空时优先；为空或空白时使用 AstrBot 后台的对应插件设置。保存后按 AstrBot 的插件配置流程重新加载插件。两者都未配置时保持默认关闭；用户白名单中的 `*` 不生效。

任务接收者来自服务端保存且目前仍在白名单内的真实会话，页面不能伪造任意收件人。群聊路由会把结果发到原群。定时任务保留 QwenPaw 工具审批，危险操作可能等待原会话批准；不会借用已过期的 AstrBot 事件来调用其插件工具。

创建请求采用持久化去重。遇到超时，页面先要求刷新任务列表确认，不自动重发创建请求。若后台已经创建，列表中可以看到。若不存在，再新建一次；网络返回不确定时不要连续创建同名任务。

## 安全边界

页面通过 AstrBot 官方 bridge SDK 请求 `/api/plug/astrbot_plugin_qwenpaw_bridge/workbench/...`。这些接口受 AstrBot dashboard JWT / cookie 认证；插件页面资源的临时 token 无权调用管理接口。写接口同时检查 JSON 内容类型和跨站来源。

QwenPaw 运行时密钥只由服务端使用。页面没有任意上游 URL、任意文件路径、模型密钥或完整配置导出接口。状态与工具结果按字段筛选；记忆内容仅在选中文件时读取，并按纯文本显示。

工作台暂不替代 QwenPaw 的完整模型设置、浏览器画面、技能管理和记忆配置界面，也不替代 NapCat 的 QQ 扫码页面。原生后台仍可处理这些设置。统一设置和浏览器任务体验继续按项目验收清单推进。

## 本地界面检查

在独立环境安装 Playwright，并让 Node 能找到它：

```sh
npm install --prefix /tmp/workbench-ui --no-package-lock playwright@1.62.1
node /tmp/workbench-ui/node_modules/playwright/cli.js install chromium
NODE_PATH=/tmp/workbench-ui/node_modules node scripts/check_workbench_ui.cjs
```

检查使用纯测试数据，覆盖桌面 / 手机、创建任务、暂停 / 恢复、不确定创建结果防重复、记忆文本安全、工具搜索和独立页面提示。`WORKBENCH_BROWSER` 可指定已有 Chromium 可执行文件；`WORKBENCH_SCREENSHOTS` 可指定截图目录。它不登录真实账号或发送平台消息。
