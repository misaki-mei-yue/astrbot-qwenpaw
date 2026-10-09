# 验证记录

日期：2026-10-09。版本：0.2.0，媒体桥接实验版。

## 已完成

- Windows 本机 Python 3.10.2：共运行 132 项测试，127 通过，5 项因符号链接权限或 POSIX FIFO 条件跳过。
- Windows 本机 Python 3.12.14：共运行同样的 132 项测试，130 通过，仅 2 项 POSIX FIFO 测试跳过；符号链接检查实际执行通过。
- 其中 10 项使用真实本机 HTTP 连接双方适配器：SSE 聊天、工具调用、主动回传、持久去重、鉴权 / 权限拒绝、原会话审批，以及图片 / 纯文件消息的字节往返、原生发送文件输出、主动附件、篡改与跨会话拒绝。
- 媒体检查覆盖大小 / 数量上限、链接与路径越界、读取中修改、发送副本、DNS 地址约束、HTTPS 与重定向、下载流限制、音频平台差异及真实媒体后缀。
- AstrBot / QwenPaw 的运行时由接口替身提供；QwenPaw 固定版本的插件注册、channel、runtime hook 和请求上下文接口已对照官方源码核实。
- Docker Compose CLI 实际解析通过：全新部署、追加部署、原 AstrBot 加 override；测试确认原数据挂载与原网络保留。
- 两个安装准备脚本通过 Bash 语法检查。准备脚本不会启动服务。

另用固定上游真实模块执行了最小媒体格式验证：AgentScope 2.0.7.post1 的消息 / 事件 / ToolChunk、QwenPaw 2.2.1 的 schema / renderer / Envelope，串到本项目的 AssistantCollector。4 次 ToolChunk JSON 往返、8 个 renderer 样例、4 次原生 Content 往返、4 条真实 Envelope → Collector 链路通过，覆盖图片、文件、视频与音频。普通文档在原生 Envelope 中使用 `type=data`，已经据此修正桥接解析。

这项检查绕过了应用包的初始化导入，实际参与测试的类型和转换函数保持上游原样。它没有运行完整应用、模型或浏览器。复现脚本为 [check_upstream_media.py](../scripts/check_upstream_media.py)，参数说明见[部署文档](deployment.md)；[检查报告](upstream-media-validation.json)记录固定版本、源文件与构件哈希。

GitHub 持续运行 Linux Python 3.10 / 3.12 检查，最新结果按对应提交查看 [Actions](https://github.com/misaki-mei-yue/astrbot-qwenpaw/actions)。0.1.0 的首版检查已通过；本记录中的本地结果对应本次 0.2.0 代码。

协议测试不会调用模型、收发真实微信 / QQ，也不会执行上游完整容器。

## 仍需完成

- 构建并启动固定版本上游容器，配置真实模型。
- 微信、QQ 登录后的真实收发；当前使用的第三方插件实际兼容性。
- QwenPaw 真实记忆、文件、浏览器与定时任务联调。
- 服务器完整负载的内存 / CPU / 磁盘记录，以及网络中断、重启、平台发送失败验证。
- 图片、音频、文件和视频的真实平台收发、格式兼容性及资源占用验收。

本记录只证明以上本地协议与配置检查结果，不表示服务器已完成部署或所有目标功能已验收。
