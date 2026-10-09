# 桥接接口

AstrBot 适配器在内网监听 `9186`；所有端点包含 `Authorization: Bearer <BRIDGE_TOKEN>`。没有宿主公网端口映射。

| 端点 | 请求 | 用途 |
| --- | --- | --- |
| `GET /health` | 无 | 适配器初始化状态 |
| `POST /v1/tools/list` | `session_id, user_id, turn_id` | 当前真实 AstrBot 事件可用且放行的工具 |
| `POST /v1/tools/call` | `session_id, user_id, turn_id, call_id, tool_name, arguments` | 调用原插件模型工具 |
| `POST /v1/deliver` | `session_id, user_id, delivery_id, content` | 原路由主动发送文本 |

`session_id` 是桥接生成并保存的随机 `ab_` 标识，映射 `origin + user_id`；平台和群聊用户分别隔离。QwenPaw 本地插件读取其运行时的真实会话 / 用户 / 频道上下文，再向 AstrBot 回传身份。模型工具参数不暴露会话身份字段。

每次消息还生成独立 `turn_id`。它通过原生 `request_context` 交给 QwenPaw 生命周期 hook，保存在该任务的 ContextVar 中，由本地工具自动回传。网关核对当前事件对应的轮次；旧任务的迟到调用不能借用下一条消息的事件。子任务只有保留真实 AstrBot 频道和同一轮次时才可使用这些工具。

`content` 的首版格式为 `[{"type":"text","text":"消息"}]`。接口不下载任意附件 URL，也不接受客户端指定 AstrBot 数据路径。

## 工具和事件

工具列表遵循激活状态、当前会话插件范围和集成配置的 allowlist。执行采用 AstrBot 的 `FunctionToolExecutor` 和原 `AstrAgentContext`，保留工具 hooks、生成器与直接发送行为。工具可能直接发消息，返回值包含 `direct_message_sent`。

会话对话按顺序处理。工具调用使用独立锁，避免模型等待工具时与对话锁互相阻塞。消息事件过期后返回 `no_active_astrbot_event`，不能把数据库中的路由当成可伪造的插件事件。

## QwenPaw API

适配固定 QwenPaw `v2.2.1`，commit `cae5773707b26ab2fd00903f84b712387894b256`。

聊天：`POST /api/agents/{agent_id}/console/chat`，默认 agent 为 `default`，body 含 `input`、`session_id`、`user_id`、`channel: "astrbot"`。使用 `X-QwenPaw-Runtime-Token` 通过其 runtime boundary。

解析 SSE 的完整 assistant message 与最终 response；忽略 reasoning、工具输出和进度增量。最终 response 快照不会与之前的 message 重复拼接。断线最多尝试一次 `reconnect: true`、空 input 来接续已有任务，不重新投递任务。

审批从 `GET /api/approval/list` 获取，使用根会话标识并核对保存的用户路由。确认 / 拒绝通过 `/api/approval/approve` 和 `/api/approval/deny`，同意范围为 `exact`。不自动批准。

## 去重与送达边界

SQLite 保存工具调用和主动回传收据。开始执行即占用幂等键，完成后保存结果供重试返回。若进程在执行中退出，键保持不确定状态，需要人检查；不会冒险重新执行可能已有副作用的动作。

`accepted: true` / `submitted_to_adapter` 表示 AstrBot 找到平台并提交发送。微信、QQ 的实际送达还取决于登录状态、适配器以及平台限制。本项目不声称实现跨平台的严格 exactly-once 送达。
