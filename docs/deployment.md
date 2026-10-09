# 部署 AstrBot × QwenPaw

这个仓库增加两端桥接插件和部署材料，不替换 AstrBot 或 QwenPaw 的源码。部署有两种模式：已有 AstrBot 加装，以及全新安装。准备脚本只创建新目录、私密密钥和 NapCat 网络配置；不会安装软件、启动容器、购买配置升级、改域名或修改现有网站。

## 版本和资源

| 组件 | 固定版本 | 获取方式 |
|---|---|---|
| AstrBot | 4.25.1 | `soulter/astrbot:v4.25.1` |
| QwenPaw | 2.2.1 | 官方 Git 提交 `cae5773707b26ab2fd00903f84b712387894b256` 的 `deploy/Dockerfile` 构建 |
| NapCat | 4.18.33 | `mlikiowa/napcat-docker:v4.18.33` |
| 本地控制台代理 | Caddy 2.11.7 | `caddy:2.11.7-alpine`（[官方镜像清单](https://raw.githubusercontent.com/docker-library/official-images/master/library/caddy)） |

QwenPaw 使用本地构建标签 `bot-combined/qwenpaw:2.2.1-local`；这不是声称存在一个上游 `v2.2.1` 镜像。官方 Dockerfile 安装 Chromium、Playwright 使用的系统浏览器、Xvfb、Xfce 和中文字体，保留完整浏览器/桌面运行依赖。固定应用提交不等于锁定所有系统包；验证后应记录本地镜像 ID/上游 RepoDigest，并保留构建产物用于复现。[官方源文件](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/deploy/Dockerfile)。

使用 Linux amd64/arm64、Docker Engine、Compose v2 或更新版本、Python 3 和足够磁盘空间。估计至少 4 GB 内存可作为起点，8 GB 给浏览器和同机其他服务更多余量；这是容量建议，尚未实测。2 GB 可以准备配置，但建议扩容后才启动整套服务。镜像构建会用到外网和额外磁盘空间，预留 8 GB 以上可用空间。新 `.env` 的 `*_MEMORY` 和 `*_SWAP` 可按实测修改；`memswap_limit` 是 RAM 加 swap 的总量，必须不小于对应 RAM 限额。

先运行只读检查：

```bash
python3 deploy/preflight.py --disk-path /opt
```

管理页面只绑定服务器 `127.0.0.1`：AstrBot 6185、QwenPaw 控制台代理 8088、NapCat 6099。机器人协议端口 6199、桥接网关 9186 均无宿主映射。Docker 网络保留出网，用于 QQ、模型 API 和浏览器访问互联网；不能设置 `internal: true` 断掉这些功能。

## A：已有 AstrBot 加装

适用于 `/opt/bot` 已有 `compose.yaml`、容器名 `astrbot-weixin` 的安装。把本仓库放到 `/opt/bot/combined/source`；后面的命令在这个源码目录执行。

1. 运行准备脚本：

   ```bash
   bash deploy/upgrade_existing.sh /opt/bot/combined /opt/bot
   python3 deploy/preflight.py --existing-container astrbot-weixin --disk-path /opt
   ```

   新文件只在 `/opt/bot/combined`。原 `/opt/bot/data`、原 `.env`、原 `compose.yaml` 不会被写入。生成的 `.env` 权限为 0600，三个随机密钥重跑时保留。不要把该文件内容发到聊天或提交 Git。

2. 在新 `.env` 中设定所有者和工具白名单，见下方配置步骤。然后建立新专用网络（已有该名称时直接复用，经 `docker network inspect` 确认）：

   ```bash
   docker network inspect bot-combined-network
   ```

   如果返回“network not found”，执行：

   ```bash
   docker network create bot-combined-network
   ```

3. 先确认原 AstrBot 的 Compose 项目名：

   ```bash
   docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' astrbot-weixin
   ```

   以下例子使用原项目名 `astrbot-weixin`。若输出不同，请更换 `-p` 后的值。必须显式使用原项目名，因为新 `.env` 中的 `COMPOSE_PROJECT_NAME` 属于 add-on，不能用于管理原容器。先检查合并后的 Compose，命令只校验，不启动：

   ```bash
   docker compose -p astrbot-weixin --project-directory /opt/bot --env-file /opt/bot/.env --env-file /opt/bot/combined/.env -f /opt/bot/compose.yaml -f deploy/compose.astrbot.override.yaml config --quiet
   ```

   校验通过后保存原 Compose 和 `.env` 的私密备份，确认原 Compose 的 project name 和 service name（本示例 service 是 `astrbot`）。以下这一步由你明确执行：它会重建一次该 AstrBot 容器，使新增环境变量、插件挂载和专用网络生效；已有数据挂载和网站网络继续由原 Compose 提供。

   ```bash
   docker compose -p astrbot-weixin --project-directory /opt/bot --env-file /opt/bot/.env --env-file /opt/bot/combined/.env -f /opt/bot/compose.yaml -f deploy/compose.astrbot.override.yaml up -d --no-deps astrbot
   ```

   以后重建已有 AstrBot 时也要带这个 override。只运行原 Compose 会移除新增插件/网络连接。

4. 构建并启动新增服务，里面没有第二个 AstrBot：

   ```bash
   docker compose --env-file /opt/bot/combined/.env -f deploy/compose.addon.yaml config --quiet
   docker compose --env-file /opt/bot/combined/.env -f deploy/compose.addon.yaml build qwenpaw
   docker compose --env-file /opt/bot/combined/.env -f deploy/compose.addon.yaml up -d
   ```

原 `bot` 域名和网站服务不需要改变。新管理页面通过服务器端口转发访问。若要为 QwenPaw/NapCat 增设公网管理域名，应另行设置 HTTPS、访问认证和受限代理网络；本仓库不会自动改动现有 Caddy。

## B：全新安装

适用于没有任何 AstrBot 数据的新目录。把仓库放到 `/opt/bot-combined/source`，切换到该源码目录：

```bash
bash deploy/setup.sh /opt/bot-combined
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml config --quiet
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml build qwenpaw
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml up -d
```

也可将全新根目录设成 `/opt/bot`；如果已有 `data`、`compose.yaml` 或部署回执，脚本会停止并要求选 A 模式。新安装不会携带任何作者的微信登录态、QQ 账号、模型密钥、联系人或聊天记录，需要你在 AstrBot 中添加微信/QQ 和模型服务。

## 两端初次配置

1. 在 AstrBot 确认桥接插件加载。通过自己的微信/QQ 私聊发送 `/paw whoami`，取得自己的平台用户 ID；将它填入新私密 `.env` 的 `OWNER_USER_IDS`，多个 ID 逗号分隔。保持文件 0600，不把用户标识写进公共仓库。按所选部署模式的 AstrBot 命令重新应用环境变量。
2. `TOOL_ALLOWLIST` 初始为空，禁止由 QwenPaw 调用 AstrBot 插件工具。先选择需要的具体工具名称；`*` 是显式开放全部工具，不是默认值。普通插件指令仍由 AstrBot 自己处理。QwenPaw 使用自己的工作区；`/bridge-files` 是单独共享的导出目录，不挂载其他项目或整个宿主数据。
3. QwenPaw 初次启动会通过官方入口初始化自己的空数据目录。默认 agent ID 是 `default`，不是 `main`。在控制台配置有效的模型提供商和模型；不要把 AstrBot 密钥自动抄到另一服务。
4. QwenPaw 会自动发现 `astrbot-bridge` 插件，无需给插件编造 `enabled` 开关。在 `default` agent 的 Channels 中启用 `astrbot`。插件已只读挂载到 `/app/working/plugins/qwenpaw_plugin_astrbot_bridge`；回调地址 `http://astrbot:9186` 和回调密钥由环境变量提供。需要手动编辑时，仅合并 `/app/working/workspaces/default/agent.json` 中 `channels.astrbot = {"enabled": true}`，保留所有其他字段；不要整份替换未知 `agent.json`。Native tool 使用环境变量 `ASTRBOT_BRIDGE_URL` 和 `BRIDGE_TOKEN`。
5. 在 AstrBot 中发送普通文字先验证自己的请求，再验证浏览器任务和插件工具。当前桥接发送文字；图片、音频、文件等产物需到 QwenPaw 控制台下载。`/bridge-files` 已预留，但不能把它当成已完成的附件传输。微信和 QQ 会话各自隔离；不同账号的 ID 不应随意合并。
6. 主动任务通过 QwenPaw 原生 Cron/Heartbeat 配置，对应渠道选 `astrbot` 和已经登记的会话。每个 Cron 的 `runtime.tool_safety` 保持 `true`；它是任务的字段，不是可以随意塞进 `agent.json` 的开关。遇到敏感工具审批应等待所有者批准，不配置自动放行。

QwenPaw 的 `QWENPAW_RUNTIME_INTERNAL_TOKEN` 保护全部 HTTP/WebSocket 路径。控制台代理只在 loopback 注入这个头，避免把密钥写进浏览器 URL。它不是给公网使用的无认证入口。若开启 QwenPaw 额外用户登录（`QWENPAW_AUTH_ENABLED=true`），桥接调用还需要该用户的 API 凭据；请按桥接插件支持情况配置，不要以关闭 runtime token 解决 401。

## 查看状态和保留数据

按选定模式将 `compose.yaml` 换成 `compose.addon.yaml`，`.env` 路径也对应更换：

```bash
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml ps
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml logs --tail 60 qwenpaw
docker stats --no-stream bot-qwenpaw bot-napcat
```

NapCat 日志可能含登录 token/二维码；在自己终端查看，发送截图前遮住这些内容。QwenPaw 数据、密钥、备份分别保存，三个运行组件只共享 `state/bridge-files`。停服务不会删除 state；不要使用 `down -v` 或删除数据目录。

已有安装回退：停止新增 add-on 服务，再用原项目的 Compose 和原 `.env` 显式重建原 AstrBot。这会移除新增挂载/网络，保留原数据。不得改用这个仓库的 fresh Compose 管理原 AstrBot。

运行验证前应确认：微信/QQ 各收到一次回应、普通插件指令可用、QwenPaw 任务能回到原会话、非所有者无法操作、空工具白名单被拒绝、文件路径越界被拒绝、需要审批的动作不会自行通过。仓库静态测试不代替这些真实平台测试。
