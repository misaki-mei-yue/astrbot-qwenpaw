# 验证记录

日期：2026-10-10。当前版本：0.4.0 开发版，Windows 组合启动包与原生后台统一入口。

## 0.4.0 本地组合包

- Windows Python 3.10.2 完整回归：177 项，172 通过，5 项因 Windows 符号链接权限或 POSIX FIFO 条件跳过。包含新增 12 项网关检查、15 项 Windows 启动器检查和 1 项测试模块隔离回归。
- 12 项网关检查使用真实 loopback HTTP / WebSocket，覆盖三套来源路由、请求与登录头隔离、Cookie 不跨用户共用、压缩资源、上传、流式响应、WebSocket 文本 / 二进制 / 子协议、跳转令牌保护、Host / Origin 拒绝及就绪判定。
- 15 项启动器检查实际调用 PowerShell，由受控替身模拟 Docker / Python / 浏览器；验证参数与路径、首次 / 重复启动、故障即停、数据保留、项目范围和就绪判断，不代表容器已构建启动。
- 真实 Chromium 访问实际网关，验证单个入口切换、不同来源的登录存储、表单内容保留、POST、iframe CSP、录音权限声明、下载、独立窗口、手机宽度与键盘操作。三套上游页面均明确标记为测试替身；没有扫码、使用真实原生后台或录音。
- Docker Compose CLI 已实际解析本地组合配置；只向宿主 loopback 发布一个端口，没有固定容器 / 网络名称。Python 脚本语法检查通过。
- GitHub Actions 增加 Windows 启动器与 Linux Chromium 入口检查，保留 Linux Python 3.10 / 3.12 回归；以对应提交的 [Actions 结果](https://github.com/misaki-mei-yue/astrbot-qwenpaw/actions) 为准。

本机 Docker Desktop 在创建运行端点时自行退出，引擎未就绪；因此本轮没有构建或启动完整的三套原生容器。没有删除 Docker 数据、镜像或用户配置，也没有改动服务器。组合包是首次联网获取 / 构建镜像的启动包，不是包含镜像和已登录账号的离线包。

复现：`python -m unittest discover -s tests -v`；浏览器检查为 `node scripts/check_bundle_ui.cjs`，需要 Playwright / Chromium 与开发依赖。详情见 [Windows 使用说明](windows.md)。

## 0.3.1 本地回归

- Python 3.10.2：149 项，144 通过，5 项因 Windows 符号链接权限或 POSIX FIFO 条件跳过。
- 0.3.1 撤掉重复工作台及其 20 项专用测试；消息、工具、权限、审批、媒体和部署回归继续保留。
- 新增 15 项只读诊断检查，使用受控 Docker 输出验证故障和脱敏边界，尚未在真实 Docker daemon 上执行该诊断。

下面的固定官方运行检查独立记录，不混入单元测试总数。GitHub Actions 包含 Linux Python 3.10 / 3.12 回归、检查脚本语法和 Compose 解析，按实际提交查看运行结果。之前 0.3.0 的 169 项回归和自建页面检查已通过，但不作为当前原生后台的界面验收。

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

## 桥接的真实 AstrBot 运行验证

在独立 Python 3.12.14 虚拟环境中，使用 AstrBot 4.25.1 的固定 Git 提交 `d609f23b7136e30adf38929bac6402a581148ffc` 运行了 **10 项真实运行检查**，实际加载桥接插件 0.3.1。源码从该提交的 `git archive` 提取；脚本校验整个 `astrbot/` 框架树哈希，拒绝只保留相同 VERSION 的较新或已修改源码。每次生成新的数据根目录和测试身份，原 AstrBot 的配置、数据库、密钥与登录状态均未读取。实际执行上游核心生命周期、PluginManager、命令过滤器、消息流水线、FunctionToolExecutor 与工具 hooks、原生消息和 Context 主动发送；普通插件指令、`/paw whoami`、桥接单次回复、消息 / 工具重放去重、非所有者拒绝和原路由主动回传通过。

插件终止后，实际网关、客户端、审批任务、状态库和锁完成清理。原生后台不再被本项目新增管理路由；本轮不包含两端原生后台的浏览器界面验收。

平台收发和 QwenPaw HTTP 响应是本机测试边界；没有登录真实 QQ / 微信、调用真实模型或浏览器，也没有运行生产容器。验证进程清除代理环境变量并将连接、DNS 和监听地址限制在 loopback；文转图端点与模型元数据刷新不访问外网。测试环境使用 MCP 1.30.0，并记录实际关键依赖版本，不能据此断言任意未锁定依赖环境都兼容。

可复现脚本为 [check_astrbot_runtime.py](../scripts/check_astrbot_runtime.py)，需要另行准备完整的 AstrBot 运行依赖；常规协议测试的轻量依赖不足以运行它。示例：

```bash
mkdir -p /tmp/astrbot-v4.25.1
git -C /path/to/clean/AstrBot archive d609f23b7136e30adf38929bac6402a581148ffc | tar -x -C /tmp/astrbot-v4.25.1
python scripts/check_astrbot_runtime.py --astrbot-source /tmp/astrbot-v4.25.1 --report /tmp/astrbot-runtime-report.json
```

[运行报告](astrbot-runtime-validation.json)只保留版本、检查项、关键依赖和参与执行的上游源文件哈希，不包含账号、密钥、绝对个人路径或测试目录。本节 10 项独立运行检查不计入协议测试数量。

### 官方镜像依赖的实际核对

本次没有启动 Docker 镜像，不能把本机虚拟环境的包版本当作官方镜像结果。AstrBot 核心直接依赖 aiohttp；测试使用的 MCP 1.30.0 元数据还声明 `jsonschema>=4.20.0`，因此正常 MCP 1.x 安装通常已带 jsonschema，但镜像实际版本和是否满足桥接的 `jsonschema>=4.23,<5` 仍应核对。在自己的运行容器里可只读取包版本并校验，不加载 AstrBot 配置或连接平台：

```bash
docker exec astrbot-weixin python -c 'import sys; sys.path.append("/AstrBot/data/site-packages"); import importlib.metadata as m; from packaging.specifiers import SpecifierSet; import aiohttp, jsonschema; expected={"aiohttp": ">=3.10,<4", "jsonschema": ">=4.23,<5"}; versions={name:m.version(name) for name in expected}; print(versions); assert all(SpecifierSet(rule).contains(versions[name]) for name,rule in expected.items())'
```

全新部署将容器名换为 `bot-astrbot`。缺包或版本不满足时该命令失败，应先检查镜像与插件依赖，再决定安装方案；本项目没有据推测重装核心依赖。上游 `pip_installer` 仅在 packaged desktop 模式自动使用 `data/site-packages`，普通 Linux 容器的自动补装默认进入容器系统环境，随重建消失；这只影响实际发生补装的包。原迁移安装脚本单独采用持久 `data/site-packages`，不代表所有未来插件自动继承该安装目标。

## QwenPaw 完整运行时验证

独立 Python 3.12.14 环境运行官方 QwenPaw 2.2.1、AgentScope 和 ReMe，安装包内 940 个 Python 源文件与固定提交 `cae5773707b26ab2fd00903f84b712387894b256` 一致。实际加载本项目插件，通过真实 `QwenPawClient` 和 `AssistantCollector` 完成带可信会话身份的工具往返；原生文件写入和读取成功。

通过官方记忆文件 API 保存测试标记后，ReMe 文件监听器实际建立 BM25 索引，Agent 调用原生 `memory_search` 检索到该标记。真正的 Cron 任务通过官方接口手动触发执行，历史记录为 success、最终结果只回传一次，暂停 / 恢复成功；没有等待日历时间验证到点触发。停止再重启后，记忆文件、检索、Cron、会话和禁用工具设置保留。

模型响应与 AstrBot 回调由 loopback 测试服务器提供，本轮 16 次本机模型请求、0 次付费模型调用。没有验证真实模型推理、自动记忆提炼质量、向量检索效果或微信 / QQ 送达。复现步骤与边界见[完整运行说明](qwenpaw-runtime.md)，实际版本、源码哈希与结果见[脱敏报告](qwenpaw-runtime-validation.json)。

## QwenPaw 的真实浏览器运行验证

在独立的完整 QwenPaw 2.2.1 环境中，使用已有 Windows Edge 的全新 guest / incognito 上下文，真实 Browser SDK 与工具子进程 worker 均完成打开空白页、读取本机 HTML、点击按钮并再次读取变化。9 项断言全部通过，执行前核对 10 个固定官方浏览器源文件的 SHA256。没有使用用户现有浏览器资料或账号，没有访问外网网页，也未调用模型。

这证明 Windows 浏览器路径的真实启动和交互，不替代 Linux Chromium 容器验证、模型自主规划浏览器任务或外部网站兼容性。可复现脚本为 [check_qwenpaw_browser.py](../scripts/check_qwenpaw_browser.py)，[验证说明](browser-validation.md)记录参数与边界，[运行报告](qwenpaw-browser-validation.json)记录版本和实际检查结果。

## 仍需完成

- 构建并启动固定版本上游容器，配置真实模型。
- 微信、QQ 登录后的真实收发；当前使用的第三方插件实际兼容性。
- 原生后台的实际访问与登录入口；真实模型驱动的自动记忆提炼、文件、Linux 浏览器和定时到点执行。
- 服务器完整负载的内存 / CPU / 磁盘记录，以及网络中断、重启、平台发送失败验证。
- 图片、音频、文件和视频的真实平台收发、格式兼容性及资源占用验收。

本记录只证明以上本地协议、配置和限定范围的真实运行检查结果，不表示服务器已完成部署或所有目标功能已验收。
