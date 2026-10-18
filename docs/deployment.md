# Linux 与已有 AstrBot 部署

本仓库提供 Compose 配置和文件准备脚本。**Linux 流程需要手动检查并执行构建、启动命令**；`setup.sh` / `upgrade_existing.sh` 只准备目录、私密配置与初始 NapCat 网络设置，不安装软件、不启动容器、不修改域名或现有服务。

Windows 本地体验优先使用[双击启动方式](windows.md)。下面的 Linux 配置与 Windows 统一入口不同，分别提供原生后台的回环端口。

## 版本、依赖与端口

需要 Docker Engine、Compose v2、Python 3 与联网能力。模板固定 AstrBot 4.25.1、QwenPaw 2.2.1、NapCat 4.18.33。服务器的 QwenPaw 使用固定提交 `cae5773707b26ab2fd00903f84b712387894b256` 的官方 Dockerfile 构建；它包含浏览器和桌面依赖，首次构建可能耗时较久。

模板的管理入口只绑定本机：

| 入口 | 宿主地址 |
|---|---|
| AstrBot，全新安装 | `127.0.0.1:6185` |
| QwenPaw Console 代理 | `127.0.0.1:8088` |
| NapCat WebUI | `127.0.0.1:6099` |

桥接 `9186` 与 OneBot `6199` 不发布到宿主公网。QwenPaw 的 `console` 代理为原生页面补内部认证头，不应将内部令牌写进浏览器 URL。

浏览器、长任务与并发会增加内存和磁盘占用。先运行只读容量检查，并按实际负载调整生成 `.env` 的 `*_MEMORY`、`*_SWAP`。`memswap_limit` 是 RAM 加 swap 总量，不能小于 RAM 限额。预留至少数 GB 的镜像与构建空间，并为后续数据留出余量。

```bash
python3 deploy/preflight.py --disk-path /opt
```

以下路径均为示例。在仓库源码目录执行命令；准备配置后不要随意移动源码，因为生成文件会记录它的绝对路径。私密 `.env` 保持 `0600`，不要提交 Git。

## A：全新安装

选择一个没有已有 AstrBot 数据的专用目录，例如 `/opt/astrbot-qwenpaw`：

```bash
bash deploy/setup.sh /opt/astrbot-qwenpaw
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml config --quiet
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml build qwenpaw
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml up -d
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml ps
```

也可直接使用同一准备 CLI：

```bash
python3 deploy/prepare.py --mode fresh --root /opt/astrbot-qwenpaw
```

准备脚本生成独立随机通信令牌和新 QQ 内部连接。已有非空令牌与已有配置不会自动重置。如果检测到已有 AstrBot 安装特征，会停止，不能用全新安装模板覆盖旧数据。

按下方“初次配置”完成模型、平台与权限设置。`up -d` 返回不表示模型和真实聊天已经可用。

## B：已有 AstrBot 加装

此模式只新增 QwenPaw、NapCat 与 Console，原 AstrBot 使用自己的 Compose 和数据。需要先备份原配置，确认以下实际值：

- 原安装目录与 `compose.yaml`。
- 原 Compose **项目名**、AstrBot **服务名**和**容器名**，三者可能不同。
- 原数据挂载、网络与已使用的端口。

示例用原安装目录 `/opt/assistant-existing`，新增目录 `/opt/assistant-integration`：

```bash
bash deploy/upgrade_existing.sh /opt/assistant-integration /opt/assistant-existing
```

对应的直接 CLI 为：

```bash
python3 deploy/prepare.py --mode existing --root /opt/assistant-integration --existing-dir /opt/assistant-existing
```

准备步骤不改原 Compose、原 `.env` 或数据。检查新增 `.env` 的实际容器／项目值、owner 与工具允许列表；不要使用准备文件的示例默认值来猜测旧项目。

模板 `deploy/compose.astrbot.override.yaml` 的服务键是 **`astrbot`**。只有原服务也叫这个名称时才能直接合并；否则先制作自己的 override 副本，将该键改成原来的服务名，保留其余原配置。不要用本仓库的全新安装 Compose 来管理旧 AstrBot。

默认专用网络是 `bot-combined-network`，需与新增 `.env` 的 `BRIDGE_NETWORK` 一致：

```bash
docker network inspect bot-combined-network
```

仅在确认不存在时创建：

```bash
docker network create bot-combined-network
```

之后显式使用**原项目名**合并配置。以下 `ORIGINAL_PROJECT` 需替换成已核实的值，示例假设原服务名为 `astrbot`，且原安装有 `.env`：

```bash
docker compose -p ORIGINAL_PROJECT --project-directory /opt/assistant-existing --env-file /opt/assistant-existing/.env --env-file /opt/assistant-integration/.env -f /opt/assistant-existing/compose.yaml -f deploy/compose.astrbot.override.yaml config --quiet
```

审查通过并保存原配置备份后，下面的命令会重建一次原 AstrBot，应用插件挂载、环境变量和新增网络：

```bash
docker compose -p ORIGINAL_PROJECT --project-directory /opt/assistant-existing --env-file /opt/assistant-existing/.env --env-file /opt/assistant-integration/.env -f /opt/assistant-existing/compose.yaml -f deploy/compose.astrbot.override.yaml up -d --no-deps astrbot
docker compose --env-file /opt/assistant-integration/.env -f deploy/compose.addon.yaml config --quiet
docker compose --env-file /opt/assistant-integration/.env -f deploy/compose.addon.yaml build qwenpaw
docker compose --env-file /opt/assistant-integration/.env -f deploy/compose.addon.yaml up -d
```

以后重建原 AstrBot 时仍需带上同一 override，否则新增插件和网络会移除。已有 OneBot 配置需按 [QQ 说明](qq.md)检查与 NapCat 连接匹配；加装不会替你覆盖原平台配置。

## 初次配置

1. 登录 AstrBot 原生后台，确认桥接插件已加载。全新安装的默认用户名为 `astrbot`，随机初始密码在该容器启动日志中；立即设置自己的密码。
2. 在 QwenPaw 配置模型并为模板 `default` 选择有效模型。在 Channels 中启用 `astrbot`；内部回调地址与令牌由环境变量提供。不要整份替换已有 `agent.json`。详见 [QwenPaw 说明](qwenpaw.md)。
3. 在 NapCat WebUI 输入本机生成的 WebUI token 并扫码 QQ；微信在 AstrBot 原生后台配置。WebUI token 与 `ONEBOT_TOKEN` 不同。
4. 在已连接的私聊中发送 `/paw whoami`，把用户 ID 加入 AstrBot 桥接插件允许列表；或在私密 `.env` 中配置非空 `OWNER_USER_IDS`。空环境值使用后台设置，非空值优先。
5. `TOOL_ALLOWLIST` 默认空。按需只放行确定要调用的 AstrBot 模型工具；普通插件命令不依赖这张工具列表。
6. 先测试普通对话，再测试记忆、附件、浏览器与一条需审核的任务。任务使用 `astrbot` 频道和 `/paw status` 显示的接收会话，选择 `final`，开启工具安全与共享会话。详见[个人助手说明](personal-memory.md)。

模板的 `QWENPAW_AUTH_ENABLED=false` 适用于回环地址、隧道或受认证外层代理。内部 runtime token 仍然启用。本版桥接尚未处理 QwenPaw 原生用户登录的额外 Bearer token，不能仅改为 `true` 就认为整套配置已完成。

## 原生后台的访问与登录

服务器的 localhost 不是自己电脑的 localhost。可用 SSH 隧道或管理工具的端口转发访问；以下是 SSH 示例，替换用户名与服务器地址：

```bash
ssh -N -L 6185:127.0.0.1:6185 -L 8088:127.0.0.1:8088 -L 6099:127.0.0.1:6099 user@server
```

保持隧道后，在本机打开 `http://127.0.0.1:6185/`、`http://127.0.0.1:8088/` 和 `http://127.0.0.1:6099/webui/`。已有 AstrBot 可继续使用原管理入口。

网页终端本身不会自动建立这些隧道。需要公网管理域名时另行配置 HTTPS、访问认证和受限代理网络，不要通过改成公网监听来代替认证。本仓库不修改现有域名或反向代理。

## 状态、日志与数据

全新模式示例：

```bash
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml ps
docker compose --env-file /opt/astrbot-qwenpaw/.env -f deploy/compose.yaml logs --tail 60 qwenpaw
python3 deploy/doctor.py --mode fresh
```

加装模式改用新增 `.env` 和 `compose.addon.yaml`。诊断容器名与模板不一致时，用 `doctor.py --help` 中的 `--astrbot-container` 等参数明确指定；该工具只读，不调用模型或发送平台消息。

日志可能含口令、二维码、聊天与路径，提交问题前先脱敏。只读健康检查不能证明平台已登录或消息已送达。

运行数据保存在选定安装根目录的 `state`；三个程序只共享专用 `state/bridge-files`，不要增加宿主根目录或整个用户资料目录的挂载。默认附件每文件最多 20 MiB、每次 4 个，可收紧。文件和平台差异见[媒体说明](media.md)。

停服务不删除数据。升级前备份专用数据、配置及镜像信息；不要用 `down -v`、删除数据库或关闭工具守卫排错。当前不提供自动升级回滚。已有安装回退时，停止新增服务，再用原项目的原 Compose 和原 `.env` 重建原 AstrBot，恢复其原挂载与网络。
