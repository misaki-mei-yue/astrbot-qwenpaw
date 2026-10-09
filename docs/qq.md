# 接入 QQ（NapCat）

NapCat 登录 QQ，负责 OneBot v11 通信；AstrBot 接收消息并保留原插件生态，选择 QwenPaw 任务时再交给桥接插件。不用运行第二个 AstrBot，也不用合并旧版聊天数据库。

1. 根据 [部署说明](deployment.md) 选择 fresh 或 add-on 模式，完成准备并启动服务。通过服务器端口转发打开 `http://127.0.0.1:6099/webui/`，在自己的服务器日志中读取 NapCat 登录密码。公开管理入口需要单独的 HTTPS 和认证，本项目没有发布公网 6099。
2. 在 WebUI 选择 QQ 的二维码登录，用手机 QQ 扫码确认。扫码和账户验证由你操作；项目不带预设账号。新版本可能在登录后刷新 WebUI 密码并要求更换，以界面提示为准。旧 Windows 客户端缓存不拷贝到 Linux；服务器成功后退出旧本机 NapCat。
3. AstrBot 后台 → 机器人 → 添加 → OneBot v11：ID `qq`、启用、监听 `0.0.0.0`、端口 `6199`。token 填准备脚本生成的 `ONEBOT_TOKEN`（在自己的私密 `.env` 中读取）。
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
5. AstrBot 日志出现“aiocqhttp(OneBot v11) 适配器已连接”后，先用自己的 QQ 私聊测试，再测试群内 @、图片、语音、文件、视频和已有插件指令。最后测试 `/paw`、长期记忆、主动任务。不要把私人联系人或聊天日志写进仓库。

QQ 客户端数据保存在 `state/napcat/qq`，设置保存在 `state/napcat/config`。NapCat 只有 `/bridge-files` 的只读共享，不可读 AstrBot 的模型配置/数据库，也不可读 QwenPaw 的全部工作区。当前桥接只发送文字，QwenPaw 生成的附件去控制台下载；共享目录为后续文件传输预留。原 AstrBot 插件如果给 NapCat 传其他本地路径，需单独适配或传 URL/base64，不能声称全部文件插件天然兼容。

NapCat 使用 [官方固定版本 Docker 镜像](https://hub.docker.com/r/mlikiowa/napcat-docker/tags)。当前 WebUI 不支持 `prefix`，不应直接假设能放在 `bot` 域名的 `/napcat` 子路径。配置依据：[NapCat 官方说明](https://napneko.github.io/config/basic)、[AstrBot OneBot 说明](https://docs.astrbot.app/platform/aiocqhttp.html)。
