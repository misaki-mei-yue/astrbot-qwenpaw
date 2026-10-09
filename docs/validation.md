# 验证记录

日期：2026-10-09。当前版本：0.3.0，组合工作台实验版。

## 0.3.0 本地回归

- Python 3.10.2：169 项，164 通过，5 项因 Windows 符号链接权限或 POSIX FIFO 条件跳过。
- Python 3.12.14 独立环境：169 项，167 通过，仅 2 项 POSIX FIFO 条件跳过。
- 新增 20 项工作台检查实际使用 Quart 请求处理与 aiohttp 本机 HTTP：登录保护、跨站请求、请求 / 响应大小限制、禁重定向、错误脱敏、目标智能体就绪、会话归属、并发创建去重、超时不重发、记忆路径及成员校验、工具字段过滤。
- 新增 15 项只读诊断检查，使用受控 Docker 输出验证故障和脱敏边界，尚未在真实 Docker daemon 上执行该诊断。
- 另用 Playwright 运行实际页面，检查桌面与 390px 手机视口、创建任务、暂停 / 恢复、创建失败不重复提交、记忆按纯文本显示、工具搜索及独立打开时的提示。UI 上游数据为模拟数据，未登录平台。复现方法见[工作台说明](workbench.md)。

下面的固定官方运行检查独立记录，不混入单元测试总数。GitHub Actions 包含 Linux Python 3.10 / 3.12 回归与 Chromium 页面检查，按实际提交查看运行结果。

## 0.2.0 历史基线

- Windows 本机 Python 3.10.2：共运行 132 项测试，127 通过，5 项因符号链接权限或 POSIX FIFO 条件跳过。
- Windows 本机 Python 3.12.14：共运行同样的 132 项测试，130 通过，仅 2 项 POSIX FIFO 测试跳过；符号链接检查实际执行通过。
- 其中 10 项使用真实本机 HTTP 连接双方适配器：SSE 聊天、工具调用、主动回传、持久去重、鉴权 / 权限拒绝、原会话审批，以及图片 / 纯文件消息的字节往返、原生发送文件输出、主动附件、篡改与跨会话拒绝。
- 媒体检查覆盖大小 / 数量上限、链接与路径越界、读取中修改、发送副本、DNS 地址约束、HTTPS 与重定向、下载流限制、音频平台差异及真实媒体后缀。
- 上述 132 项协议测试中的 AstrBot / QwenPaw 运行时由接口替身提供；QwenPaw 固定版本的插件注册、channel、runtime hook 和请求上下文接口已对照官方源码核实。
- Docker Compose CLI 实际解析通过：全新部署、追加部署、原 AstrBot 加 override；测试确认原数据挂载与原网络保留。
- 两个安装准备脚本通过 Bash 语法检查。准备脚本不会启动服务。

另用固定上游真实模块执行了最小媒体格式验证：AgentScope 2.0.7.post1 的消息 / 事件 / ToolChunk、QwenPaw 2.2.1 的 schema / renderer / Envelope，串到本项目的 AssistantCollector。4 次 ToolChunk JSON 往返、8 个 renderer 样例、4 次原生 Content 往返、4 条真实 Envelope → Collector 链路通过，覆盖图片、文件、视频与音频。普通文档在原生 Envelope 中使用 `type=data`，已经据此修正桥接解析。

这项检查绕过了应用包的初始化导入，实际参与测试的类型和转换函数保持上游原样。它没有运行完整应用、模型或浏览器。复现脚本为 [check_upstream_media.py](../scripts/check_upstream_media.py)，参数说明见[部署文档](deployment.md)；[检查报告](upstream-media-validation.json)记录固定版本、源文件与构件哈希。

GitHub 持续运行 Linux Python 3.10 / 3.12 检查，最新结果按对应提交查看 [Actions](https://github.com/misaki-mei-yue/astrbot-qwenpaw/actions)。0.2.0 的检查已通过；本节 132 项记录保留为上版历史基线。

协议测试不会调用模型、收发真实微信 / QQ，也不会执行上游完整容器。

## 组合工作台的真实 AstrBot 运行验证

随后在独立 Python 3.12.14 虚拟环境中，使用 AstrBot 4.25.1 的固定 Git 提交 `d609f23b7136e30adf38929bac6402a581148ffc` 运行了 **15 项真实运行检查**。源码从该提交的 `git archive` 提取；脚本校验整个 `astrbot/` 框架树哈希，拒绝只保留相同 VERSION 的较新或已修改源码。每次生成新的数据根目录和测试身份，原 AstrBot 的配置、数据库、密钥与登录状态均未读取。实际执行上游核心生命周期、PluginManager、命令过滤器、消息流水线、FunctionToolExecutor 与工具 hooks、原生消息和 Context 主动发送；普通插件指令、`/paw whoami`、桥接单次回复、消息 / 工具重放去重、非所有者拒绝和原路由主动回传通过。

工作台还经过真实 AstrBot Dashboard 的 Quart 测试客户端验证：7 个原生插件 API 注册成功，未登录访问返回 401、有效后台 JWT 可读取状态，跨站修改返回 403；Plugin Pages 自动发现工作台，页面 HTML 和官方 SDK 资源可访问，资源 token 不能授权管理 API；终止插件后工作台路由被移除。这验证服务端加载与登录保护，未用真实浏览器渲染页面。

平台收发和 QwenPaw HTTP 响应是本机测试边界；没有登录真实 QQ / 微信、调用真实模型或浏览器，也没有运行生产容器。验证进程清除代理环境变量并将连接、DNS 和监听地址限制在 loopback；文转图端点与模型元数据刷新不访问外网。测试环境使用 MCP 1.30.0，并记录实际关键依赖版本，不能据此断言任意未锁定依赖环境都兼容。

可复现脚本为 [check_astrbot_runtime.py](../scripts/check_astrbot_runtime.py)，需要另行准备完整的 AstrBot 运行依赖；常规协议测试的轻量依赖不足以运行它。示例：

```bash
mkdir -p /tmp/astrbot-v4.25.1
git -C /path/to/clean/AstrBot archive d609f23b7136e30adf38929bac6402a581148ffc | tar -x -C /tmp/astrbot-v4.25.1
python scripts/check_astrbot_runtime.py --astrbot-source /tmp/astrbot-v4.25.1 --report /tmp/astrbot-runtime-report.json
```

[运行报告](astrbot-runtime-validation.json)只保留版本、检查项、关键依赖和参与执行的上游源文件哈希，不包含账号、密钥、绝对个人路径或测试目录。上述 132 项是上版历史基线，本节 15 项独立运行检查不计入该协议测试数量。

### 官方镜像依赖的实际核对

本次没有启动 Docker 镜像，不能把本机虚拟环境的包版本当作官方镜像结果。AstrBot 核心直接依赖 aiohttp；测试使用的 MCP 1.30.0 元数据还声明 `jsonschema>=4.20.0`，因此正常 MCP 1.x 安装通常已带 jsonschema，但镜像实际版本和是否满足桥接的 `jsonschema>=4.23,<5` 仍应核对。在自己的运行容器里可只读取包版本并校验，不加载 AstrBot 配置或连接平台：

```bash
docker exec astrbot-weixin python -c 'import sys; sys.path.append("/AstrBot/data/site-packages"); import importlib.metadata as m; from packaging.specifiers import SpecifierSet; import aiohttp, jsonschema; expected={"aiohttp": ">=3.10,<4", "jsonschema": ">=4.23,<5"}; versions={name:m.version(name) for name in expected}; print(versions); assert all(SpecifierSet(rule).contains(versions[name]) for name,rule in expected.items())'
```

全新部署将容器名换为 `bot-astrbot`。缺包或版本不满足时该命令失败，应先检查镜像与插件依赖，再决定安装方案；本项目没有据推测重装核心依赖。上游 `pip_installer` 仅在 packaged desktop 模式自动使用 `data/site-packages`，普通 Linux 容器的自动补装默认进入容器系统环境，随重建消失；这只影响实际发生补装的包。原迁移安装脚本单独采用持久 `data/site-packages`，不代表所有未来插件自动继承该安装目标。

## 仍需完成

- 构建并启动固定版本上游容器，配置真实模型。
- 微信、QQ 登录后的真实收发；当前使用的第三方插件实际兼容性。
- QwenPaw 真实记忆、文件、浏览器与定时任务联调。
- 服务器完整负载的内存 / CPU / 磁盘记录，以及网络中断、重启、平台发送失败验证。
- 图片、音频、文件和视频的真实平台收发、格式兼容性及资源占用验收。

本记录只证明以上本地协议、配置和限定范围的真实运行检查结果，不表示服务器已完成部署或所有目标功能已验收。
