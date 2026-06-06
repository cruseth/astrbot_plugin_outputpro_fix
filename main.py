import contextvars
import time
from types import SimpleNamespace
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


class _ActiveSendEvent:
    """Small event adapter for messages sent through context.send_message."""

    def __init__(self, unified_msg_origin: Any, chain: list[Any], context: Context):
        self.unified_msg_origin = str(unified_msg_origin or "")
        self.message_str = "".join(
            comp.text for comp in chain if isinstance(comp, Plain)
        )
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
            meta = getattr(platform, "meta", None)
            if str(getattr(meta, "id", "") or "") == self.platform_id:
                return platform
        return None


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.pipeline = Pipeline(self.cfg)
        self._original_send_message = None
        self._wrapped_send_message = None

    async def initialize(self):
        await self.pipeline.initialize()
        self._install_send_message_wrapper()

    async def terminate(self):
        self._restore_send_message()
        await self.pipeline.terminate()

    def _install_send_message_wrapper(self) -> None:
        if self._original_send_message is not None:
            return
        original = getattr(self.context, "send_message", None)
        if original is None:
            logger.warning("[OutputPro] context.send_message 不存在，主动发送拦截未启用。")
            return

        self._original_send_message = original

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
        return "".join(comp.text for comp in chain if isinstance(comp, Plain))

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
        ctx = OutContext(
            event=event,  # type: ignore[arg-type]
            chain=active_chain,
            is_llm=False,
            plain=self._plain_from_chain(active_chain),
            gid=group_id,
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(group_id),
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
        return await original(unified_msg_origin, processed, *args, **kwargs)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        """收到群消息时"""
        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(gid)

        if self.cfg.reply.threshold > 0 and sender_id != self_id:
            g.msg_queue.append(event.message_obj.message_id)

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
            group=StateManager.get_group(event.get_group_id()),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)
