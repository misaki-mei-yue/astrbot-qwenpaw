# 第三方项目

本仓库包含独立编写的适配器、配置与测试，不包含用户迁移数据、上游完整源码或二进制镜像。

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：适配 v4.25.1。上游许可证 AGPL-3.0；插件依赖其公开 API 与相应版本内部工具执行器。
- [QwenPaw](https://github.com/agentscope-ai/QwenPaw)：适配 v2.2.1 / commit `cae5773707b26ab2fd00903f84b712387894b256`。上游许可证 Apache-2.0。Docker 构建直接使用固定版本官方源码与官方 Dockerfile。
- [NapCat Docker](https://github.com/NapNeko/NapCat-Docker) 与 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：QQ 接入依赖固定版本外部镜像。运行时的 NapCat 与 QQ 客户端遵守各自许可及平台约定。
- [aiohttp](https://github.com/aio-libs/aiohttp)、[jsonschema](https://github.com/python-jsonschema/jsonschema)、[PyYAML](https://github.com/yaml/pyyaml)：通过包管理器安装，未随本仓库打包。

项目名称用于说明兼容关系；本集成不是三个上游团队的官方合并发行版。
