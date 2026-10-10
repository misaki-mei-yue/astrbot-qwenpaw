# 部署 AstrBot × QwenPaw

这个仓库增加两端桥接插件和部署材料，不替换 AstrBot 或 QwenPaw 的源码。部署有两种模式：已有 AstrBot 加装，以及全新安装。准备脚本只创建新目录、私密密钥和 NapCat 网络配置；不会安装软件、启动容器、购买配置升级、改域名或修改现有网站。

## 版本和资源

| 组件 | 固定版本 | 获取方式 |
|---|---|---|
| AstrBot | 4.25.1 | `soulter/astrbot:v4.25.1` |
| QwenPaw | 2.2.1 | 本地组合包使用官方 ACR 固定摘要；服务器模式保留固定 Git 提交的源码构建 |
| NapCat | 4.18.33 | `mlikiowa/napcat-docker:v4.18.33` |
| 本地控制台代理 | Caddy 2.11.7 | `caddy:2.11.7-alpine`（[官方镜像清单](https://raw.githubusercontent.com/docker-library/official-images/master/library/caddy)） |

Windows 本地 `compose.bundle.yaml` 默认使用官方镜像 `agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/qwenpaw@sha256:4127130c41f415434aca5a9ea8eada99d3185d99e2bf181bd95c6e7fb959a3a7`，清单和构建证明已核对到 Git 提交 `cae5773707b26ab2fd00903f84b712387894b256`。可选源码覆盖与启动选项见 [Windows 说明](windows.md#官方镜像与可选源码构建)。

以下服务器 `compose.yaml` / `compose.addon.yaml` 仍从该固定提交构建，使用本地标签 `bot-combined/qwenpaw:2.2.1-local`。官方 Dockerfile 安装 Chromium、Playwright 使用的系统浏览器、Xvfb、Xfce 和中文字体，保留完整浏览器/桌面运行依赖。源码构建的固定应用提交不等于锁定所有系统包；验证后应记录本地镜像 ID/上游 RepoDigest，并保留构建产物用于复现。[官方源文件](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/deploy/Dockerfile)。

使用 Linux amd64/arm64、Docker Engine、Compose v2 或更新版本、Python 3 和足够磁盘空间。产品保留完整文件、记忆和浏览器能力；硬件容量、任务并发与容器限额按实际使用测试和配置。镜像构建会用到外网和额外磁盘空间，预留 8 GB 以上可用空间。新 `.env` 的 `*_MEMORY` 和 `*_SWAP` 是可修改的部署参数；`memswap_limit` 是 RAM 加 swap 的总量，必须不小于对应 RAM 限额。

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

原 `bot` 域名和网站服务不需要改变。使用已有 AstrBot 原生后台管理平台连接和插件，使用 QwenPaw 原生 Console 管理模型、记忆、工具和任务；两边都保留官方页面。QwenPaw Console 和 NapCat 管理页通过下文的服务器端口转发访问。若要为原生后台增设公网管理域名，应另行设置 HTTPS、访问认证和受限代理网络；本仓库不会自动改动现有 Caddy。

## B：全新安装

适用于没有任何 AstrBot 数据的新目录。把仓库放到 `/opt/bot-combined/source`，切换到该源码目录：

```bash
bash deploy/setup.sh /opt/bot-combined
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml config --quiet
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml build qwenpaw
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml up -d
```

也可将全新根目录设成 `/opt/bot`；如果已有 `data`、`compose.yaml` 或部署回执，脚本会停止并要求选 A 模式。新安装不会携带任何作者的微信登录态、QQ 账号、模型密钥、联系人或聊天记录，首次全新准备会预配 QQ 接收端，请先检查已有条目，避免重复添加。微信接入、QQ 扫码和模型服务仍需自行配置。

## 两端初次配置

1. 在 AstrBot 确认桥接插件加载。通过自己的微信/QQ 私聊发送 `/paw whoami`，取得自己的平台用户 ID，填入桥接插件的“允许使用的用户ID”。也可以填入新私密 `.env` 的 `OWNER_USER_IDS`，多个 ID 逗号分隔；非空环境变量优先，空值使用后台设置。保持文件 0600，不把用户标识写进公共仓库。后台配置按 AstrBot 流程重新加载；改环境变量则按所选部署模式重新应用 AstrBot 容器配置。
2. `TOOL_ALLOWLIST` 初始为空，禁止由 QwenPaw 调用 AstrBot 插件工具。先选择需要的具体工具名称；`*` 是显式开放全部工具，不是默认值。普通插件指令仍由 AstrBot 自己处理。QwenPaw 使用自己的工作区；`/bridge-files` 是专用附件共享，不挂载其他项目或整个宿主数据。
3. QwenPaw 初次启动会通过官方入口初始化自己的空数据目录。默认 agent ID 是 `default`，不是 `main`。在控制台配置有效的模型提供商和模型；不要把 AstrBot 密钥自动抄到另一服务。
4. QwenPaw 会自动发现 `astrbot-bridge` 插件，无需给插件编造 `enabled` 开关。在 `default` agent 的 Channels 中启用 `astrbot`。插件已只读挂载到 `/app/working/plugins/qwenpaw_plugin_astrbot_bridge`；回调地址 `http://astrbot:9186` 和回调密钥由环境变量提供。需要手动编辑时，仅合并 `/app/working/workspaces/default/agent.json` 中 `channels.astrbot = {"enabled": true}`，保留所有其他字段；不要整份替换未知 `agent.json`。Native tool 使用环境变量 `ASTRBOT_BRIDGE_URL` 和 `BRIDGE_TOKEN`。
5. 在 AstrBot 中发送普通文字先验证自己的请求，再验证浏览器任务和插件工具。附件按下方步骤测试；桥接支持受限的会话附件，尚需实际 QQ/微信联调确认平台上传结果。微信和 QQ 会话各自隔离；不同账号的 ID 不应随意合并。
6. 在 QwenPaw 原生 `/cron-jobs` 页面创建主动任务，选择已登记的 `astrbot` 频道、用户和桥接会话。固定版本的原生新建表单默认 `mode=stream`、`tool_safety=false`；需明确选 `final`、开启工具安全和共享会话。允许发送结果时关闭 silent；遇到敏感工具审批等待所有者批准。`runtime.tool_safety` 是每个任务的字段，不是 `agent.json` 的任意开关。原生 Heartbeat、记忆设置和工具管理仍使用 QwenPaw 自己的页面，详见[原生页面分工](qwenpaw.md#使用两边原生后台)。

## 原生后台的访问与登录

部署材料已保留两套完整官方后台，当前没有统一登录。AstrBot 后台管理微信/QQ、插件和桥接配置；QwenPaw Console 管理模型、ReMe 记忆、文件、工具和主动任务。QwenPaw 构建使用官方 Dockerfile，包含它自己的前端构建产物。

`console` 服务把服务器 `127.0.0.1:8088` 转到 QwenPaw `8088`，并通过已有 `deploy/console.Caddyfile` 为 HTML、API、SSE 和 WebSocket 请求注入 `X-QwenPaw-Runtime-Token`。它转发原生 Console，不提供另一套管理页面。不要绕过代理直接打开 QwenPaw 容器端口，也不要把内部令牌放进浏览器 URL。

使用可连接服务器的 SSH 客户端时，例如：

```bash
ssh -N -L 8088:127.0.0.1:8088 -L 6099:127.0.0.1:6099 用户名@服务器地址
```

保持隧道连接后，在自己电脑打开 `http://127.0.0.1:8088/` 进入 QwenPaw 原生 Console，打开 `http://127.0.0.1:6099/webui/` 进入 NapCat。全新 AstrBot 若需要同样访问，可增加 `-L 6185:127.0.0.1:6185`，再打开本机 `http://127.0.0.1:6185/`；已有安装继续使用它现有的 AstrBot 管理入口与原账号。

仅能使用阿里云网页终端时，网页终端不会自动建立到你电脑的端口隧道。需使用管理工具提供的端口转发，或另行将原生 Console 接到有认证和 HTTPS 的管理域名。现有 Compose 没有配置该公网域名；不要把 `127.0.0.1:8088` 改为公开监听来代替认证。

当前 `.env` 和 Compose 默认 `QWENPAW_AUTH_ENABLED=false`，因此 QwenPaw Console 不额外要求它自己的账号登录，访问权限来自隧道或外层认证。AstrBot 和 NapCat 继续使用各自原生登录机制。内部服务仍由 `QWENPAW_RUNTIME_INTERNAL_TOKEN` 保护，代理补头只解决这一层访问。

QwenPaw 2.2.1 支持原生单用户账号：启用 `QWENPAW_AUTH_ENABLED=true` 后，可在 `/login` 首次注册，之后登录；官方也支持 `QWENPAW_AUTH_USERNAME` / `QWENPAW_AUTH_PASSWORD` 首次自动建号。**本版桥接尚未支持该额外登录 Bearer token**，且现有 Compose 没有传入这两个建号变量。原生账号注册后，内部 runtime header 不会绕过用户登录认证，AstrBot 请求会缺少第二层凭据；不能仅打开这个开关就认为整套配置齐全。当前默认访问方式适用于隧道/受认证外层代理，新增公网入口应保留内部边界与完整访问认证。

## 会话附件和文件权限

三个组件使用同一个容器路径 `/bridge-files`，宿主目录是 `${INSTALL_ROOT}/state/bridge-files`。AstrBot 和 QwenPaw 可读写，NapCat 只读。每个由服务器登记的 `ab_sid` 有三个独立目录：

| 目录 | 用途 |
|---|---|
| `/bridge-files/{ab_sid}/inbound` | AstrBot 检查、复制进来的本会话附件 |
| `/bridge-files/{ab_sid}/outbound` | QwenPaw 为本会话生成、准备发送的文件 |
| `/bridge-files/{ab_sid}/delivery` | AstrBot 发送时保存的受检文件副本 |

不要自行把 QQ/微信用户 ID 当成 `ab_sid`，也不要由模型填写目标会话。`astrbot_media_workspace` 工具从可信运行上下文返回当前会话的 `inbound_dir`、`outbound_dir` 和限制；让 QwenPaw 将生成文件写入它返回的 `outbound_dir`，再调用 `astrbot_send_file(path, kind)`。该工具没有用户或会话身份参数，Cron 也必须使用原来登记的真实会话。不能从 QwenPaw 的普通工作区直接发送：先由获准的文件操作生成本会话 outbound 文件。

默认每个文件最多 **20 MiB**、每次最多 **4 个附件**。`.env` 中 `BRIDGE_MAX_FILE_BYTES=20971520` 和 `BRIDGE_MAX_FILES=4` 可以降低，上限由两端执行；`BRIDGE_MEDIA_TIMEOUT=60` 控制 AstrBot 媒体操作等待。只接受本会话 outbound 中的普通文件，拒绝目录外路径、跨会话、URL、符号链接和硬链接。即使上游 `send_file_to_user` 输出一个 `file://` 引用，也不能绕过桥接自己的路径检查。

挂载目录只解决容器看见文件，**不会自动给 QwenPaw 文件工具授权**。保持工具审批和沙箱设置；首次让文件工具写 outbound 时，在 QwenPaw 的审批流程中确认实际工具和完整路径。需要持久授权时，由管理员在该工作区的 governance `policy.yaml` 里保留原有规则，并仅合并本会话路径的 `Read`、`Write`（按需再加 `Edit`、`Append`）规则，例如 `Write(/bridge-files/实际ab_sid/outbound/**)`、`Read(/bridge-files/实际ab_sid/outbound/**)`，`action: allow`。若要读取 inbound，只增加该会话 inbound 的 `Read` 规则。源码会将这些文件规则编译成沙箱只读/可写挂载。不要给 `/bridge-files/**`、`/app/working/**` 或整个宿主目录添加一条全局允许，也不要关掉 guard 来解决路径拒绝。将目录绑定成额外项目根会自动增加广泛的允许规则，不能把整个共享根作为绑定目标。[固定源码的权限实现](https://github.com/agentscope-ai/QwenPaw/blob/cae5773707b26ab2fd00903f84b712387894b256/src/qwenpaw/governance/resource_governor.py)。

AstrBot 入站本地文件来源默认只允许 `/AstrBot/data/temp`（`BRIDGE_SOURCE_ROOTS`）；如果适配器缓存位于别处，先确认真实缓存路径，再只添加该缓存目录。`BRIDGE_NAPCAT_HOSTS=napcat` 限定私网 HTTP 下载的 NapCat 服务名；不用把任意私网主机加入。QQ/微信协议登录缓存、数据库和模型密钥仍不应成为附件来源。默认镜像内三个应用以 root 运行，准备脚本创建的私密目录无需改成 0777；自行改 UID/GID 时，只调整专用 state 子目录的所有权。

验收时，用自己的 QQ/微信会话分别发送一张小图片和一个小文件，再让 QwenPaw 生成一个 outbound 文本文件并发送回来。QQ 可使用图片、音频、视频和文件组件；AstrBot 4.25.1 的微信适配器原生发送图片、视频和文件，音频作为文件发送。检查重复回调只投递一次、超过限额被拒绝、跨会话路径和链接文件被拒绝；这部分真实平台验收尚不能由静态测试代替。已有普通 AstrBot 插件的任意本地文件路径兼容性需另行检查。

## 查看状态和保留数据

从仓库源码目录运行只读诊断，优先取得不含密钥、QQ 号码或聊天内容的结构化结果：

```bash
python3 deploy/doctor.py --mode existing
```

全新安装用 `--mode fresh`。它核对容器、内网连接和鉴权、指定智能体就绪、共享目录挂载、用户 / 工具 / 频道配置。只执行读取和健康检查，不重启容器、不调用模型、不发送消息、不写配置。非默认容器名可用 `--astrbot-container` 等参数覆盖；`--help` 列出选项。该诊断尚未在实际 Linux Docker 部署中运行，单元测试使用受控 Docker 响应；`ok` 不等于真实 QQ / 微信已经成功登录和收发。

按选定模式将 `compose.yaml` 换成 `compose.addon.yaml`，`.env` 路径也对应更换：

```bash
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml ps
docker compose --env-file /opt/bot-combined/.env -f deploy/compose.yaml logs --tail 60 qwenpaw
docker stats --no-stream bot-qwenpaw bot-napcat
```

NapCat 日志可能含登录 token/二维码；在自己终端查看，发送截图前遮住这些内容。QwenPaw 数据、密钥、备份分别保存，三个运行组件只共享 `state/bridge-files`。停服务不会删除 state；不要使用 `down -v` 或删除数据目录。

已有安装回退：停止新增 add-on 服务，再用原项目的 Compose 和原 `.env` 显式重建原 AstrBot。这会移除新增挂载/网络，保留原数据。不得改用这个仓库的 fresh Compose 管理原 AstrBot。

运行验证前应确认：微信/QQ 各收到一次回应和一个小附件、普通插件指令可用、QwenPaw 任务能回到原会话、非所有者无法操作、空工具白名单被拒绝、文件路径越界被拒绝、需要审批的动作不会自行通过。仓库静态测试不代替这些真实平台测试。

## 可选：真实上游消息结构检查

`scripts/check_upstream_media.py` 可在独立 Python 3.11–3.13 环境中复现附件消息结构检查，不启动 AstrBot/QwenPaw、浏览器或 QQ/微信。需要 Pydantic 2、python-dotenv、aiohttp，以及固定 QwenPaw 提交的干净源码和 AgentScope `2.0.7.post1` 的官方 wheel。它不会把完整 QwenPaw 的模型/浏览器依赖装进常规 CI。

在单独测试环境准备这些小依赖和 wheel 后运行，例如：

```bash
python -m pip install 'pydantic>=2,<3' 'python-dotenv>=1,<2' 'aiohttp>=3.9,<4'
python -m pip download --no-deps --only-binary=:all: agentscope==2.0.7.post1 --dest /tmp/upstream-media
python scripts/check_upstream_media.py --qwenpaw-source /tmp/qwenpaw-fixed-source --agentscope-path /tmp/upstream-media/agentscope-2.0.7.post1-py3-none-any.whl --report /tmp/upstream-media/report.json
```

`/tmp/qwenpaw-fixed-source` 需事先检出本页所列 Git 提交；拒绝其中含 `.env` 的目录。脚本检查 wheel SHA256 和关键源码 hash，验证四类媒体的真实 `ToolChunk` JSON 往返、Qwen renderer 的列表/JSON 字符串输出、原生 Content 往返，以及真实工具事件经 Qwen `Envelope` 到本项目 `AssistantCollector` 的最终文字和附件去重。输出报告列出确切次数和范围。

为避免安装整个框架，检查器仅替换包初始化入口为 namespace：AgentScope 的 `tool`、`model` 和 QwenPaw 的父包不执行原 `__init__.py`；`model.FinishedReason` 转发未改动源码中的真实枚举。实际事件类、`ToolChunk`、schema、renderer、`Envelope` 和 collector 都执行真实源码，没有替代数据类。它验证消息协议，不能证明完整应用启动、模型能力、文件沙箱行为或 QQ/微信真实上传成功。
