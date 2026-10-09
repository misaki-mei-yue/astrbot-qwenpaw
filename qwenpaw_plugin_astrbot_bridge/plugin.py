"""Official QwenPaw plugin registration entry point."""

from .channel import AstrBotChannel
from .tools import astrbot_call_tool, astrbot_list_tools, configure_tools


class AstrBotBridgePlugin:
    def register(self, api):
        from .runtime_hooks import BridgeTurnCleanupHook, BridgeTurnContextHook

        configure_tools(api.config)
        api.register_runtime_hook(BridgeTurnContextHook())
        api.register_runtime_hook(BridgeTurnCleanupHook())
        api.register_channel(
            channel_class=AstrBotChannel,
            label="AstrBot 微信 / QQ",
            description="将主动任务结果发送到原来的 AstrBot 会话。",
            config_fields=[
                {"name": "callback_url", "label": "AstrBot 内部桥接地址", "type": "text", "default": "http://astrbot:9186"},
                {"name": "callback_token", "label": "桥接令牌", "type": "password", "required": True},
                {"name": "files_dir", "label": "共享文件目录", "type": "text", "default": "/bridge-files"},
            ],
        )
        # A channel plugin can also register tools in the 2.2.1 PluginApi.
        api.register_tool(
            tool_name="astrbot_list_tools",
            tool_func=astrbot_list_tools,
            description="列出当前 AstrBot 会话允许使用的插件工具。",
            enabled=True,
            tool_type="network",
        )
        api.register_tool(
            tool_name="astrbot_call_tool",
            tool_func=astrbot_call_tool,
            description="调用当前 AstrBot 会话允许使用的插件工具。",
            enabled=True,
            tool_type="network",
        )


plugin = AstrBotBridgePlugin()
