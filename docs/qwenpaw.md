# QwenPaw 接入说明

本项目针对 **QwenPaw 2.2.1**，官方源码固定到 `cae5773707b26ab2fd00903f84b712387894b256`。AstrBot 持有微信和 QQ 连接；QwenPaw 负责记忆、执行任务、工作区与浏览器。两边分别维护自己的插件、配置与数据。本项目提供桥接协议，不是改造后的 QwenPaw 或 AstrBot 核心。

## 当前支持范围

- AstrBot 文字消息进入 QwenPaw，同一平台会话固定使用服务端随机生成的 `ab_<32位hex>` 会话 ID，保留历史。
- QwenPaw 的最终文字回复通过 AstrBot 回到原会话。
- QwenPaw 定时任务将最终文字结果发回已登记的 AstrBot 会话。
- `astrbot_list_tools()`、`astrbot_call_tool(tool_name, arguments)` 调用管理员允许的 AstrBot 插件工具。工具函数内部读取真实 QwenPaw 会话、用户、频道和 Agent 上下文，不让模型选择身份。
- QwenPaw 的记忆、文件、浏览器等能力在其自身运行环境中执行；桥接不能让服务器直接操作用户电脑上的浏览器或文件。

**0.1 版仅桥接文字。** 收发图片、附件、视频和微信音频还没有完整适配。QwenPaw 生成媒体时会明确提示到控制台查看，不把内部文件路径、媒体 URL 或音频数据发到微信 / QQ。工作区不需要挂载 AstrBot 的整个 `data` 目录。

## 安装与配置

将本仓库 `qwenpaw_plugin_astrbot_bridge` 整个目录复制到 QwenPaw 的工作目录 `plugins` 下。官方 Docker 镜像中是：

```text
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.json
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.py
```

插件 manifest 的 ID 为 `astrbot-bridge`，类型为 `channel`。2.2.1 官方 `PluginApi` 允许同一个 channel 插件注册工具；这里同时注册 `astrbot` 频道和两个本地工具，不需要远程 MCP。不要把本仓库的研究源码当运行插件安装。

QwenPaw 的工作目录通过 `QWENPAW_WORKING_DIR` 指定。官方 Docker 默认 `/app/working`；Agent 默认 ID 是 `default`，其配置在 `/app/working/workspaces/default/agent.json`。将以下内容合并到该 Agent 原有配置，不要覆盖已有模型、记忆或工具配置：

```json
{
  "approval_level": "AUTO",
  "channels": {
    "console": {"enabled": true},
    "astrbot": {
      "enabled": true,
      "callback_url": "http://astrbot:9186",
      "callback_token": "与 AstrBot 一致的私有桥接令牌"
    }
  }
}
```

正式部署从服务器环境变量读取密钥：

```text
ASTRBOT_BRIDGE_URL=http://astrbot:9186
BRIDGE_TOKEN=<私有桥接令牌>
QWENPAW_AGENT_ID=default
QWENPAW_ENABLED_CHANNELS=console,astrbot
```

环境变量优先于频道 `callback_url` / `callback_token`；工具也可读取 root `config.json` 中插件 `plugins["astrbot-bridge"].bridge.callback_url/callback_token` 作为后备。已有 AstrBot 服务的网络别名可能是 `astrbot-weixin`，使用部署说明生成的实际内部地址。不能填公网管理后台域名；后台 HTTPS 入口与内部桥接端口是两个服务。

两边配置的 `QWENPAW_AGENT_ID` 必须一致。桥接工具拒绝其他 Agent、Console 会话、未登记的会话以及缺失用户上下文。AstrBot 为每轮对话生成新的 32 位随机 `turn_id`，通过可信原生请求上下文传入 QwenPaw；插件的 PRE_DISPATCH hook 将它放入 ContextVar，工具自行附带它。模型工具参数不包含会话、身份或 turn ID。网关验证 nonce 与仍有效的原始 AstrBot event 一致，防止旧任务迟到的工具调用绑定下一轮 event；FINALLY hook 清理当前请求的 nonce。单有会话 ID 不构成授权。

官方 `fork_agent` 保留原频道和用户，桥接通过 ephemeral approval route 的 `channel_meta` 继承原 nonce；只有原轮仍活跃时才可执行 AstrBot 工具。普通 `spawn_agent` 路径没有显式传原频道，本版可能将其视为 Console 并拒绝 AstrBot 工具；它仍可使用 QwenPaw 自身工具。无 nonce 的后台任务不得恢复或猜测历史 nonce。

QwenPaw 模型提供商与 AstrBot 模型配置独立，需要在 QwenPaw 配置有效模型。可使用云端 API，无须在这台服务器运行本地大模型。浏览器和长任务会增加内存占用；共享的 2GB 服务器应限制同时执行数，再实测资源。

## 实际 HTTP 接口

QwenPaw 2.2.1 对话接口是：

```http
POST /api/agents/default/console/chat
X-QwenPaw-Runtime-Token: <QWENPAW_RUNTIME_INTERNAL_TOKEN>
Content-Type: application/json
```

```json
{
  "session_id": "ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "user_id": "登记的用户 ID",
  "channel": "astrbot",
  "request_context": {"astrbot_bridge_turn_id": "cccccccccccccccccccccccccccccccc"},
  "input": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
}
```

返回 `text/event-stream`，每条事件为 `data: <JSON>\n\n`。最终回复是 `object=message,status=completed,type=message,role=assistant` 的 `content`；最终 `object=response,status=completed` 的 `output` 还会包含相同消息，不能重复发送。reasoning、工具调用和工具输出不转成聊天回复。

同一个会话有任务运行时，新发消息返回 HTTP 409，需要在 AstrBot 侧排队。断开 SSE 不会终止任务；`reconnect:true` 可以重新连接已有任务。不要因网络超时自动重新执行同一任务。停止接口接受真实 ChatSpec UUID：`POST /api/agents/default/console/chat/stop?chat_id=<UUID>`；它的 session fallback 只查 Console 频道，不应依赖该 fallback 停止 AstrBot 会话。

普通 QwenPaw 登录鉴权是 `QWENPAW_AUTH_ENABLED=true` 下的 `Authorization: Bearer <登录 token>`；本项目使用单独的 `QWENPAW_RUNTIME_INTERNAL_TOKEN` 与 `X-QwenPaw-Runtime-Token` 保护整个内部服务。二者含义不同。桥接回 AstrBot 使用 `Authorization: Bearer <BRIDGE_TOKEN>`，不要混用令牌，也不要将内部端口公开到公网。

Console 的 SSE 处理路径只输出 SSE 并打印回复，不会再向 `astrbot` 频道调用 `send`，所以正常聊天不会天然发送两份回复。定时任务通过其显式 dispatch 调用 `astrbot` 频道。

## 主动任务

让定时任务使用同一个原会话，设 `dispatch.channel="astrbot"`、原来的 `user_id/session_id`、`dispatch.mode="final"`。本版不支持将 Cron 的中间流式输出逐条转发。候选会话可从 `GET /api/agents/default/cron/dispatch-targets?channel=astrbot` 查询，不要自己猜微信 / QQ ID。

以下是参考 job body，不会自动创建任务。`POST /api/agents/default/cron/jobs` 才会保存它：

```json
{
  "name": "每日简报",
  "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
  "task_type": "agent",
  "request": {
    "input": [{"role": "user", "content": [{"type": "text", "text": "整理我已授权关注的内容，给出今天的简报。"}]}]
  },
  "dispatch": {
    "channel": "astrbot",
    "target": {"session_id": "ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "user_id": "登记的用户 ID"},
    "mode": "final",
    "silent": false
  },
  "save_result_to_inbox": true,
  "runtime": {"tool_safety": true, "share_session": true, "max_concurrency": 1, "timeout_seconds": 600}
}
```

`save_result_to_inbox=true` 将记录保存在 QwenPaw Inbox，同时将最终回复发送一次到 AstrBot；Inbox 记录不是第二条微信通知。固定文字任务可使用 `task_type=text` 与 `text`，每次执行生成新的 delivery ID。同一次 HTTP 重试保留 ID；网关去重只能说明网关已受理，不能保证微信或 QQ 一定收到。

务必显式设置 **`runtime.tool_safety=true`**。官方 2.2.1 的默认值是 `false`，会把该 Cron 执行的审批级别改为 `OFF`。`true` 设为 `AUTO`，被拦截的操作等待用户批准；它不代表自动批准一切。`share_session=true` 将历史和主动回复保持在原会话。Cron 没有当前聊天的 turn nonce，也没有仍有效的原始 AstrBot event，因此不能借用 `astrbot_list_tools` / `astrbot_call_tool`；主动任务应使用 QwenPaw 自身工具，最终回复仍可回到原 AstrBot 会话。

## 审批

官方工具审批通过异步 pending request 等待结果，默认超时 300 秒。对于 HTTP Console 发起的任务，不能依赖自定义频道一定收到审批推送。AstrBot 插件查询 `GET /api/approval/list` 的 `pending_approvals`，按 `root_session_id` 和 `owner_agent_id` 过滤，向原会话显示需要审批的操作。

用户明确批准或拒绝后，使用 `/api/approval/approve` 或 `/api/approval/deny`，body 为 `request_id/session_id/user_id`，批准可带 `scope="exact"`。这里 `session_id` 是审批所属的根会话。审批接口返回的 `user_id` 字段本身不是有效鉴权保证；AstrBot 本地仍必须核对操作人和已登记会话。不要对 pending request 自动批准。超时或拒绝会让 QwenPaw 的原任务继续按拒绝结果处理。

## 媒体扩展契约（尚未实现）

后续可以使用两容器共同挂载的 `/bridge-files`，通过严格相对路径引用文件。协议必须拒绝 `..`、绝对路径、URL、Windows 驱动器、符号链接逃逸，验证实际解析路径与文件大小后再发送。不允许以“兼容附件”为由给 QwenPaw 挂载整个 AstrBot 数据目录或读取其凭据。微信音频另有平台与编码限制，应单独适配、测试，不能以 QQ 支持音频推断微信也支持。

## 核实来源

- [QwenPaw 官方稳定版本源码](https://github.com/agentscope-ai/QwenPaw/tree/cae5773707b26ab2fd00903f84b712387894b256)
- [Console 对话 API](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/routers/console.py)
- [流式事件 schema](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/schemas.py)
- [插件 channel/tool 注册 API](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/plugins/api.py)
- [频道真实工厂与发送签名](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/channels/base.py)
- [真实运行上下文注入](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/hooks/request_setup/contextvars_hook.py)
- [runtime hook 契约与排序](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/runtime/hooks.py)
- [Cron schema 和默认安全字段](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/crons/models.py)
- [Cron 最终消息分发](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/crons/executor.py)
- [官方 Dockerfile](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/deploy/Dockerfile)

协议测试使用 QwenPaw / AstrBot API 轻量 stub；综合测试另外启动真实 loopback HTTP，串通聊天 SSE、可信上下文、工具回调、网关鉴权与去重、主动消息和审批查询。完整官方 runtime、真实微信/QQ、浏览器执行与模型 API 仍需部署后联调。本地通过测试不等于服务器已经部署或所有平台特性已经可用。
