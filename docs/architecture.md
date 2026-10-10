# 桥接结构与接口

本机入口将三套原生后台放在一起管理，实际能力互通由 AstrBot 插件和 QwenPaw 插件完成：微信／QQ 消息进入 AstrBot，由身份记录选择独立 QwenPaw Agent，Agent 使用自己的记忆和受限工具，最终结果回到原来的 AstrBot 会话。入口就绪不等于模型、长期记忆效果或平台登录已经验收。

## 本机组合入口

`deploy/compose.bundle.yaml` 将三套原程序与轻量网关作为同一 Compose 项目启动，只发布 `127.0.0.1:18080`。从 `http://localhost:18080/` 进入；原生后台使用同端口的 `astrbot.localhost`、`qwenpaw.localhost`、`napcat.localhost`，保留各自 API、资源路径及浏览器登录存储，没有统一登录。

网关代理 HTTP、上传下载、SSE 和 WebSocket。QwenPaw runtime token 只在网关服务端注入；先移除客户端提供的 runtime token、Hub 可信头及转发头。只接收指定本机 Host 和同源 API 调用，不跟随携带内部令牌的 HTTP／WebSocket 跳转，不集中保存各用户上游 Cookie。嵌入页面只调整 frame-ancestors／X-Frame-Options，其余 CSP 保留。

就绪检查覆盖页面及模板 Agent，不证明实际模型、微信／QQ 登录或插件工具可用。首次使用见 [Windows 说明](windows.md)。服务器模式沿用独立部署配置。

## 个人身份与会话

| 层次 | 标识与持久数据 | 边界 |
|---|---|---|
| 平台账号 | 平台类型、机器人连接 ID、发送者 ID | 相同字符串 ID 跨平台或跨机器人不自动合并 |
| 个人身份 | 随机 person ID、`abp_<32位hex>` Agent | 同账号多次私聊共享个人工作区；跨平台需显式绑定 |
| 群内范围 | 账号与群 origin 对应的 `abg_<32位hex>` Agent | 与私聊、其他群、同群其他成员分开 |
| 聊天会话 | 随机 `ab_<32位hex>`、origin、账号、Agent、scope | 上下文和附件归属；新会话不重建个人记忆 |
| 活跃轮次 | 一次性随机 `turn_id` 与原始 AstrBot event | 只有当前未过期轮次可以调用 AstrBot 插件工具 |

身份、路由、退役路由和收据保存在 AstrBot 持久 SQLite；QwenPaw 的个人工作区、记忆文件和索引保存在其持久卷。`QWENPAW_AGENT_ID` 是配置模板，默认 `default`，不再承载所有人的私聊记忆。新 Agent 只继承必要模型配置并采用允许的技能名单，不复制模板的聊天、记忆与资料。每次准备个人操作都验证管理标记和工作区配置，失败时停止，不切回共享 Agent。

账号绑定只在私聊进行。源账号生成 10 分钟有效的高熵绑定码，数据库仅存哈希；目标账号兑换成功后共用源个人身份。两边均需通过 owner 授权。目标已有独立工作区／记忆或已有其他账号绑定时拒绝静默合并，旧数据保留。绑定不合并群工作区。`/paw new` 更换当前 session，保留个人 Agent 与长期记忆；旧会话主动任务需在原生后台重新选择目标。

显式备注通过受管 Agent 的工作区 API 写入 `digest/bridge-note-*.md`，使用签名验证归属后才能更正或删除，并请求刷新索引。原生 watcher 异步摄入文件，即使重建接口返回 completed 也不等于新文件已经被摄入；CRUD 返回 `index_status=pending`，刷新请求异常时保留成功写入的备注编号并附 `index_warning`。无 embedding 配置时只请求 BM25 重建。删除备注不会删除历史聊天或另外提炼的自动记忆。自动提炼和召回由每个 Agent 自己的 ReMe 管理；代码和模拟 API 的测试不能证明实际模型已经学会用户偏好。

## 内部桥接接口

AstrBot 适配器在私网监听 `9186`；所有端点要求 `Authorization: Bearer <BRIDGE_TOKEN>`，没有宿主公网端口映射。

| 端点 | 请求字段 | 用途 |
|---|---|---|
| `GET /health` | 无 | 适配器初始化状态 |
| `POST /v1/tools/list` | `agent_id, session_id, user_id, turn_id` | 当前原始 AstrBot 事件可用且放行的工具 |
| `POST /v1/tools/call` | 上述身份字段及 `call_id, tool_name, arguments` | 调用原插件模型工具 |
| `POST /v1/deliver` | `agent_id, session_id, user_id, delivery_id, content` | 原路由主动发送文本与附件 |

QwenPaw 插件从真实运行上下文获取 Agent、频道、用户及根会话，再向 AstrBot 回传。模型参数不暴露身份选择字段。网关检查 Agent 与活动路由、owner 和平台配置一致；固定文本 Cron 没有请求 ContextVar 时，出站频道使用官方注入的所属 Workspace Agent，而不是猜测 default Agent。

每次聊天还生成新的 `turn_id`，通过原生 `request_context` 交给 PRE_DISPATCH hook，保存在任务 ContextVar，由桥接工具自动附带；FINALLY 清理。数据库中有 session 不意味着可以伪造原 event，旧轮次 nonce 不能使用新消息的事件。主动任务可沿持久路由发送结果，但不能取得已过期 event 来调用插件工具。

## 五个工具与工作区边界

| 工具 | 授权来源 |
|---|---|
| `astrbot_list_tools` | 实际 Agent／频道／用户／根会话和活跃轮次 nonce |
| `astrbot_call_tool` | 同上，加 AstrBot 原插件权限与允许列表 |
| `astrbot_media_workspace` | 实际 Agent／频道／用户／根会话 |
| `astrbot_send_file` | 同上，加当前会话 outbound 文件验证 |
| `astrbot_browser` | 原生上下文中的受管 Agent 和 AstrBot 频道 |

AstrBot 工具执行沿用 `FunctionToolExecutor`、`AstrAgentContext`、工具 hooks、生成器及直接发送行为。返回 `direct_message_sent` 表示工具已自行发送。对话按 Agent 串行，绑定后的两端私聊使用同一把锁；工具回调用独立锁，避免模型等待工具时阻塞自身。

QwenPaw 通过官方 `PluginApi.register_middleware` 注册 AgentScope `on_acting` 中间件。它只保护 `abp_`／`abg_` 受管 Agent，对其聊天、Console 和 Cron 请求都生效，不依赖 nonce；独立 default 等 Agent 保持原有行为。原生读写、编辑、追加、搜索、查看媒体和发送文件的参数先验证并转成绝对路径，仅可访问该 Agent 的普通工作文件和当前合法 `ab_` 附件目录。

运行配置、凭据、策略、插件及技能加载目录不可通过这些工具读写。父目录越界、相邻 Agent、其他会话、符号链接、目录连接、硬链接及特殊文件拒绝；递归搜索也检查子树，包含受保护内容时需缩小范围。原生 Python 浏览器、shell、批量调用、跨 Agent 委派、`web_fetch` 和未经审核的新工具拒绝。`memory_search` 使用本工作区原生记忆管理器，不接受其他 Agent／目录参数；`web_search` 只允许提供商查询字段。

结构化 `astrbot_browser` 仅暴露 open／read／click／fill／status／close。每个 Agent 使用独立临时浏览器上下文与 Cookie；没有模型代码执行、指定 profile 或文件上传接口。网页请求和重定向检查公开 HTTP(S) 目标，阻止 WebSocket，关闭弹窗并取消下载。已用真实 Chromium 在无网络测试容器中验证本机网页交互和不同用户 Cookie 隔离。

上述是工具调用边界，不是 OS 沙箱，不保证抵抗恶意宿主进程并发换链接、任意已安装 Python 插件或浏览器 DNS 重绑定。严格部署仍需独立权限及网络出站策略，不能据此授予宿主机根目录或任意执行权限。

## 附件

`content` 支持文本及 `image/file/video/audio` 媒体描述符，例如：

```json
{
  "type": "file",
  "path": "ab_0123456789abcdef0123456789abcdef/outbound/report.pdf",
  "filename": "report.pdf",
  "size": 1234,
  "sha256": "<64位十六进制内容哈希>"
}
```

路径相对于 `/bridge-files`，必须属于已鉴权 session 的 `outbound`。每条消息最多 4 个附件、每文件最多 20MiB，可收紧配置。网关检查普通文件、每层路径、链接数、大小及 SHA-256，再复制到同会话发送快照；不接受远程 URL、目录、跨会话或根外文件。见[媒体说明](media.md)。

入站附件位于同会话 `inbound`，以 QwenPaw `image_url/video_url/file_url` 或音频 `data` 字段传入，不使用公共 Console 上传目录。主动任务需要 `share_session=true` 才能继续使用原会话附件。原生 `send_file_to_user` 的最终媒体输出也必须通过这些检查，不会自动把整个个人工作区公开给平台。

## QwenPaw 原生 API 与主动任务

适配版本为 QwenPaw `2.2.1`，源码 commit `cae5773707b26ab2fd00903f84b712387894b256`。

聊天使用 `POST /api/agents/{实际个人或群Agent}/console/chat`，携带 `X-QwenPaw-Runtime-Token`，body 包含 `input, session_id, user_id, channel: "astrbot"` 和可信轮次上下文。SSE 只发送完整 assistant 最终文字；明确完成的原生 `send_file_to_user` 媒体输出另经边界检查。reasoning、工具诊断及进度不转发，最终 response 与此前 message 去重。断线仅尝试一次空 input 的 `reconnect: true` 接续已有任务，不重投用户动作。

主动任务在同一个受管 Agent 中创建，并从该 Agent 的候选列表选择原 AstrBot 会话。使用 `dispatch.mode=final`、`runtime.share_session=true`、`runtime.tool_safety=true`；需要实际发送时 `silent=false`。发送结果必须同时匹配任务所属 Agent、session、user，不能在默认 Agent 里填写别人的 session 来投递。Cron 缺少活跃原 event，因此 `astrbot_list_tools`／`astrbot_call_tool` 不可借用历史 nonce；其受限文件、浏览器与显式附件发送仍按真实上下文工作。

审批查询 `GET /api/approval/list` 后按根 session 和所属 Agent 核对持久路由。用户用 `/paw approve` 或 `/paw deny` 明确选择，调用原生批准／拒绝 API；同意范围为 `exact`，不自动批准。详细任务与认证配置见 [QwenPaw 说明](qwenpaw.md)。

## 去重与验收边界

SQLite 保存调用、对话及主动回传收据。开始执行即占用幂等键，完成后保存结果；进程中途退出时保留不确定状态，避免重新执行可能已有副作用的动作。`accepted: true`／`submitted_to_adapter` 仅表示 AstrBot 已提交平台适配器，不是微信或 QQ 的收件确认，也不是跨平台严格 exactly-once 保证。

当前个人身份、记忆 API、回调归属与工作区边界已由自动测试覆盖，结构化浏览器完成真实 Chromium 离线测试。真实模型的自动提炼、召回质量和微信／QQ 收发、定时送达仍需配置后的人工联调；代码完成或容器就绪不能替代这些验收。
