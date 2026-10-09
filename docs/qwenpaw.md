# QwenPaw 接入说明

本项目针对 **QwenPaw 2.2.1**，官方源码固定到 `cae5773707b26ab2fd00903f84b712387894b256`。AstrBot 持有微信和 QQ 连接；QwenPaw 负责记忆、执行任务、工作区与浏览器。两边分别维护自己的插件、配置与数据。本项目提供桥接协议，不是改造后的 QwenPaw 或 AstrBot 核心。

## 当前支持范围

- AstrBot 消息与受支持的附件进入 QwenPaw，同一平台会话固定使用服务端随机生成的 `ab_<32位hex>` 会话 ID，保留历史。
- QwenPaw 的最终文字和已验证的共享文件通过 AstrBot 回到原会话。
- QwenPaw 定时任务将最终结果发回已登记的 AstrBot 会话，也可显式调用 `astrbot_send_file` 发送生成文件。
- `astrbot_list_tools()`、`astrbot_call_tool(tool_name, arguments)` 调用管理员允许的 AstrBot 插件工具。工具函数内部读取真实 QwenPaw 会话、用户、频道和 Agent 上下文，不让模型选择身份。
- QwenPaw 的记忆、文件、浏览器等能力在其自身运行环境中执行；桥接不能让服务器直接操作用户电脑上的浏览器或文件。

**0.2 版增加会话隔离的附件传输。** 出站仅发送当前会话共享工作区内真实存在的图片、文件、视频或音频。默认每文件20MiB、每消息4个附件；不支持远程 URL、data URL 或桥接根外路径导出。QQ 音频走 Record；微信接口不支持语音气泡时以文件发送并明确说明。图片理解、音频转写和视频理解仍取决于 QwenPaw 配置的模型与提供商。工作区不需要挂载 AstrBot 的整个 `data` 目录；具体平台行为仍需真实账号联调。

## 安装与配置

将本仓库 `qwenpaw_plugin_astrbot_bridge` 整个目录复制到 QwenPaw 的工作目录 `plugins` 下。官方 Docker 镜像中是：

```text
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.json
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.py
```

插件 manifest 的 ID 为 `astrbot-bridge`，类型为 `channel`。2.2.1 官方 `PluginApi` 允许同一个 channel 插件注册工具；这里同时注册 `astrbot` 频道、两个插件工具桥接函数和两个附件函数，不需要远程 MCP。不要把本仓库的研究源码当运行插件安装。

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
BRIDGE_FILES_ROOT=/bridge-files
BRIDGE_MAX_FILE_BYTES=20971520
BRIDGE_MAX_FILES=4
```

环境变量优先于频道 `callback_url` / `callback_token`；工具也可读取 root `config.json` 中插件 `plugins["astrbot-bridge"].bridge.callback_url/callback_token` 作为后备。已有 AstrBot 服务的网络别名可能是 `astrbot-weixin`，使用部署说明生成的实际内部地址。不能填公网管理后台域名；后台 HTTPS 入口与内部桥接端口是两个服务。

`BRIDGE_FILES_ROOT` 优先于旧版 `BRIDGE_FILES_DIR`，再回落到频道 `files_dir`。AstrBot 和 QwenPaw 的共享根与限额必须一致。限额可降低，不能超过20MiB或4个；0、负数和无效文本会让配置失败。给模型授予共享目录的最小文件读写权限需要单独配置 QwenPaw 文件治理，挂载目录不代表自动授权。

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

返回 `text/event-stream`，每条事件为 `data: <JSON>\n\n`。最终回复是 `object=message,status=completed,type=message,role=assistant` 的 `content`；最终 `object=response,status=completed` 的 `output` 还会包含相同消息，不能重复发送。reasoning、工具调用和工具输出的文字不转成聊天回复。只有明确发送文件的 `send_file_to_user` 已完成工具输出允许提取媒体引用，之后仍需通过共享目录边界检查。

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

Cron 发送生成文件优先使用 `astrbot_media_workspace()` 和 `astrbot_send_file(path, kind)`。它们验证真实 Agent、频道、用户和根会话，不借用已过期 event。任务必须明确得到用户允许发送文件，使用 `share_session=true`、`tool_safety=true` 和 `silent=false`。官方 Cron 的 `silent=true` 只阻止最终 dispatch；2.2.1 没有把该标记传进工具上下文，因此它不会阻止显式发送工具的副作用。静默任务不能调用 `astrbot_send_file`。本插件不猜测静默状态，也不自动批准工具。

## 审批

官方工具审批通过异步 pending request 等待结果，默认超时 300 秒。对于 HTTP Console 发起的任务，不能依赖自定义频道一定收到审批推送。AstrBot 插件查询 `GET /api/approval/list` 的 `pending_approvals`，按 `root_session_id` 和 `owner_agent_id` 过滤，向原会话显示需要审批的操作。

用户明确批准或拒绝后，使用 `/api/approval/approve` 或 `/api/approval/deny`，body 为 `request_id/session_id/user_id`，批准可带 `scope="exact"`。这里 `session_id` 是审批所属的根会话。审批接口返回的 `user_id` 字段本身不是有效鉴权保证；AstrBot 本地仍必须核对操作人和已登记会话。不要对 pending request 自动批准。超时或拒绝会让 QwenPaw 的原任务继续按拒绝结果处理。

## 附件工作流与边界

调用 `astrbot_media_workspace()` 获取当前会话的 `inbound_dir`、`outbound_dir` 和限额，模型不能传入用户或会话参数。例如当前会话为 `ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`：

```text
/bridge-files/ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/inbound/   收到的附件
/bridge-files/ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/outbound/  允许发送的生成文件
```

将生成文件写到返回的 `outbound_dir`，完成写入后调用 `astrbot_send_file("outbound/report.pdf", "file")`。`kind` 可以是 `file`、`image`、`video` 或 `audio`。也可使用该目录内本地绝对路径；插件导出时转成受限相对路径，不向平台暴露服务器路径。已有工作区的文件不会被桥接自动复制；需先通过用户授权的 QwenPaw 文件操作放到本会话 outbound。子目录允许使用，每一层都必须通过无链接检查。

QwenPaw 插件和 AstrBot 网关独立校验所属会话、真实文件、每一层路径、单链接数量、大小和 SHA-256。符号链接、Windows junction/reparse point、hardlink、FIFO、目录、根外文件、另一会话文件以及 outbound 之外的文件都不能导出。读取过程中被修改的文件会拒绝；网关再复制成随机发送快照，重新核对 hash 和 size。附件不可用或超出限制时会返回明确提示，不静默丢弃。

私网 `POST /v1/deliver` 的媒体块为：

```json
{
  "type": "file",
  "path": "ab_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/outbound/report.pdf",
  "filename": "report.pdf",
  "size": 1234,
  "sha256": "文件内容的64位小写hex SHA-256"
}
```

外层仍含真实 `session_id`、`user_id`、`delivery_id`。每个工具调用只生成一次 delivery ID，同次 HTTP 重试保持完全相同请求；网关持久去重表示它已受理，不能冒充微信 / QQ 的送达确认。重新调用工具属于新发送，不能在超时后反复调用来猜测送达情况。

官方输入 schema 使用 `image_url`、`video_url`、音频 `data` 和文件 `file_url/filename`；这些字段可以引用共享入站附件的本地绝对路径。桥接不使用官方 `/console/upload` 存储用户附件，因为它的媒体目录按 Agent 共用而不是按本项目会话隔离；也不把 `/files/preview` 当授权下载出口。

普通 SSE 对话可用 QwenPaw 原生 `send_file_to_user`，前提是文件已在本会话 outbound。2.2.1 将该工具返回的 `DataBlock(URLSource)` 封装为已完成 `plugin_call_output` 的 `data.output` JSON 列表，本项目只提取明确发送工具的媒体字段，保留它直到最终回复并按引用去重；工具文字和内部 `view_image` / 浏览器截图仍不发送。Cron final 模式只派发最后一个完成消息，不能依赖它自动拾取更早的工具附件，因此主动任务优先用本插件的显式发送工具。

## 核实来源

- [QwenPaw 官方稳定版本源码](https://github.com/agentscope-ai/QwenPaw/tree/cae5773707b26ab2fd00903f84b712387894b256)
- [Console 对话 API](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/routers/console.py)
- [流式事件 schema](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/schemas.py)
- [媒体输入转换](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/runtime/message_convert.py)
- [官方文件发送工具](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/agents/tools/send_file.py)
- [真实工具媒体输出封装](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/runtime/envelope.py)
- [插件 channel/tool 注册 API](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/plugins/api.py)
- [频道真实工厂与发送签名](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/channels/base.py)
- [真实运行上下文注入](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/hooks/request_setup/contextvars_hook.py)
- [runtime hook 契约与排序](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/runtime/hooks.py)
- [Cron schema 和默认安全字段](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/crons/models.py)
- [Cron 最终消息分发](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/crons/executor.py)
- [官方 Dockerfile](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/deploy/Dockerfile)

协议测试使用 QwenPaw / AstrBot API 轻量 stub；综合测试另外启动真实 loopback HTTP，串通聊天 SSE、可信上下文、工具回调、网关鉴权与去重、主动消息和审批查询。完整官方 runtime、真实微信/QQ、浏览器执行与模型 API 仍需部署后联调。本地通过测试不等于服务器已经部署或所有平台特性已经可用。
