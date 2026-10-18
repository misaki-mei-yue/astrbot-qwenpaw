# 第三方项目与许可边界

AstrBot-QwenPaw 是独立的社区集成层。项目名称用于说明兼容关系，不表示得到上游官方背书，也不是三个上游团队的联合发行版。

公开仓库包含桥接插件、网关、部署配置、文档与测试；不包含上游完整源码、容器镜像、QQ 客户端或用户运行数据。部署时下载的外部组件继续适用各自许可，根目录的 AGPL 许可证不会替换它们的条款。

| 项目 | 适配版本 | 用途与许可 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 4.25.1 | 消息平台适配、插件及工具执行；上游 AGPL-3.0，具体权利与义务以上游许可证为准。 |
| [QwenPaw](https://github.com/agentscope-ai/QwenPaw) | 2.2.1，提交 `cae5773707b26ab2fd00903f84b712387894b256` | 原生助手、记忆、任务与工具运行时；[Apache-2.0](https://github.com/agentscope-ai/QwenPaw/blob/v2.2.1/LICENSE)。Copyright 2025 The QwenPaw Authors。 |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 4.18.33 | QQ 接入；[Limited Redistribution License for NapCat](https://raw.githubusercontent.com/NapNeko/NapCatQQ/v4.18.33/LICENSE)，Copyright © 2024 Mlikiowa。 |
| [NapCat-Docker](https://github.com/NapNeko/NapCat-Docker) | 外部镜像 `mlikiowa/napcat-docker:v4.18.33` | 镜像封装与运行环境。镜像中的 NapCat、QQ 客户端和其他依赖各有条款；镜像引用不等于取得任意再分发许可。 |

## NapCat 特别说明

NapCat 4.18.33 的许可证含非商业使用、再分发署名和许可文本要求，以及修改代码不得公开发布的限制；未明确授予的权利需向其作者取得许可。请阅读上方链接的固定版本完整条款。

本仓库只在部署配置中引用外部镜像，不附带 NapCat 源码、公开修改版、镜像层或 QQ 客户端，也不向使用者重新授予这些组件的权利。默认组合依赖 NapCat 时，不能仅凭本仓库的 AGPL 许可将整个组合描述为可任意商用的自由软件。商业使用、再分发镜像或修改 NapCat 前，应分别核实授权。

`-BuildQwenPaw` 仅用于可选的 QwenPaw 固定源码构建，不是 NapCat 源码构建或修改选项。

## 其他运行依赖

[aiohttp](https://github.com/aio-libs/aiohttp)、[jsonschema](https://github.com/python-jsonschema/jsonschema)、[PyYAML](https://github.com/yaml/pyyaml) 等依赖通过包管理器或上游镜像取得。本仓库不以自己的许可证替换其许可。Docker、浏览器、系统包、模型服务与消息平台也分别适用其条款。

若以后分发含第三方源码、镜像或二进制的产物，须另行核对该产物中的许可与通知；本文件不替代任何第三方许可证。

## 随附许可原文

- [AstrBot 4.25.1](licenses/AstrBot-v4.25.1.txt)
- [QwenPaw 2.2.1](licenses/QwenPaw-v2.2.1.txt)
- [NapCat 4.18.33](licenses/NapCat-v4.18.33.txt)
