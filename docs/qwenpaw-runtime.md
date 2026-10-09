# QwenPaw 真运行时验证

本项目提供 `scripts/check_qwenpaw_runtime.py`，运行完整官方 QwenPaw、AgentScope 和 ReMe，并加载本项目的 QwenPaw 插件。模型与 AstrBot 回调服务器是本机测试边界；Agent、原生文件工具、记忆索引、运行钩子、Cron 和本项目 `QwenPawClient` / `AssistantCollector` 使用实际实现。

本次使用 QwenPaw **2.2.1**，官方源码提交 **`cae5773707b26ab2fd00903f84b712387894b256`**。安装包内 940 个 Python 源文件与该提交逐字节比对；树的 SHA-256 为 **`c52273e4f2d3afc817b3252bb315406f75515311c254e9a994530a9ceaef9503`**。哈希依次包含按相对路径排序的 Python 文件名、零字节、文件内容、零字节，不包含缓存文件。

## 复现

先在单独的 Python 3.12 环境安装固定上游基础包。本脚本不安装依赖，不读取现有 QwenPaw 配置，也不启动用户现有 AstrBot。以下命令以 Linux 为例；Windows 使用独立环境的 `Scripts/python.exe`。

```sh
python3.12 -m venv /tmp/qwenpaw-runtime-check-env
/tmp/qwenpaw-runtime-check-env/bin/python -m pip install \
  'git+https://github.com/agentscope-ai/QwenPaw.git@cae5773707b26ab2fd00903f84b712387894b256'
/tmp/qwenpaw-runtime-check-env/bin/python scripts/check_qwenpaw_runtime.py \
  --artifacts-dir /tmp/qwenpaw-runtime-check-evidence
```

`--artifacts-dir` 必须是不存在的新目录；脚本会留下日志、原始 SSE、测试配置和报告，不删除已有目录。省略此参数时自动创建临时目录。可追加 `--upstream-source /path/to/QwenPaw`，要求该只读 Git checkout 的 HEAD 等于固定提交，并验证安装包的全部 Python 源文件与它一致。没有此参数时只验证发行版本，报告会明确 `source_verified=false`。

脚本生成随机内部令牌，绑定随机 localhost 端口，启动完全独立的工作区和密钥目录，关闭系统 keyring 读取，并阻止 QwenPaw 测试进程连接或解析非 loopback 地址。模型响应由本机 OpenAI 协议测试服务器产生。测试中没有付费模型调用、微信/QQ 登录或对用户现有文件的操作。出现文件审批时，测试脚本只允许精确匹配自己创建的临时测试文件；这段测试逻辑不进入产品插件。

## 验证范围

脚本依次检查：

1. 官方 CLI 初始化默认 Agent；真实插件发现、channel/tool/runtime-hook 注册与 Agent 启动。
2. `/api/healthz`、插件状态、频道健康、四个桥接工具、记忆文件列表与运行状态、模型配置状态。
3. 本地模型配置后，真实 `QwenPawClient.chat` 两次工具往返：可信会话、用户和 turn nonce 到达回调，`AssistantCollector` 只返回最终文本。
4. QwenPaw Agent 实际调用原生 `write_file` 和 `read_file`；在独立工作区写入再读取文件。官方 `.txt` 写入使用 UTF-8 BOM，测试按它的实际编码读取。
5. 通过官方记忆文件 API 保存测试内容，由真实 ReMe 文件监听器建立 BM25 索引，再让 Agent 调用实际 `memory_search`，检索到文件名和唯一测试标记。
6. 保存真正的 `CronJobSpec`，通过官方运行接口手动触发执行，设置 `channel=astrbot`、`mode=final`、`share_session=true`、`tool_safety=true`、`max_concurrency=1`、`timeout_seconds=900`。最终回复只调用一次频道回传，执行历史为 success，pause/resume 返回实际结果；未等待日历时间验证自动到点触发。
7. 关闭本脚本启动的 QwenPaw 进程，再从同一测试状态重启；记忆文件、ReMe 检索、Cron、会话文件和用户禁用工具的设置均须保留。

默认未配置 embedding 时，ReMe 使用 BM25/关键词索引。这里验证的是实际文件保存、索引、检索和重启持久性；不将它描述成向量检索效果测试。测试关闭自动摘要与夜间 dream 任务以避免不受控的额外任务，因此没有验证自动提炼质量。浏览器的真实 SDK/worker 验证另见 [浏览器验证](browser-validation.md)。真实平台送达仍需服务器上联调。

脱敏实际结果保存在 [qwenpaw-runtime-validation.json](qwenpaw-runtime-validation.json)。原始日志与 SSE 只保留在运行者指定的本地测试目录，公开报告不包含令牌、真实账号或个人路径。

## 启动与 API 的实际行为

- 初次冷启动会导入官方渠道 SDK。本机完整环境约需 30 秒，`/api/healthz` 在默认 Agent 还未加载时返回 503；不要把已监听 HTTP 或 `/api/version` 当作已完成 Agent 启动。
- 没有模型配置也可以启动默认 Agent 与 ReMe；`/api/models/active?scope=effective&agent_id=default` 返回 `active_llm=null`。配置模型只证明设置已保存；对话测试才验证实际调用。
- `/api/agents/default/tools` 实际列出本插件四个工具，均默认启用。官方插件注册会保存工具配置并保留已经存在的 disabled 设置；不需要猜测性修改 manifest。
- `/api/agents/default/workspace/memory?section=daily|digest` 返回文件数组；`/api/agents/default/memory/runtime-status` 返回实际 worker、auto_memory、tasks、recent 和索引状态。
- 记忆文件读取 API 会剥除首尾空白；测试比较归一化内容，不假定它是原始字节读取。

官方来源：[固定源码](https://github.com/agentscope-ai/QwenPaw/tree/cae5773707b26ab2fd00903f84b712387894b256)、[插件注册](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/plugins/api.py)、[原生文件工具](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/agents/tools/file_io.py)、[真实 ReMe 后端](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/agents/memory/reme_light_memory_manager.py)、[Cron 执行](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/app/crons/executor.py)。
