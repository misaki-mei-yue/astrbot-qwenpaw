# Windows 本地使用

在 Docker Desktop 中运行 AstrBot、QwenPaw 和 NapCat，统一从 **http://localhost:18080/** 打开。三套原生后台分别保留自己的登录与配置。

## 准备

- Docker Desktop，切换到 **Linux 容器**，选择本机 Docker context。
- Python **3.10+**，可通过 Python launcher 或 PATH 调用。
- 首次下载镜像所需的网络和磁盘空间。镜像、浏览器和运行数据会持续占用空间，请提前查看 Docker Desktop 的数据盘位置。

将完整仓库下载或克隆到普通文件夹，不要放在盘符根目录、符号链接或目录联接里。希望减少系统盘占用时，将项目放到其他数据盘，并分别调整 Docker Desktop 的数据盘；只移动项目文件夹不会移动 Docker 的镜像与缓存。

## 第一次启动

1. 打开 Docker Desktop，等待引擎就绪。
2. 双击项目根目录的 **`start.cmd`**。
3. 等启动器检查完成并打开浏览器，或自行访问 **http://localhost:18080/**。

启动器检查 Docker 与 Python，准备私密运行目录，校验 Compose，下载缺少的固定版本镜像，构建轻量网关并启动本项目。它会等待原生页面和 QwenPaw 默认 Agent 就绪，再检查缺失的 AstrBot 回传频道配置。已有配置及明确关闭的频道保留。

这不是离线包，也不包含已登录账号。启动成功后仍需完成下面的模型和平台设置。

## 第一次进入三个页面

| 页面 | 要做的事 |
|---|---|
| 聊天与插件 / AstrBot | 默认用户名 `astrbot`。在 Docker Desktop 对应项目的 AstrBot 日志中查看最新启动的 `Initial password:`，登录后设置自己的密码。配置微信平台与桥接权限。 |
| 记忆与任务 / QwenPaw | 在 Models 中配置自己的模型提供商和实际使用的模型，并为默认 Agent 选定模型。本地模式默认没有额外的 QwenPaw 用户登录。 |
| QQ 登录 / NapCat | 在本机打开 `runtime/state/napcat/config/webui.json`，将 `token` 填入原生登录页，然后用手机 QQ 扫码。全新配置已准备内部反向 WebSocket。 |

NapCat 网页 token、AstrBot 网页密码和 `runtime/.env` 中的程序间通信令牌是不同凭据。只在自己电脑查看，不要提交仓库、发到 issue 或贴出带二维码的日志。

在微信或 QQ 与机器人私聊，发送 **`/paw whoami`**，将返回的用户 ID 加入 AstrBot 桥接插件的允许列表。模型工具默认不放行，按需设置具体工具名称。详细说明见 [QQ 接入](qq.md)、[QwenPaw 配置](qwenpaw.md)和[个人记忆](personal-memory.md)。

先给模板 Agent 配好模型，再开始个人聊天。已经创建的个人 Agent 有自己的模型设置；修改模板不会自动覆盖所有个人 Agent。

## 启动与停止

以后双击 `start.cmd` 启动，双击 **`stop.cmd`** 停止当前项目。停止保留数据，不关闭其他 Docker 项目或 Docker Desktop。

也可以在项目目录运行：

```powershell
.\start.ps1
.\stop.ps1
```

PowerShell 策略阻止直接执行时使用 `.cmd`。它仅为当前进程传递执行策略参数，不修改系统策略。自动化启动可用 `start.cmd -NoBrowser`。

运行目录为项目下的 **`runtime`**，包括私有 `.env`、账号、聊天、个人工作区和浏览器相关资料。开始使用后不要在运行中移动或改名项目目录，Compose 项目标识与其路径关联。搬动或升级前先停止，并备份原目录及数据；不要直接把另一个包的 `runtime` 覆盖进来。

## 官方镜像与可选源码构建

默认使用 AstrBot、NapCat 外部固定版本镜像，以及固定摘要的官方 QwenPaw 2.2.1 镜像：

```text
agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/qwenpaw@sha256:4127130c41f415434aca5a9ea8eada99d3185d99e2bf181bd95c6e7fb959a3a7
```

只有轻量 `gateway` 默认本地构建。镜像下载失败会停止，不会自动替换成未知镜像或开始完整源码构建。

需要自己构建固定提交的 QwenPaw 时，显式运行：

```powershell
.\start.cmd -BuildQwenPaw
```

此选项使用 `deploy/compose.qwenpaw.source.yaml`，保留同一运行目录与端口，但构建时间和缓存占用可能明显增加。以后继续使用源码镜像时仍需加此选项；普通启动恢复官方镜像。它不构建或修改 NapCat 源码。各组件许可见[第三方说明](../THIRD_PARTY_NOTICES.md)。

## 一个地址如何工作

网关仅发布 **`127.0.0.1:18080`**。入口中三套后台使用同端口的 `astrbot.localhost`、`qwenpaw.localhost` 和 `napcat.localhost`，保留原生 API 路径并隔离浏览器登录存储。Chrome / Edge 通常直接将 `.localhost` 解析到本机，无需修改 hosts。

需要弹窗、浏览器授权或独立窗口时，点击“独立打开”。账号仍分别管理，这不是统一登录。QwenPaw 的结构化浏览器运行在容器内，不接管电脑上已有登录状态的 Chrome / Edge。

此入口用于本机。不要将网关直接改成公网监听；服务器访问方式见[部署说明](deployment.md)。

## 启动失败怎么检查

先看启动器显示的失败阶段，再检查 Docker Desktop 的对应项目与容器日志。缺少 Python、引擎未就绪、Windows 容器模式、远程 context、配置无效或未知运行目录都会让启动停止。

修好环境后可重跑 `start.cmd`。不要删除 `runtime`、清空数据库或执行 `down -v` 来试错。日志可能含密钥、二维码和聊天，提交问题前先脱敏。

页面健康检查只说明页面和模板 Agent 可访问，不检查模型额度、QQ／微信扫码、实际回复和每个插件。按[验收步骤](validation.md)逐项测试。
