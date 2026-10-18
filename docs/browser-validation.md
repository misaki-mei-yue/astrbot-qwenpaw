# 真实浏览器运行验证

2026-10-09，在独立临时环境中验证了 QwenPaw 2.2.1 的实际 Browser SDK 与 `browser` 工具的 subprocess worker。SDK 和工具 worker 均成功打开 `about:blank`、读取本机临时 HTML 页面、点击按钮并从新快照确认页面变化。[脱敏报告](qwenpaw-browser-validation.json)包含全部九项断言与实际版本；[可复现脚本](../scripts/check_qwenpaw_browser.py)不包含官方源码。

本次使用 Python 3.12.14、AgentScope 2.0.7.post1、Playwright 1.63.0，Windows 上现有 Edge 浏览器引擎版本为 154.0.4258.62。运行使用 headless 模式和全新 `guest` / `incognito` 上下文；工作配置、密钥目录及浏览器临时数据均位于本次创建的临时目录。没有使用既有浏览器 profile，没有访问外网网页、调用模型或向 QQ / 微信发消息。

## 固定源码

上游为 [QwenPaw v2.2.1](https://github.com/agentscope-ai/QwenPaw/tree/cae5773707b26ab2fd00903f84b712387894b256)，提交 `cae5773707b26ab2fd00903f84b712387894b256`。脚本在首次导入 QwenPaw 前检查安装包版本，并验证实际运行的十个源文件的 SHA-256，包括工具注册、SDK、身份决策、启动参数解析、Playwright 控制适配器及 subprocess worker。哈希先将 CRLF 归一化为 LF，以支持 Windows 安装；全部预期值和本次实际值见脚本与报告。该检查覆盖所列文件，未声称验证整棵源码或所有第三方依赖。

固定版本中的工具名是 `browser`。默认身份 `auto` 在 Chrome 扩展未连接时选择 `guest`，对应 Playwright 的无痕上下文；本脚本显式指定 `guest`。`/api/browser/chrome/status` 检查的是 Chrome 扩展连接，不能证明服务器 Playwright 能启动。仅成功导入 `playwright` 也不足以证明浏览器可用。

## 复现

需要已有 QwenPaw 2.2.1 完整 Python 环境、对应 Playwright 依赖和已安装的 Chromium / Edge。脚本不安装软件、不下载浏览器，必须明确提供浏览器可执行文件路径；不要在正在运行的 QwenPaw 进程中导入并调用它。它会拒绝已有 QwenPaw 导入状态和安装目录中的 `.env`，避免读取其他运行环境的配置。

在新的终端进程中运行，例如 Linux 已有 Chromium 时：

```bash
python scripts/check_qwenpaw_browser.py \
  --browser-executable /usr/bin/chromium \
  --report browser-report.json
```

Windows 可使用既有 Edge 的标准程序路径：

```powershell
python scripts/check_qwenpaw_browser.py --browser-executable 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe' --report browser-report.json
```

默认使用操作系统临时目录；`--temp-root` 可指定一个已有的临时目录。脚本先验证本次目录位于选定临时根内，再清理本次生成的配置及数据。它设置独立配置与密钥目录、禁用背景联网和代理、将域名解析限制到本机；页面仅包含脚本内置的测试内容。请求只触及 `about:blank` 和脚本启动的 `127.0.0.1` 页面。报告只输出版本、固定源文件哈希和布尔结果，排除个人路径、临时端口、工具 stdout、异常原文及凭据。返回码 `0` 表示全部九项通过；其他返回码需查看脱敏失败字段。

## 结果边界

本次确实运行了未替换的官方 SDK、真实 Playwright 浏览器和实际工具 subprocess worker；没有使用框架符号 shim 或浏览器假对象。这验证了当前 Windows 环境的浏览器执行链、页面读取和点击能力。Linux 镜像内的 Chromium/系统依赖、服务器资源、外网网站、登录流程、模型主动选择浏览器及 QQ / 微信最终收发尚需对应环境验证；本报告不能代替这些验证。

上游在清理工作区时还会尝试关闭未连接的 Chrome 扩展控制链，记录 `BridgeNotReady` 警告。脚本记录该警告的布尔标记，不返回堆栈；Playwright 清理及本次进程退出均成功。这个警告不表示 guest Playwright 启动失败。
