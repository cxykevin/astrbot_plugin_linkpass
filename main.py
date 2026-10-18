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

#: 挂在 AstrMessageEvent / Context 类上的补丁标记 / 当前生效的插件实例。
_PATCH_MARKER = "_linkpass_original_set_result"
_ACTIVE_MARKER = "_linkpass_active_plugin"
_STREAM_WRAPPED_EXTRA = "_linkpass_stream_wrapped"
_CONTEXT_PATCH_MARKER = "_linkpass_original_send_message"
_CONTEXT_ACTIVE_MARKER = "_linkpass_active_context_plugin"
_EVENT_SEND_PATCH_MARKER = "_linkpass_original_event_send"
_EVENT_SEND_WRAPPED_MARKER = "_linkpass_event_send_wrapped"


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


def _install_send_message_patch() -> None:
    """包装 ``Context.send_message``，过滤主动发送/工具直发的文本。

    ``send_message_to_user``、cron 主动消息等路径不会经过
    ``on_decorating_result``，而是直接调用 ``Context.send_message``。
    这里在真正交给平台适配器之前改写 ``Plain`` 组件。
    """
    if hasattr(Context, _CONTEXT_PATCH_MARKER):
        return

    original = Context.send_message
    setattr(Context, _CONTEXT_PATCH_MARKER, original)

    async def patched_send_message(self, session, message_chain):
        plugin = getattr(Context, _CONTEXT_ACTIVE_MARKER, None)
        if plugin is not None and isinstance(message_chain, MessageChain):
            try:
                plugin._filter_outgoing_message(session, message_chain)
            except Exception:
                logger.exception("linkpass: 过滤主动发送消息失败，降级为原样发送")
        return await original(self, session, message_chain)

    Context.send_message = patched_send_message


def _uninstall_send_message_patch() -> None:
    """撤销 ``Context.send_message`` 补丁。"""
    original = getattr(Context, _CONTEXT_PATCH_MARKER, None)
    if original is None:
        return
    Context.send_message = original
    delattr(Context, _CONTEXT_PATCH_MARKER)


def _iter_event_classes() -> list[type]:
    """返回 ``AstrMessageEvent`` 及其全部已加载子类。"""
    classes: list[type] = []
    stack = [AstrMessageEvent]
    while stack:
        cls = stack.pop()
        classes.append(cls)
        stack.extend(cls.__subclasses__())
    return classes


def _install_event_send_patch() -> None:
    """包装各平台消息事件的 ``send``，过滤工具结果等直接发送。

    ``astr_agent_run_util`` 在 webchat 下会把工具调用结果直接
    ``await event.send(chain)``，这条路径同样不经过
    ``on_decorating_result`` 和 ``Context.send_message``。
    """
    for cls in _iter_event_classes():
        if "send" not in cls.__dict__:
            continue
        original = cls.__dict__["send"]
        if getattr(original, _EVENT_SEND_WRAPPED_MARKER, False):
            continue

        async def patched_send(self, message, *args, _original=original, **kwargs):
            plugin = getattr(AstrMessageEvent, _ACTIVE_MARKER, None)
            if plugin is not None and isinstance(message, MessageChain):
                try:
                    plugin._filter_outgoing_message(
                        self.unified_msg_origin,
                        message,
                    )
                except Exception:
                    logger.exception("linkpass: 过滤直发消息失败，降级为原样发送")
            return await _original(self, message, *args, **kwargs)

        setattr(patched_send, _EVENT_SEND_WRAPPED_MARKER, True)
        setattr(cls, _EVENT_SEND_PATCH_MARKER, original)
        cls.send = patched_send


def _uninstall_event_send_patch() -> None:
    """恢复所有被包装的平台事件 ``send``。"""
    for cls in _iter_event_classes():
        original = getattr(cls, _EVENT_SEND_PATCH_MARKER, None)
        if original is None:
            continue
        cls.send = original
        delattr(cls, _EVENT_SEND_PATCH_MARKER)


def _escape_chain(chain: MessageChain) -> int:
    """转义消息链中所有 ``Plain`` 文本，返回改动的组件数。"""
    changed = 0
    for comp in chain.chain or []:
        if not isinstance(comp, Plain) or not comp.text:
            continue
        escaped = escape_links_in_text(comp.text)
        if escaped != comp.text:
            comp.text = escaped
            changed += 1
    return changed


def _wrap_streaming_result(event: AstrMessageEvent, result) -> None:
    """命中目标 UMO 时，处理流式或最终结果链。"""
    plugin = getattr(AstrMessageEvent, _ACTIVE_MARKER, None)
    if plugin is None or not isinstance(result, MessageEventResult):
        return
    if not plugin._enabled() or not plugin._is_target(event.unified_msg_origin):
        return

    if result.result_content_type == ResultContentType.STREAMING_RESULT:
        if result.async_stream is None:
            return
        if event.get_extra(_STREAM_WRAPPED_EXTRA, False):
            return

        event.set_extra(_STREAM_WRAPPED_EXTRA, True)
        result.async_stream = _filtered_stream(result.async_stream)
        return

    # 最终结果链同样可能在流式结束后被平台或插件再次使用。
    if result.result_content_type in (
        ResultContentType.LLM_RESULT,
        ResultContentType.AGENT_RUNNER_ERROR,
        ResultContentType.STREAMING_FINISH,
    ):
        _escape_chain(result)


async def _filtered_stream(
    stream: AsyncGenerator[MessageChain, None],
) -> AsyncGenerator[MessageChain, None]:
    """逐 chunk 转义流式文本，并保留非文本组件。

    reasoning / think 段与普通回复分开缓冲，避免两段文本被拼到一起；
    两段都会转义，因此思考过程里出现的链接也不会漏掉。
    """
    escapers = {
        "plain": StreamingLinkEscaper(),
        "reasoning": StreamingLinkEscaper(),
    }
    current = "plain"

    def text_chain(kind: str, text: str) -> MessageChain:
        chain = MessageChain().message(text)
        if kind == "reasoning":
            chain.type = "reasoning"
        return chain

    async for chain in stream:
        # audio_chunk 的 Plain 是音频 base64，不能当文本处理。
        if chain.type == "audio_chunk":
            for text in escapers[current].flush():
                yield text_chain(current, text)
            yield chain
            continue

        kind = "reasoning" if chain.type == "reasoning" else "plain"
        if kind != current:
            for text in escapers[current].flush():
                yield text_chain(current, text)
            current = kind

        for comp in chain.chain or []:
            if isinstance(comp, Plain):
                for text in escapers[kind].feed(comp.text or ""):
                    yield text_chain(kind, text)
            else:
                for text in escapers[kind].flush():
                    yield text_chain(kind, text)
                yield chain.derive([comp])

    for text in escapers[current].flush():
        yield text_chain(current, text)


class LinkPassPlugin(Star):
    """LinkPass · QQ 群助手链接转义。

    只对 ``target_umos`` 中列出的会话生效。普通回复走
    ``on_decorating_result``，流式输出靠 ``AstrMessageEvent.set_result``
    补丁，``send_message_to_user`` / 主动消息走 ``Context.send_message``
    补丁，工具结果等直发走 ``AstrMessageEvent.send`` 补丁；这些路径都会在
    发送前转义 ``Plain`` 文本。
    """

    def __init__(self, context: Context, config: Mapping | None = None) -> None:
        super().__init__(context)
        self.plugin_config = config if isinstance(config, Mapping) else {}

    async def initialize(self) -> None:
        """启动时挂上流式结果与直发消息补丁，并打印一次生效范围。"""
        setattr(AstrMessageEvent, _ACTIVE_MARKER, self)
        setattr(Context, _CONTEXT_ACTIVE_MARKER, self)
        _install_set_result_patch()
        _install_send_message_patch()
        _install_event_send_patch()

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
        """插件停用、卸载或重载时撤销所有补丁。"""
        if getattr(AstrMessageEvent, _ACTIVE_MARKER, None) is self:
            setattr(AstrMessageEvent, _ACTIVE_MARKER, None)
            _uninstall_set_result_patch()
        if getattr(Context, _CONTEXT_ACTIVE_MARKER, None) is self:
            setattr(Context, _CONTEXT_ACTIVE_MARKER, None)
            _uninstall_send_message_patch()
        _uninstall_event_send_patch()

    @filter.on_decorating_result()
    async def escape_links(self, event: AstrMessageEvent) -> None:
        """发送消息前转义 AI 文本中的链接。"""
        if not self._enabled() or not self._is_target(event.unified_msg_origin):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 普通流水线只处理模型输出；直发消息由 Context.send_message 补丁处理。
        if not result.is_model_result():
            return

        changed = _escape_chain(result)

        if changed:
            logger.debug(
                "linkpass: 已转义 %d 段文本（UMO=%s）。",
                changed,
                event.unified_msg_origin,
            )

    def _filter_outgoing_message(self, session, chain: MessageChain) -> None:
        """过滤直发消息链（Context / 平台事件 send）。"""
        if not self._enabled():
            return
        umo = session if isinstance(session, str) else str(session)
        if not self._is_target(umo):
            return

        changed = _escape_chain(chain)
        if changed:
            logger.debug(
                "linkpass: 已转义 %d 段直发文本（UMO=%s）。",
                changed,
                umo,
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
