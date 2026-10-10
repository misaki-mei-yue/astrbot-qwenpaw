# Windows 本地组合包

这套包在 Docker Desktop 中运行 AstrBot、QwenPaw 和 NapCat，统一从 **http://localhost:18080/** 打开。入口连接两边原生后台和 QQ 登录页；各程序的账号、配置页面和能力保留。它不是三个已经登录、配置好模型的账号副本。

## 第一次使用

1. 自行安装并打开 Docker Desktop，使用 **Linux 容器**；再安装 Python 3.10 或更新版本，安装时启用 Python launcher 或加入 PATH。脚本不会替你安装软件、购买服务器或导入已有账号。
2. 把完整组合包解压到自己的普通文件夹，避免符号链接和目录联接。留出镜像、浏览器和数据所需的磁盘空间；首次下载需要联网。希望减少 C 盘占用时，将包放到 E 盘，并在首次拉取前确认 Docker Desktop 的 Linux 数据盘也在 E 盘：程序数据写入包目录的 `runtime`，镜像与构建缓存由 Docker 数据盘位置决定。
3. 双击根目录的 **start.cmd**。它检查本机 Docker 和 Python，准备私密配置，校验组合配置，拉取缺少的应用镜像，再构建小型入口服务并启动。QwenPaw 默认直接使用已核对源码提交的官方固定镜像，无需在本机安装几百个系统包。等 QwenPaw 默认 Agent 就绪后，启动器通过官方接口检查桥接频道：首次空配置会启用，已有配置保留，包括你主动关闭的状态。脚本与密钥不会放入进程参数。Docker Desktop 尚未运行时，只会启动已安装的程序并等待，不修改全局执行策略。
4. 网关和三个程序均可访问后，脚本才打开浏览器。首次仍需配置模型、扫码登录微信／QQ、设置允许使用的用户与工具；具体操作见[部署说明](deployment.md)和 [QQ 接入说明](qq.md)。就绪检查不代替模型请求或真实平台收发验收。

也可以在包目录的 PowerShell 中运行：

```powershell
.\start.ps1
.\stop.ps1
```

若当前 PowerShell 策略阻止直接运行，双击 `.cmd`；它只对这一次 PowerShell 进程使用执行策略参数。不会改变系统设置。自动化时可运行 `.\start.ps1 -NoBrowser`。

## 第一次进入三个页面

- **聊天与插件 / AstrBot**：用户名默认为 `astrbot`。4.25.1 使用随机初始密码；在 Docker Desktop 打开本目录对应项目的 AstrBot 日志，查找最新一次启动的 `Initial password:`。只在自己电脑查看，登录后亲自在原生页面设置新密码。
- **记忆与任务 / QwenPaw**：本地模式默认没有额外账号登录。进入 Models 配置自己的模型，再选择 Default Agent 使用的模型。首次启动会自动准备 AstrBot 回传频道；已有关闭状态保持不变。
- **QQ 登录 / NapCat**：用记事本打开包内 `runtime/state/napcat/config/webui.json`，将 `token` 字段复制到登录框，然后用手机 QQ 扫码。这个登录密码与 `runtime/.env` 中的 `ONEBOT_TOKEN` 不同。全新包已预配 QQ 的内部连接，已有配置不会覆盖，详情见 [QQ 说明](qq.md)。

这些密码、令牌、二维码和日志只在本机查看，不要贴到聊天或上传仓库。微信在 AstrBot 原生机器人页面按其方式登录；微信和 QQ 登录后仍需在桥接插件中设置允许使用的用户。

## 官方镜像与可选源码构建

本地组合包默认使用官方阿里云 ACR 的 QwenPaw 2.2.1 镜像，固定摘要为 `sha256:4127130c41f415434aca5a9ea8eada99d3185d99e2bf181bd95c6e7fb959a3a7`。已匿名核对 ACR 和 Docker Hub 清单，两者摘要相同；amd64 镜像的 SLSA 构建证明中，`vcs:revision` 是项目适配的 `cae5773707b26ab2fd00903f84b712387894b256`。[官方发布](https://github.com/agentscope-ai/QwenPaw/releases/tag/v2.2.1)、[官方 Docker Hub 标签元数据](https://hub.docker.com/v2/repositories/agentscope/qwenpaw/tags/v2.2.1)、[本项目来源核对记录](qwenpaw-image-validation.json)。完整原生 Console、浏览器与桌面依赖保留，amd64 压缩层合计约 993 MB，实际下载速度取决于网络。

启动器先运行 `pull --ignore-buildable --policy missing`，缺失应用镜像才拉取；默认只构建 `gateway`。[Docker 的拉取参数](https://docs.docker.com/reference/cli/docker/compose/pull/)。官方镜像不可达时会明确停止，不会自动改用未知版本或偷偷启动长时间源码构建。

需要自己构建同一固定源码时，显式运行：

```powershell
.\start.cmd -BuildQwenPaw
```

此选项增加 `deploy/compose.qwenpaw.source.yaml` 覆盖，并构建固定提交的官方 Dockerfile；可能耗时较久、占用较多构建缓存。仍使用同一 `runtime` 和项目，数据挂载、端口与权限不变，`stop.cmd` 仍可停止它。以后继续使用源码镜像时每次加此选项；普通 `start.cmd` 会恢复官方固定镜像。服务器的现有安装 / add-on 配置未随本地组合包改动。

## 日常开启与停止

以后继续双击 **start.cmd**，访问地址保持不变。脚本重用 `runtime` 中的配置与数据；`prepare.py` 不重置已有密钥或 NapCat 配置。双击 **stop.cmd** 只停止当前包对应的 Compose 项目，保留数据、登录状态和密钥，不停止 Docker Desktop 或其他项目。

项目名由包目录生成。开始使用后不要在服务运行时移动或改名整个包；若需搬动，先停止，再明确处理数据和原容器。不要把 `runtime/.env`、数据库、登录二维码、聊天记录或浏览器资料提交 Git 或发到聊天中。

## 启动失败

失败时脚本显示停止原因，不会声称已经就绪，也不会自动清理容器或删数据。Windows 容器模式、远程 Docker context、缺少 Python、未知的已有 `runtime` 数据和配置校验失败都会停止流程。当前包只管理本机 Docker Desktop；需要远程部署时按[服务器部署文档](deployment.md)处理。

打开 Docker Desktop 检查引擎和对应项目状态。环境问题修好后可重跑 `start.cmd`；请勿用删除 `runtime` 或执行 `down -v` 来解决启动问题。模型与 QQ／微信的首次配置仍由你完成。

Windows Docker Desktop 已实际启动三套原生应用和统一入口；本机回归还验证了文件、记忆、任务、Linux Chromium 和配置保留。真实账号、模型与平台收发仍待配置，具体范围见[验证记录](validation.md)。

## 一个地址的实现与边界

你从 `http://localhost:18080/` 进入，在标签页里切换三套原生后台。Docker 仅向本机发布这一个端口。内部页面分别使用 `astrbot.localhost`、`qwenpaw.localhost`、`napcat.localhost` 的同一端口，由网关转发到各程序；Chrome / Edge 将这些 `.localhost` 名称解析到本机，不需要改 hosts 或配置域名。这样保留原来的 API 路径，并隔离原生后台的登录存储。不是三套账号的统一登录。

遇到浏览器权限、OAuth 弹窗或通行密钥限制时，可以点「独立打开」进入当前原生页面。录音和通行密钥已声明 iframe 权限委派，但仍需浏览器与用户授权，未经过真实账号验收。此入口只用于本机，不应把网关端口改成公网监听。

QwenPaw 使用 Docker 内的 guest 浏览器。操控电脑上已登录 Chrome 的扩展 / Native Messaging 接入尚未包含在本启动包中；无需额外浏览器桌面端口。原生 Cron 新建任务时，按 [QwenPaw 配置](qwenpaw.md#主动任务) 选择 `final` 并开启工具安全和共享会话。

网关的页面就绪检查仅检查原生 HTML 可访问及 QwenPaw 默认 Agent 已加载，未检查模型密钥、平台扫码、插件加载和实际消息送达。完整连接仍按部署说明逐项测试。
