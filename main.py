"""astrbot_plugin_linkpass —— 针对指定 UMO 转义 AI 回复中的链接。

QQ 群助手会拦截机器人消息里的可点击链接。本插件在消息发出前读取最终消息链，
只处理命中配置 UMO 的 **LLM 结果**，把文本中的链接改写成不可点击形式：

* 去掉 ``http://`` / ``https://``（浏览器仍能直接打开裸域名）；
* 域名和 IPv4 的每个 ``.`` 前加一个反斜杠，例如 ``example.com`` ->
  ``example\\.com``；
* IPv6 的每个 ``:`` 前加一个反斜杠，例如 ``2001:db8::1`` ->
  ``2001\\:db8\\:\\:1``；
* 邮箱只改写 ``@`` 后面的域名，local part 保持不变。

插件元数据以 ``metadata.yaml`` 为准，因此这里不使用已废弃的 ``@register``。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)

from .link_filter import StreamingLinkEscaper, escape_links_in_text

#: 挂在 AstrMessageEvent 类上的补丁标记 / 当前生效的插件实例。
_PATCH_MARKER = "_linkpass_original_set_result"
_ACTIVE_MARKER = "_linkpass_active_plugin"
_STREAM_WRAPPED_EXTRA = "_linkpass_stream_wrapped"


def _install_set_result_patch() -> None:
    """包装 ``AstrMessageEvent.set_result``，拦截流式生成器。

    流式结果不会经过 ``on_decorating_result``（respond stage 直接消费
    ``async_stream``），所以在结果被设置时就把它换成带过滤的生成器。
    """
    if hasattr(AstrMessageEvent, _PATCH_MARKER):
        return

    original = AstrMessageEvent.set_result
    setattr(AstrMessageEvent, _PATCH_MARKER, original)

    def patched_set_result(self, result):
        try:
            _wrap_streaming_result(self, result)
        except Exception:
            logger.exception("linkpass: 包装流式输出失败，降级为原样发送")
        return original(self, result)

    AstrMessageEvent.set_result = patched_set_result


def _uninstall_set_result_patch() -> None:
    """撤销 ``set_result`` 补丁。"""
    original = getattr(AstrMessageEvent, _PATCH_MARKER, None)
    if original is None:
        return
    AstrMessageEvent.set_result = original
    delattr(AstrMessageEvent, _PATCH_MARKER)


def _wrap_streaming_result(event: AstrMessageEvent, result) -> None:
    """命中目标 UMO 时，把流式结果包成带链接转义的生成器。"""
    plugin = getattr(AstrMessageEvent, _ACTIVE_MARKER, None)
    if plugin is None or not isinstance(result, MessageEventResult):
        return
    if result.result_content_type != ResultContentType.STREAMING_RESULT:
        return
    if result.async_stream is None:
        return
    if not plugin._enabled() or not plugin._is_target(event.unified_msg_origin):
        return
    if event.get_extra(_STREAM_WRAPPED_EXTRA, False):
        return

    event.set_extra(_STREAM_WRAPPED_EXTRA, True)
    result.async_stream = _filtered_stream(result.async_stream)


async def _filtered_stream(
    stream: AsyncGenerator[MessageChain, None],
) -> AsyncGenerator[MessageChain, None]:
    """逐 chunk 转义流式文本，并保留非文本组件。

    只缓冲最后一个可能仍在延伸的 ASCII token；遇到空白、中文或括号就
    把前面的内容转义后立即发出，因此不会把跨 chunk 的链接拆坏。
    """
    escaper = StreamingLinkEscaper()

    async for chain in stream:
        # reasoning / audio_chunk 等非普通文本链原样透传。
        if chain.type not in (None, "plain"):
            for text in escaper.flush():
                yield MessageChain().message(text)
            yield chain
            continue

        for comp in chain.chain or []:
            if isinstance(comp, Plain):
                for text in escaper.feed(comp.text or ""):
                    yield chain.derive([Plain(text)])
            else:
                for text in escaper.flush():
                    yield chain.derive([Plain(text)])
                yield chain.derive([comp])

    for text in escaper.flush():
        yield MessageChain().message(text)


class LinkPassPlugin(Star):
    """LinkPass · QQ 群助手链接转义。

    只对 ``target_umos`` 中列出的会话生效，且只处理模型产生的消息结果
    （``result.is_model_result()``），不会改动指令、插件主动发送的普通消息。

    流式输出不会经过 ``on_decorating_result``（respond stage 直接消费
    ``async_stream``），因此启动时会把 ``AstrMessageEvent.set_result``
    包一层：命中 UMO 的流式生成器会被替换成逐段转义后的版本。
    """

    def __init__(self, context: Context, config: Mapping | None = None) -> None:
        super().__init__(context)
        self.plugin_config = config if isinstance(config, Mapping) else {}

    async def initialize(self) -> None:
        """启动时挂上流式结果补丁，并打印一次生效范围。"""
        setattr(AstrMessageEvent, _ACTIVE_MARKER, self)
        _install_set_result_patch()

        if not self._enabled():
            logger.info("linkpass: 插件已禁用，不处理任何消息。")
            return

        targets = self._target_umos()
        if targets:
            logger.info(
                "linkpass: 已启用，将对 %d 个 UMO 的 AI 回复转义链接。",
                len(targets),
            )
        else:
            logger.warning(
                "linkpass: target_umos 为空，不会处理任何会话。"
                "可在目标会话发送 /sid 查看当前会话的 UMO 并填入配置。",
            )

    async def terminate(self) -> None:
        """插件停用、卸载或重载时撤销 ``set_result`` 补丁。"""
        if getattr(AstrMessageEvent, _ACTIVE_MARKER, None) is self:
            setattr(AstrMessageEvent, _ACTIVE_MARKER, None)
            _uninstall_set_result_patch()

    @filter.on_decorating_result()
    async def escape_links(self, event: AstrMessageEvent) -> None:
        """发送消息前转义 AI 文本中的链接。"""
        if not self._enabled() or not self._is_target(event.unified_msg_origin):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 仅处理模型输出，避免误改指令回复和插件主动发送的消息。
        if not result.is_model_result():
            return

        changed = 0
        for comp in result.chain:
            if not isinstance(comp, Plain) or not comp.text:
                continue
            escaped = escape_links_in_text(comp.text)
            if escaped != comp.text:
                comp.text = escaped
                changed += 1

        if changed:
            logger.debug(
                "linkpass: 已转义 %d 段文本（UMO=%s）。",
                changed,
                event.unified_msg_origin,
            )

    def _enabled(self) -> bool:
        """读取启用开关。"""
        value = self.plugin_config.get("enabled", True)
        return value if isinstance(value, bool) else True

    def _target_umos(self) -> set[str]:
        """读取并规范化目标 UMO 列表。

        Returns:
            去除首尾空白后的 UMO 集合；配置格式非法时返回空集合。
        """
        raw = self.plugin_config.get("target_umos", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _is_target(self, umo: str) -> bool:
        """判断当前会话是否在过滤范围内。"""
        return umo in self._target_umos()
