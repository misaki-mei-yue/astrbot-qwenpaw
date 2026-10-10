# Windows 本地组合包

这套包在 Docker Desktop 中运行 AstrBot、QwenPaw 和 NapCat，统一从 **http://localhost:18080/** 打开。入口连接两边原生后台和 QQ 登录页；各程序的账号、配置页面和能力保留。它不是三个已经登录、配置好模型的账号副本。

## 第一次使用

1. 自行安装并打开 Docker Desktop，使用 **Linux 容器**；再安装 Python 3.10 或更新版本，安装时启用 Python launcher 或加入 PATH。脚本不会替你安装软件、购买服务器或导入已有账号。
2. 把完整组合包解压到自己的普通文件夹，避免符号链接和目录联接。留出镜像、浏览器和数据所需的磁盘空间；首次构建需要联网，可能较久。
3. 双击根目录的 **start.cmd**。它检查本机 Docker 和 Python，准备私密配置，校验组合配置，构建镜像并启动。Docker Desktop 尚未运行时，只会启动已安装的程序并等待，不修改全局执行策略。
4. 网关和三个程序均可访问后，脚本才打开浏览器。首次仍需配置模型、扫码登录微信／QQ、设置允许使用的用户与工具；具体操作见[部署说明](deployment.md)和 [QQ 接入说明](qq.md)。就绪检查不代替模型请求或真实平台收发验收。

也可以在包目录的 PowerShell 中运行：

```powershell
.\start.ps1
.\stop.ps1
```

若当前 PowerShell 策略阻止直接运行，双击 `.cmd`；它只对这一次 PowerShell 进程使用执行策略参数。不会改变系统设置。自动化时可运行 `.\start.ps1 -NoBrowser`。

## 日常开启与停止

以后继续双击 **start.cmd**，访问地址保持不变。脚本重用 `runtime` 中的配置与数据；`prepare.py` 不重置已有密钥或 NapCat 配置。双击 **stop.cmd** 只停止当前包对应的 Compose 项目，保留数据、登录状态和密钥，不停止 Docker Desktop 或其他项目。

项目名由包目录生成。开始使用后不要在服务运行时移动或改名整个包；若需搬动，先停止，再明确处理数据和原容器。不要把 `runtime/.env`、数据库、登录二维码、聊天记录或浏览器资料提交 Git 或发到聊天中。

## 启动失败

失败时脚本显示停止原因，不会声称已经就绪，也不会自动清理容器或删数据。Windows 容器模式、远程 Docker context、缺少 Python、未知的已有 `runtime` 数据和配置校验失败都会停止流程。当前包只管理本机 Docker Desktop；需要远程部署时按[服务器部署文档](deployment.md)处理。

打开 Docker Desktop 检查引擎和对应项目状态。环境问题修好后可重跑 `start.cmd`；请勿用删除 `runtime` 或执行 `down -v` 来解决启动问题。模型与 QQ／微信的首次配置仍由你完成。

本轮启动流程由模拟进程测试验证失败边界、项目隔离、参数传递、数据保留和就绪判断；尚不能据此声称整套 Windows 镜像已经构建运行、真实账号已经登录或收发成功。

## 一个地址的实现与边界

你从 `http://localhost:18080/` 进入，在标签页里切换三套原生后台。Docker 仅向本机发布这一个端口。内部页面分别使用 `astrbot.localhost`、`qwenpaw.localhost`、`napcat.localhost` 的同一端口，由网关转发到各程序；Chrome / Edge 将这些 `.localhost` 名称解析到本机，不需要改 hosts 或配置域名。这样保留原来的 API 路径，并隔离原生后台的登录存储。不是三套账号的统一登录。

遇到浏览器权限、OAuth 弹窗或通行密钥限制时，可以点「独立打开」进入当前原生页面。录音和通行密钥已声明 iframe 权限委派，但仍需浏览器与用户授权，未经过真实账号验收。此入口只用于本机，不应把网关端口改成公网监听。

QwenPaw 使用 Docker 内的 guest 浏览器。操控电脑上已登录 Chrome 的扩展 / Native Messaging 接入尚未包含在本启动包中；无需额外浏览器桌面端口。原生 Cron 新建任务时，按 [QwenPaw 配置](qwenpaw.md#主动任务) 选择 `final` 并开启工具安全和共享会话。

网关的页面就绪检查仅检查原生 HTML 可访问及 QwenPaw 默认 Agent 已加载，未检查模型密钥、平台扫码、插件加载和实际消息送达。完整连接仍按部署说明逐项测试。
