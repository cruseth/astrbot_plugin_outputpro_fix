import asyncio
import contextvars
import time
from types import MethodType, SimpleNamespace
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import OutContext, StateManager, StepName
from .core.pipeline import Pipeline


_ACTIVE_SEND_PROCESSING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "outputpro_active_send_processing", default=False
)


def _is_plain_component(comp) -> bool:
    return type(comp).__name__ == "Plain" and isinstance(getattr(comp, "text", None), str)


def _plain_text_from_chain(chain: list[Any]) -> str:
    return "".join(comp.text for comp in chain if _is_plain_component(comp))


class _ActiveSendEvent:
    """Small event adapter for messages sent through context.send_message."""

    def __init__(self, unified_msg_origin: Any, chain: list[Any], context: Context):
        self.unified_msg_origin = str(unified_msg_origin or "")
        self.message_str = _plain_text_from_chain(chain)
        self._context = context
        self._stopped = False
        self._result = None

        self.platform_id, self.message_type, self.session_id = self._parse_umo(
            self.unified_msg_origin
        )
        self.message_obj = SimpleNamespace(
            message_id="",
            timestamp=int(time.time()),
            group_id=self.get_group_id(),
            sender=SimpleNamespace(user_id=self.get_sender_id(), nickname=""),
            raw_message=None,
        )
        self.session = SimpleNamespace(
            session_id=self.session_id,
            message_type=self.message_type,
        )

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str]:
        parts = umo.split(":", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], parts[1], ""
        return "", "", umo

    def get_platform_name(self) -> str:
        platform = self._find_platform()
        meta = self._platform_meta(platform)
        name = str(getattr(meta, "name", "") or "").strip()
        if name:
            return name

        for attr in ("platform_name", "name", "adapter_name"):
            value = getattr(platform, attr, None)
            if value:
                return str(value)
        return self.platform_id

    def get_platform_id(self) -> str:
        return self.platform_id

    def get_group_id(self) -> str:
        message_type = self.message_type.lower()
        if "group" in message_type:
            return self.session_id
        return ""

    def get_sender_id(self) -> str:
        message_type = self.message_type.lower()
        if "private" in message_type or "friend" in message_type:
            return self.session_id
        return ""

    def get_self_id(self) -> str:
        platform = self._find_platform()
        if platform is None:
            return ""
        for attr in ("self_id", "bot_id", "account_id", "id"):
            value = getattr(platform, attr, None)
            if value:
                return str(value)
        bot = getattr(platform, "bot", None)
        for attr in ("self_id", "bot_id", "account_id", "qq"):
            value = getattr(bot, attr, None)
            if value:
                return str(value)
        return ""

    def get_sender_name(self) -> str:
        return ""

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result

    def stop_event(self):
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def plain_result(self, text: str):
        return SimpleNamespace(
            chain=[Plain(text)],
            get_plain_text=lambda: text,
            is_llm_result=lambda: False,
            is_model_result=lambda: False,
        )

    def should_call_llm(self, *_args, **_kwargs):
        return None

    async def send_typing(self):
        return None

    def _find_platform(self):
        manager = getattr(self._context, "platform_manager", None)
        platform_insts = getattr(manager, "platform_insts", []) or []
        for platform in platform_insts:
            if str(getattr(platform, "id", "") or "") == self.platform_id:
                return platform
            meta = self._platform_meta(platform)
            if str(getattr(meta, "id", "") or "") == self.platform_id:
                return platform
        return None

    def _platform_meta(self, platform):
        if platform is None:
            return None
        meta = getattr(platform, "meta", None)
        if callable(meta):
            try:
                return meta()
            except Exception:
                return None
        return meta


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.pipeline = Pipeline(self.cfg)
        self._original_send_message = None
        self._wrapped_send_message = None

    async def initialize(self):
        await self.pipeline.initialize()
        self._ensure_send_message_wrapper()

    async def terminate(self):
        self._restore_send_message()
        await self.pipeline.terminate()

    def _ensure_send_message_wrapper(self) -> None:
        current = getattr(self.context, "send_message", None)
        if current is None:
            logger.warning("[OutputPro] context.send_message 不存在，主动发送拦截未启用。")
            return

        if current is self._wrapped_send_message:
            return

        if self._wrapped_send_message is not None:
            logger.warning(
                "[OutputPro] context.send_message 已被替换，重新安装主动发送拦截。"
            )

        self._original_send_message = current

        async def wrapped_send_message(unified_msg_origin, message_chain, *args, **kwargs):
            return await self._send_message_with_pipeline(
                unified_msg_origin, message_chain, *args, **kwargs
            )

        self._wrapped_send_message = wrapped_send_message
        setattr(self.context, "send_message", wrapped_send_message)
        logger.info("[OutputPro] 已启用主动发送 pipeline 拦截。")

    def _restore_send_message(self) -> None:
        if self._original_send_message is None:
            return
        current = getattr(self.context, "send_message", None)
        if current is self._wrapped_send_message:
            setattr(self.context, "send_message", self._original_send_message)
            logger.info("[OutputPro] 已恢复原始主动发送函数。")
        else:
            logger.warning(
                "[OutputPro] context.send_message 已被其他逻辑替换，跳过自动恢复。"
            )
        self._original_send_message = None
        self._wrapped_send_message = None

    def _plain_from_chain(self, chain: list[Any]) -> str:
        return _plain_text_from_chain(chain)

    def _event_state_key(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            return umo

        for getter in ("get_group_id", "get_sender_id"):
            fn = getattr(event, getter, None)
            if callable(fn):
                try:
                    value = str(fn() or "").strip()
                    if value:
                        return value
                except Exception:
                    pass
        return ""

    def _flatten_split_segments(self, segments: list[list[Any]]) -> list[Any]:
        return [comp for segment in segments for comp in segment]

    def _matches_full_split_chain(self, message_chain, segments: list[list[Any]]) -> bool:
        chain = getattr(message_chain, "chain", None)
        if chain is None:
            return False
        expected = self._flatten_split_segments(segments)
        return len(chain) == len(expected) and all(
            actual is expect or actual == expect
            for actual, expect in zip(chain, expected)
        )

    def _derive_split_message(self, message_chain, segment: list[Any]):
        derive = getattr(message_chain, "derive", None)
        if callable(derive):
            return derive(segment)
        return MessageChain(segment)

    async def _sleep_split_delay(self, event, delay: float) -> None:
        if delay <= 0:
            return

        platform_name = ""
        try:
            platform_name = str(event.get_platform_name() or "")
        except Exception:
            pass

        if not self.cfg.split.show_typing or platform_name not in {
            "telegram",
            "weixin_oc",
            "aiocqhttp",
        }:
            await asyncio.sleep(delay)
            return

        async def show_once():
            try:
                if platform_name in {"telegram", "weixin_oc"}:
                    send_typing = getattr(event, "send_typing", None)
                    if callable(send_typing):
                        await send_typing()
                    return

                gid = ""
                try:
                    gid = event.get_group_id()
                except Exception:
                    pass
                if platform_name == "aiocqhttp" and not gid:
                    bot = getattr(event, "bot", None)
                    api = getattr(bot, "api", None)
                    uid = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
                    if api and uid:
                        await api.call_action(
                            "set_input_status", user_id=uid, event_type=1
                        )
            except Exception:
                logger.debug("[Splitter] 发送 typing 失败", exc_info=True)

        if delay <= 1.0:
            await show_once()
            await asyncio.sleep(delay)
            return

        interval = min(2.5, max(1.0, delay / 3))
        remaining = delay
        while remaining > 0:
            await show_once()
            sleep_time = min(interval, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time

    async def _send_split_segments(
        self,
        event,
        original_send,
        message_chain,
        segments: list[list[Any]],
        delays: list[float] | None,
        *args,
        **kwargs,
    ):
        sent_result = None
        delays = delays or []
        for index, segment in enumerate(segments):
            split_message = self._derive_split_message(message_chain, segment)
            sent_result = await original_send(split_message, *args, **kwargs)
            if index < len(segments) - 1:
                delay = delays[index] if index < len(delays) else 0.0
                await self._sleep_split_delay(event, delay)
        return sent_result

    def _install_event_split_sender(self, event, ctx: OutContext) -> None:
        segments = ctx.split_segments
        if not segments or getattr(event, "__outputpro_split_sender_installed", False):
            return

        original_send = getattr(event, "__outputpro_original_event_send", None)
        if original_send is None:
            original_send = getattr(event, "send", None)
        if original_send is None:
            logger.warning("[Splitter] event.send 不存在，无法安装分段发送包装器。")
            return

        delays = list(ctx.split_delays or [])

        async def wrapped_send(_event_self, message_chain, *args, **kwargs):
            if not self._matches_full_split_chain(message_chain, segments):
                return await original_send(message_chain, *args, **kwargs)

            setattr(_event_self, "send", original_send)
            return await self._send_split_segments(
                _event_self,
                original_send,
                message_chain,
                segments,
                delays,
                *args,
                **kwargs,
            )

        setattr(event, "__outputpro_split_sender_installed", True)
        setattr(event, "send", MethodType(wrapped_send, event))

    def _is_core_result_send(self, event, message_chain) -> bool:
        get_result = getattr(event, "get_result", None)
        if not callable(get_result):
            return False

        try:
            result = get_result()
        except Exception:
            return False

        if result is None:
            return False
        if message_chain is result:
            return True

        result_chain = getattr(result, "chain", None)
        chain = getattr(message_chain, "chain", None)
        return result_chain is not None and chain is result_chain

    def _install_event_send_wrapper(self, event: AstrMessageEvent) -> None:
        if getattr(event, "__outputpro_active_send_installed", False):
            return

        original_send = getattr(event, "send", None)
        if original_send is None:
            return

        setattr(event, "__outputpro_original_event_send", original_send)

        async def wrapped_send(_event_self, message_chain, *args, **kwargs):
            if (
                _ACTIVE_SEND_PROCESSING.get()
                or self._is_core_result_send(_event_self, message_chain)
            ):
                return await original_send(message_chain, *args, **kwargs)

            chain = getattr(message_chain, "chain", None)
            if chain is None:
                return await original_send(message_chain, *args, **kwargs)

            active_chain = list(chain)
            state_key = self._event_state_key(_event_self)
            ctx = OutContext(
                event=_event_self,
                chain=active_chain,
                is_llm=False,
                plain=self._plain_from_chain(active_chain),
                gid=_event_self.get_group_id(),
                uid=_event_self.get_sender_id(),
                bid=_event_self.get_self_id(),
                group=StateManager.get_group(state_key),
                timestamp=getattr(_event_self.message_obj, "timestamp", int(time.time())),
            )

            token = _ACTIVE_SEND_PROCESSING.set(True)
            try:
                should_send = await self.pipeline.run(ctx)
            except Exception as exc:
                logger.warning(
                    f"[OutputPro] event.send pipeline 处理失败，回退原始发送：{exc}"
                )
                return await original_send(message_chain, *args, **kwargs)
            finally:
                _ACTIVE_SEND_PROCESSING.reset(token)

            if getattr(_event_self, "is_stopped", lambda: False)() or not should_send or not ctx.chain:
                return None

            processed = MessageChain(ctx.chain)
            if ctx.split_segments:
                return await self._send_split_segments(
                    _event_self,
                    original_send,
                    processed,
                    ctx.split_segments,
                    ctx.split_delays,
                    *args,
                    **kwargs,
                )

            return await original_send(processed, *args, **kwargs)

        setattr(event, "__outputpro_active_send_installed", True)
        setattr(event, "send", MethodType(wrapped_send, event))

    async def _send_message_with_pipeline(
        self, unified_msg_origin, message_chain, *args, **kwargs
    ):
        original = self._original_send_message
        if original is None:
            return await self.context.send_message(
                unified_msg_origin, message_chain, *args, **kwargs
            )

        if _ACTIVE_SEND_PROCESSING.get():
            return await original(unified_msg_origin, message_chain, *args, **kwargs)

        chain = getattr(message_chain, "chain", None)
        if chain is None:
            return await original(unified_msg_origin, message_chain, *args, **kwargs)

        active_chain = list(chain)
        event = _ActiveSendEvent(unified_msg_origin, active_chain, self.context)
        group_id = event.get_group_id()
        state_key = event.unified_msg_origin or group_id
        ctx = OutContext(
            event=event,  # type: ignore[arg-type]
            chain=active_chain,
            is_llm=False,
            plain=self._plain_from_chain(active_chain),
            gid=group_id,
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(state_key),
            timestamp=event.message_obj.timestamp,
        )

        token = _ACTIVE_SEND_PROCESSING.set(True)
        try:
            should_send = await self.pipeline.run(ctx)
        except Exception as exc:
            logger.warning(
                f"[OutputPro] 主动发送 pipeline 处理失败，回退原始发送：{exc}"
            )
            return await original(unified_msg_origin, message_chain, *args, **kwargs)
        finally:
            _ACTIVE_SEND_PROCESSING.reset(token)

        replacement = event.get_result()
        if replacement is not None and getattr(replacement, "chain", None) is not None:
            ctx.chain[:] = list(replacement.chain)

        if event.is_stopped() or not should_send or not ctx.chain:
            return None

        processed = MessageChain(ctx.chain)
        if ctx.split_segments:
            sent_result = None
            delays = ctx.split_delays or []
            for index, segment in enumerate(ctx.split_segments):
                sent_result = await original(
                    unified_msg_origin, MessageChain(segment), *args, **kwargs
                )
                if index < len(ctx.split_segments) - 1:
                    delay = delays[index] if index < len(delays) else 0.0
                    await self._sleep_split_delay(event, delay)
            return sent_result

        return await original(unified_msg_origin, processed, *args, **kwargs)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        """收到消息时"""
        self._ensure_send_message_wrapper()
        self._install_event_send_wrapper(event)

        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(self._event_state_key(event))

        if self.cfg.reply.threshold > 0 and sender_id != self_id:
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
            if message_id:
                g.msg_queue.append(message_id)

        if self.cfg.pipeline.is_enabled_step(StepName.AT) and not self.cfg.at.at_str:
            name = event.get_sender_name()
            if len(g.name_to_qq) >= 100:
                g.name_to_qq.popitem(last=False)
            g.name_to_qq[name] = sender_id

    @filter.on_decorating_result(priority=10000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前"""
        result = event.get_result()
        if not result or not result.chain:
            return

        ctx = OutContext(
            event=event,
            chain=result.chain,
            is_llm=result.is_llm_result(),
            plain=result.get_plain_text(),
            gid=event.get_group_id(),
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(self._event_state_key(event)),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)
        self._install_event_split_sender(event, ctx)
