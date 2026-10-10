# QwenPaw 接入说明

本项目针对 **QwenPaw 2.2.1**，官方源码固定到 `cae5773707b26ab2fd00903f84b712387894b256`。AstrBot 持有微信和 QQ 连接；QwenPaw 负责记忆、执行任务、工作区与浏览器。两边分别维护自己的插件、配置与数据。本项目通过真实聊天 API、原生 Agent 工作区、插件回调和受限浏览器把能力连起来，管理入口仍沿用两边原生后台。当前分支已实现个人身份、账号绑定、显式记忆管理与工具边界，并通过自动测试和真实 Chromium 离线网页测试；尚未验收真实模型的对话提炼、跨会话召回效果，以及微信／QQ 实际送达。

## 使用两边原生后台

AstrBot 原生后台管理微信/QQ 连接、AstrBot 插件、桥接所有者和工具白名单。QwenPaw 原生 Console 管理自己的模型、记忆、文件、浏览器工具和主动任务；两边沿用官方页面，当前没有统一登录。`QWENPAW_AGENT_ID` 指定配置模板，通常为 `default`；它不再是所有聊天共用的记忆空间。首次实际对话或记忆操作会建立 `abp_<32位hex>` 个人 Agent，群聊使用 `abg_<32位hex>`。配置个人模型、查看记忆和创建任务前，在 Console 切到对应受管 Agent，避免把其他人的资料或任务放到模板空间。

| 要做的事 | QwenPaw 2.2.1 原生页面 |
|---|---|
| 配置模型提供商和实际使用的模型 | `/models` |
| 查看 ReMe 状态，调整自动记录、整理、检索和 embedding | `/agent-config` 的记忆页签 |
| 查看、编辑日常记忆和整理后的记忆文件 | `/files` 中的 daily / digest 分区 |
| 创建、编辑、启停任务，查看执行结果 | `/cron-jobs`；心跳单独在 `/heartbeat` |
| 查看原生工具和本插件五个工具 | `/tools` |
| 启用 `astrbot` 出站频道 | `/channels` |

这些页面来自固定版本的官方前端，调用其真实配置与任务 API；本项目使用官方 Dockerfile 的构建步骤，会同时打包原生 Console。模型配置独立于 AstrBot，不会自动复制另一边的密钥或提供商。

现有 Compose 已含 `console` 代理：服务器的 `127.0.0.1:8088` 通向完整原生 Console，`deploy/console.Caddyfile` 只为请求补上内部 runtime header，不改变页面。通过 SSH 隧道或管理工具的端口转发访问，例如转发到本机后打开 `http://127.0.0.1:8088/`。服务器上的 localhost 不是你电脑的 localhost；只在网页终端里连接服务器，不会自动建立端口转发。详细访问步骤见[部署说明](deployment.md#原生后台的访问与登录)。已有 AstrBot 管理域名继续访问 AstrBot 后台；QwenPaw 使用自己的原生入口，新管理域名尚未由本仓库配置。

当前部署默认 `QWENPAW_AUTH_ENABLED=false`，没有额外 QwenPaw 用户登录页，入口由 loopback 绑定和隧道/外层认证保护，内部 HTTP/WebSocket 仍要求 `QWENPAW_RUNTIME_INTERNAL_TOKEN`。官方可启用原生账号登录：`QWENPAW_AUTH_ENABLED=true` 时，首位用户在 `/login` 注册，之后用该账号登录；也支持环境变量 `QWENPAW_AUTH_USERNAME` / `QWENPAW_AUTH_PASSWORD` 首次建号。但本版 AstrBot 客户端只携带 runtime header，尚未支持额外登录 Bearer token；账号注册后不能只切换此开关就声称桥接仍可用。对外提供管理域名时，先为原生 Console 的外层代理配置认证与 HTTPS，并保留内部服务边界。

## 身份、记忆与当前支持范围

账号由平台类型、机器人连接 ID 和发送者 ID 共同识别；不同平台或不同机器人连接上相同的字符串 ID 不会自动成为同一个人。每个账号默认获得独立的私聊 Agent，同账号的不同私聊会话共享个人记忆。群聊按群会话和发送者分配独立 Agent，既不读取私聊记忆，也不与同群其他成员共用记忆。

同一人的微信和 QQ 需要显式绑定。在已获授权的源账号私聊发送 `/paw link`，再在另一个已获授权的账号私聊发送 `/paw link 绑定码`。绑定码只存哈希，10 分钟有效且仅能成功兑换一次。**先绑定，再让目标账号建立独立记忆空间**；已有工作区或已有其他绑定关系的目标账号会拒绝自动合并，不搬动、不覆盖旧记忆。群内不能发起或兑换绑定。详细命令见[个人记忆说明](personal-memory.md)。

- `/paw status` 查看当前范围；`/paw new` 只更换当前会话，长期记忆保留，旧私聊会话的主动任务需重新选择接收会话。
- `/paw remember 内容`、`/paw memory`、`/paw correct 记忆ID 内容`、`/paw forget 记忆ID` 管理本范围的显式备注；不会触及其他用户的笔记。
- 自动提炼、检索和整理由独立 Agent 的原生 ReMe 负责。代码已设置每个 Agent 自己的目录与记忆搜索配置；真实模型效果尚待验证，不能把保存备注的测试当成自动理解用户的验收。
- 删除显式备注只清理该备注；原生检索索引异步同步，期间可能短暂召回旧内容。不等于同时删除历史聊天及另外提炼的自动记忆。其他记忆需在对应 Agent 的原生后台继续管理。
- 原有共享／default Agent 的历史不会自动导入个人空间。身份、会话和回传收据存于持久数据库；工作区保存于 QwenPaw 持久数据目录，容器重启不应改动这些归属。

本插件注册以下五个工具。这些能力与后台是否能打开是不同的验收项：

| 工具 | 能力和边界 |
|---|---|
| `astrbot_list_tools()` | 列出当前活跃 AstrBot 事件允许调用的插件工具 |
| `astrbot_call_tool(tool_name, arguments)` | 调用允许列表内的工具，保留 AstrBot 原事件及插件权限 |
| `astrbot_media_workspace()` | 返回当前真实会话的入站／出站附件目录 |
| `astrbot_send_file(path, kind)` | 发送本会话出站目录里的已校验文件 |
| `astrbot_browser(action, url, selector, text)` | 在当前受管 Agent 的独立浏览器中打开、读取、点击、填写、查看状态和关闭网页 |

结构化浏览器使用真实 Chromium，每个受管 Agent 的页面与 Cookie 分开；进程重启后建立新浏览器上下文。它不接受 Python、JavaScript、用户指定 profile、文件上传或下载接口，只访问通过检查的公开 HTTP(S) 地址。重定向和每个网络请求都会检查，WebSocket、弹窗及下载会被阻止或关闭；DNS 检查不是网络隔离沙箱，需要严格网络边界时还应配置出站防火墙。

受管 Agent 使用官方 `register_middleware` / AgentScope `on_acting` 拦截工具执行。原生文件工具仅能读写本 Agent 普通工作文件和本会话附件目录；运行配置、凭据、插件及策略目录受保护，跨用户路径、链接与硬链接拒绝。递归搜索包含这些受保护内容时会拒绝，请缩小到普通资料或记忆子目录。原生任意 Python 浏览器、shell、批量工具执行、跨 Agent 委派和未经审核的新工具均禁用；原生 `web_fetch` 不开放，网页操作走结构化浏览器，`web_search` 只允许固定提供商的查询字段。独立的 default／其他非受管 Agent 不会套用此中间件。**这是模型工具调用边界，不是整个进程的 OS 沙箱**，也不能让服务器直接操作用户电脑上的文件或浏览器。

附件传输仍按会话隔离。出站仅发送当前会话共享工作区内真实存在的图片、文件、视频或音频，默认每文件 20MiB、每消息 4 个附件；不支持远程 URL、data URL 或桥接根外路径导出。QQ 音频走 Record；微信接口不支持语音气泡时以文件发送并明确说明。图片理解、音频转写和视频理解依赖所选模型与提供商，具体平台行为需真实账号联调。

## 安装与配置

将本仓库 `qwenpaw_plugin_astrbot_bridge` 整个目录复制到 QwenPaw 的工作目录 `plugins` 下。官方 Docker 镜像中是：

```text
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.json
/app/working/plugins/qwenpaw_plugin_astrbot_bridge/plugin.py
```

插件 manifest 的 ID 为 `astrbot-bridge`，类型为 `channel`。2.2.1 官方 `PluginApi` 允许同一个 channel 插件注册工具；这里同时注册 `astrbot` 频道、两个插件工具桥接函数、两个附件函数和一个结构化浏览器工具，还注册会话上下文生命周期 hook 与工作区工具中间件，不需要远程 MCP。不要把本仓库的研究源码当运行插件安装。

QwenPaw 的工作目录通过 `QWENPAW_WORKING_DIR` 指定。官方 Docker 默认 `/app/working`；配置模板通常为 `default`，其配置在 `/app/working/workspaces/default/agent.json`。下面是模板的最小频道示例；合并时保留已有模型配置。桥接通过官方 Agent API 创建各自的受管工作区，只复制必要配置并采用允许的技能名单，不复制模板的聊天、记忆或个人资料。不要手工修改受管 Agent 的工作区路径或记忆目录：

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

两边配置的 `QWENPAW_AGENT_ID` 模板标识必须一致。实际聊天每次使用已保存身份对应的个人／群 Agent ID，并在回调里携带真实 `agent_id`；AstrBot 校验它与持久路由一致。受管工作区带与 runtime token 关联的管理标记，重启后会重新校验，不会创建失败就退回 default。令牌轮换后需由管理员迁移已有管理标记。桥接工具拒绝未经认可的 Agent、Console 会话、未登记路由和缺失用户上下文。AstrBot 为每轮对话生成新的 32 位随机 `turn_id`，通过可信原生请求上下文传入 QwenPaw；插件的 PRE_DISPATCH hook 将它放入 ContextVar，工具自行附带它。模型工具参数不包含会话、身份或 turn ID。网关验证 nonce 与仍有效的原始 AstrBot event 一致，防止旧任务迟到的工具调用绑定下一轮 event；FINALLY hook 清理当前请求的 nonce。单有会话 ID 不构成授权。

受管 Agent 禁用 `fork`／`spawn`／跨 Agent 委派等可能跳出个人工具边界的执行方式，不能把官方产品原有的多智能体能力当作本版已经安全开放的能力。原有协议中的 nonce 继承逻辑不等于授予委派权限。无 nonce 的后台任务不得恢复或猜测历史 nonce；后台任务仍可使用当前受管 Agent 已允许的原生工具和结构化浏览器。

QwenPaw 模型提供商与 AstrBot 模型配置独立，需要在 QwenPaw 原生 Console 配置有效模型。可使用云端 API，无须在服务器运行本地大模型。浏览器和长任务的并发数、容器限额按实际工作负载配置和测试，不以现有服务器容量裁掉功能。

## 实际 HTTP 接口

QwenPaw 2.2.1 对话接口如下，`{agent_id}` 必须是当前路由记录的 `abp_...` 或 `abg_...`，不能写成模板 default：

```http
POST /api/agents/{agent_id}/console/chat
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

同一个会话有任务运行时，新发消息返回 HTTP 409，需要在 AstrBot 侧排队。断开 SSE 不会终止任务；`reconnect:true` 可以重新连接已有任务。不要因网络超时自动重新执行同一任务。停止接口接受真实 ChatSpec UUID：`POST /api/agents/{agent_id}/console/chat/stop?chat_id=<UUID>`；它的 session fallback 只查 Console 频道，不应依赖该 fallback 停止 AstrBot 会话。

普通 QwenPaw 登录鉴权是 `QWENPAW_AUTH_ENABLED=true` 下的 `Authorization: Bearer <登录 token>`；本项目使用单独的 `QWENPAW_RUNTIME_INTERNAL_TOKEN` 与 `X-QwenPaw-Runtime-Token` 保护整个内部服务。开启原生账号登录时两层认证同时生效，runtime header 不替代登录 token；当前桥接尚未携带后者。桥接回 AstrBot 使用 `Authorization: Bearer <BRIDGE_TOKEN>`，不要混用令牌，也不要将内部端口公开到公网。

Console 的 SSE 处理路径只输出 SSE 并打印回复，不会再向 `astrbot` 频道调用 `send`，所以正常聊天不会天然发送两份回复。定时任务通过其显式 dispatch 调用 `astrbot` 频道。

## 主动任务

在对应个人／群 Agent 内创建定时任务，并使用它自己的原会话，设 `dispatch.channel="astrbot"`、原来的 `user_id/session_id`、`dispatch.mode="final"`。本版不支持将 Cron 的中间流式输出逐条转发。候选会话可从 `GET /api/agents/{agent_id}/cron/dispatch-targets?channel=astrbot` 查询，不要自己猜微信 / QQ ID，也不要在 default Agent 中创建任务后填入个人 Agent 的接收目标。

可直接在原生 `/cron-jobs` 页面创建任务：先让自己的微信/QQ 会话通过桥接完成一次对话，在同一个受管 Agent 的任务页面选择已登记的 `astrbot` 频道、用户和 `ab_...` 会话。原生表单支持这些候选目标，也提供 share session、tool safety 和 dispatch mode 字段。**固定 2.2.1 的新建表单默认 `mode=stream`、`tool_safety=false`**；需明确改成 `final`、开启工具安全和共享会话，允许发送时关闭 silent。不要用 Console 会话 ID 代替微信/QQ 桥接会话。

以下是参考 job body，不会自动创建任务。`POST /api/agents/{agent_id}/cron/jobs` 才会保存它：

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

外层仍含真实 `agent_id`、`session_id`、`user_id`、`delivery_id`。Agent ID 来自运行时或频道所属工作区，不接受模型用参数切换身份。每个工具调用只生成一次 delivery ID，同次 HTTP 重试保持完全相同请求；网关持久去重表示它已受理，不能冒充微信 / QQ 的送达确认。重新调用工具属于新发送，不能在超时后反复调用来猜测送达情况。

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
- [原生 Console 页面路由](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/console/src/layouts/registry/builtinRoutes.tsx)
- [原生 Cron 表单与目标候选](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/console/src/pages/Control/CronJobs/components/JobDrawer.tsx)
- [原生 Cron 新建默认值](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/console/src/pages/Control/CronJobs/components/constants.ts)
- [两层认证的真实实现](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/auth.py)

协议测试使用 QwenPaw / AstrBot API 轻量 stub；综合测试另启本机 HTTP 服务，串通聊天 SSE、可信上下文、工具回调、鉴权、去重、主动消息和审批查询。个人身份与记忆 API 测试覆盖不同平台／机器人账号、群成员隔离、绑定拒绝与重放、持久化和重启。文件中间件另以官方 AgentScope 实类型核验拒绝契约；结构化浏览器使用真实 Chromium 和离线本机页面验证读取、点击、填写、Cookie 隔离与重定向拒绝。

[历史完整运行时验证](qwenpaw-runtime.md) 曾使用未修改官方 2.2.1、真实插件 loader、AgentScope/ReMe 和本机模拟模型验证底层协议；[历史原生浏览器验证](browser-validation.md) 记录的是官方 SDK。它们不等于本次个人 Agent 版本已经通过真实模型或微信／QQ 账号验收。当前仍需配置真实模型，验证自动记忆提炼、跨会话召回与主动任务实际送达；通过本地代码测试也不表示现有 Docker／服务器已自动升级。
