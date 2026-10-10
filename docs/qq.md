# 接入 QQ（NapCat）

NapCat 登录 QQ，负责 OneBot v11 通信；AstrBot 接收消息并保留原插件生态，选择 QwenPaw 任务时再交给桥接插件。不用运行第二个 AstrBot，也不用合并旧版聊天数据库。

## Windows 本地组合包

1. 双击 `start.cmd` 完成启动后，打开 **http://localhost:18080/**，切换到「QQ 登录」原生页面。组合包只发布这个本机端口，不需要打开 6099 或建立服务器隧道。
2. NapCat WebUI 第一次要求输入的是它自己的 **WebUI Token / 登录密码**，不是 `ONEBOT_TOKEN`，也没有可依赖的固定 `napcat` 默认密码。在自己的电脑上用记事本打开包内 `runtime/state/napcat/config/webui.json`，仅把 `token` 字段的值复制到登录框。也可在 Docker Desktop 中打开本目录对应项目的 NapCat 日志，查找最新一次启动的 `WebUi Token:`。这是私密登录信息，不要贴到聊天、截图或仓库。
3. 在登录后的页面用手机 QQ 扫码确认。验证和账户选择由你完成；首次扫码前通常还没有账号专属的 OneBot 配置。
4. **首次全新准备现在同时配置了两端。** AstrBot 会有启用的 `qq` / OneBot v11 机器人，监听容器内部 `0.0.0.0:6199`；NapCat 默认配置连接 `ws://astrbot:6199/ws`，两端使用同一随机通信令牌。扫码后在 NapCat 网络配置里确认下表设置，账号专属配置可能优先于默认配置。不需要在电脑或云安全组开放 6199。
5. 若需要进入 AstrBot 检查机器人，用户名默认是 `astrbot`；**4.25.1 会生成随机初始密码，不是 `astrbot/astrbot`**。在 Docker Desktop 打开本目录对应项目的 AstrBot 日志，查找最新的 `Initial password:`，只在自己电脑查看并复制到登录框。登录后由你在原生页面输入新密码；组合包不代你修改密码，也不从配置中的哈希恢复明文。

已有 `runtime/state/astrbot/cmd_config.json` 或 NapCat 配置会保持原样；重跑准备脚本不会补写或覆盖它们。若旧组合包已启动过、AstrBot 机器人列表仍为空，请按下文的 OneBot v11 字段手动添加。此时通信令牌来自包内私密 `runtime/.env` 的 `ONEBOT_TOKEN`，与 WebUI 登录密码是两回事。不要删除 `runtime` 重新初始化。

## 服务器或已有 AstrBot 接入

1. 根据 [部署说明](deployment.md) 选择 fresh 或 add-on 模式，完成准备并启动服务。通过服务器端口转发打开 `http://127.0.0.1:6099/webui/`，在自己的服务器日志中读取 NapCat 登录密码。公开管理入口需要单独的 HTTPS 和认证，本项目没有发布公网 6099。
2. 在 WebUI 选择 QQ 的二维码登录，用手机 QQ 扫码确认。扫码和账户验证由你操作；项目不带预设账号。新版本可能在登录后刷新 WebUI 密码并要求更换，以界面提示为准。旧 Windows 客户端缓存不拷贝到 Linux；服务器成功后退出旧本机 NapCat。
3. 全新准备已经创建 AstrBot 的 `qq` / OneBot v11 机器人，先在后台检查现有条目，避免重复添加。已有 AstrBot 加装或保留的旧配置需要手动添加：AstrBot 后台 → 机器人 → 添加 → OneBot v11：ID `qq`、启用、监听 `0.0.0.0`、端口 `6199`。token 填准备脚本生成的 `ONEBOT_TOKEN`（在自己的私密 `.env` 中读取）。
4. NapCat 后台 → 网络配置 → WebSocket 客户端（反向 WS），确认已启用。准备脚本写入的默认设置是：

   | 字段 | 值 |
   |---|---|
   | URL | `ws://astrbot:6199/ws` |
   | 消息格式 | `array` |
   | token | 与 AstrBot 相同的 `ONEBOT_TOKEN` |
   | 上报自身消息 | 关闭 |
   | 心跳周期 | 30000 毫秒 |
   | 重连间隔 | 5000 毫秒 |

   实际账号专属配置可能优先于默认 `onebot11.json`；登录后在 WebUI 确认这些字段。不要填写 `127.0.0.1`，那是 NapCat 容器自己；不要选择正向 WS 服务端。端口 3000/3001/6199 都不用在阿里云安全组放开。
5. AstrBot 日志出现“aiocqhttp(OneBot v11) 适配器已连接”后，先用自己的 QQ 私聊测试，再测试群内 @、图片、语音、文件、视频和已有插件指令。最后测试 `/paw`、长期记忆、主动任务和下方的桥接附件。不要把私人联系人或聊天日志写进仓库。

QQ 客户端数据保存在 `state/napcat/qq`，设置保存在 `state/napcat/config`。NapCat 只有 `/bridge-files` 的只读共享，不可读 AstrBot 的模型配置/数据库，也不可读 QwenPaw 的全部工作区。QwenPaw 通过 `astrbot_media_workspace` 取得当前会话 `/bridge-files/{ab_sid}/outbound`，在获准的目录内生成文件后，用 `astrbot_send_file(path, kind)` 发回原会话。AstrBot 将受检副本放入同会话 `delivery`；QQ 文件/视频引用在 NapCat 内仍是同一个绝对路径，图片/音频由 AstrBot 转成 OneBot 可发送的数据。每文件默认最多 20 MiB、每次 4 个，不接受其他会话、根外路径、URL、符号链接或硬链接。[权限与验收步骤](deployment.md#会话附件和文件权限)。

先实测小图片、小文件，再测试 QQ 音频/视频和 Cron 回传。自动化的路径与消息类型检查不能证明 QQ 平台一定接受上传，平台大小、格式或账户限制仍以真实结果为准。微信音频作为文件发出，不代表原生语音消息。原 AstrBot 插件如果给 NapCat 传其他本地路径，需单独适配或传 URL/base64，不能声称全部文件插件天然兼容。

NapCat 使用 [官方固定版本 Docker 镜像](https://hub.docker.com/r/mlikiowa/napcat-docker/tags)。当前 WebUI 不支持 `prefix`，不应直接假设能放在 `bot` 域名的 `/napcat` 子路径。配置依据：[NapCat 官方说明](https://napneko.github.io/config/basic)、[AstrBot OneBot 说明](https://docs.astrbot.app/platform/aiocqhttp.html)。
