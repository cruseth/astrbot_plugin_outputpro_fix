import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    Node,
    Nodes,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


#: 获取机器人登录信息的上限超时（秒）。
#: aiocqhttp 的 CQHttp 以 api_timeout_sec=180 创建，协议端「已连接但无响应」时
#: get_login_info 可能长时间阻塞，这里给主动发送路径一个可接受的等待上限。
_LOGIN_INFO_TIMEOUT_SEC = 5.0


class ForwardStep(BaseStep):
    name = StepName.FORWARD

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.forward
        self._configured_node_name: str = str(self.cfg.node_name or "").strip()
        self._bot_name_cache: dict[str, str] = {}
        self._tg_single_message_limit = 3500

    @staticmethod
    def _normalize_uin(value: Any) -> str:
        """把候选账号值规整成合法的 OneBot QQ 号，不合法时返回空串。"""
        if isinstance(value, bool):
            return ""
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            return ""
        if not text or text == "0":
            return ""
        if not (text.isascii() and text.isdigit()):
            return ""
        return text

    @staticmethod
    async def _call_login_info(bot: Any) -> dict[str, Any]:
        """尽力获取机器人登录信息，任何失败都只返回空 dict。

        ``await`` 分支带 ``_LOGIN_INFO_TIMEOUT_SEC`` 上限超时；超时与异常一律
        按失败降级，交由调用方走「跳过合并转发」路径。``asyncio.CancelledError``
        不属于 ``Exception``，不会被下面的分支吞掉，任务取消语义保持有效。
        """
        if bot is None:
            return {}

        try:
            info = bot.get_login_info()
            if inspect.isawaitable(info):
                info = await asyncio.wait_for(
                    info, timeout=_LOGIN_INFO_TIMEOUT_SEC
                )
        except asyncio.TimeoutError:
            logger.debug(
                "[OutputPro] 获取机器人登录信息超时（上限 "
                f"{_LOGIN_INFO_TIMEOUT_SEC}s），已跳过合并转发"
            )
            return {}
        except Exception:
            logger.debug("[OutputPro] 获取机器人登录信息失败", exc_info=True)
            return {}

        if isinstance(info, Mapping):
            return dict(info)
        return {}

    async def _ensure_node_name(
        self,
        event: AstrMessageEvent,
        uin: str = "",
        info: dict[str, Any] | None = None,
    ) -> str:
        """解析合并转发节点昵称：配置 > 登录昵称 > "AstrBot"，失败只降级。"""
        if self._configured_node_name:
            return self._configured_node_name

        if uin and (cached := self._bot_name_cache.get(uin)):
            return cached

        if info is None:
            info = await self._call_login_info(getattr(event, "bot", None))

        node_name = ""
        if info:
            node_name = str(info.get("nickname") or "").strip()

        if not node_name:
            node_name = "AstrBot"

        if uin:
            self._bot_name_cache[uin] = node_name
        return node_name

    async def _resolve_node_identity(
        self, event: AstrMessageEvent
    ) -> tuple[str, str]:
        """解析合并转发节点的 (uin, name)。

        uin 必须是机器人自身 QQ：先采信事件自带的 self_id（被动回复路径），
        拿不到合法值时退回 ``bot.get_login_info()``（主动发送路径）。
        两者都不可用时返回 ``("", "")``，由调用方放弃合并转发。
        """
        raw_self_id = str(event.get_self_id() or "").strip()

        uin = self._normalize_uin(raw_self_id)
        if uin:
            return uin, await self._ensure_node_name(event, uin)

        info = await self._call_login_info(getattr(event, "bot", None))
        uin = self._normalize_uin(info.get("user_id"))
        if not uin:
            logger.warning(
                "[OutputPro] 无法解析合并转发节点的机器人 QQ，已跳过合并转发："
                f"platform={self._platform_name(event)} self_id={raw_self_id!r}"
            )
            return "", ""

        return uin, await self._ensure_node_name(event, uin, info)

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:
            return ""

    def _tg_utf16_len(self, text: str) -> int:
        if not text:
            return 0
        return len(text.encode("utf-16-le")) // 2

    def _tg_split_by_utf16(self, text: str, max_len: int) -> list[str]:
        if not text:
            return [""]
        if max_len <= 0:
            return [text]
        chunks = []
        buf = []
        buf_len = 0
        for ch in text:
            ch_len = self._tg_utf16_len(ch)
            if buf and buf_len + ch_len > max_len:
                chunks.append("".join(buf))
                buf = [ch]
                buf_len = ch_len
            else:
                buf.append(ch)
                buf_len += ch_len
        if buf:
            chunks.append("".join(buf))
        return chunks

    def _is_plain_component(self, comp: BaseMessageComponent) -> bool:
        return type(comp).__name__ == "Plain" and isinstance(
            getattr(comp, "text", None), str
        )

    def _plain_text_parts(self, chain: list[BaseMessageComponent]) -> list[str]:
        return [
            comp.text
            for comp in chain
            if self._is_plain_component(comp) and comp.text
        ]

    def _plain_text(self, chain: list[BaseMessageComponent]) -> str:
        return "".join(self._plain_text_parts(chain))

    def _plain_text_len(self, chain: list[BaseMessageComponent]) -> int:
        return sum(len(text) for text in self._plain_text_parts(chain))

    async def _send_tg_expandable_blocks(
        self, event: AstrMessageEvent, messages: list[str]
    ) -> bool:
        from telegram import MessageEntity
        from telegram.ext import ExtBot

        if not messages:
            return False

        tg_bot = getattr(event, "client", None)
        if not tg_bot or not isinstance(tg_bot, ExtBot):
            return False

        chat_id = str(event.get_group_id() or event.get_sender_id())
        message_thread_id = None

        if "#" in chat_id:
            chat_id, thread = chat_id.split("#", 1)
            if thread.isdigit():
                message_thread_id = int(thread)

        max_len = max(200, int(self._tg_single_message_limit))
        groups = []

        def flush_group(text: str, entities: list[MessageEntity]):
            if not text:
                return
            groups.append((text, entities))

        current_text = ""
        current_entities: list[MessageEntity] = []
        current_len = self._tg_utf16_len(current_text)

        expanded_blocks = []
        for block in messages:
            block = (block or "").strip()
            if not block:
                continue
            if self._tg_utf16_len(block) > max_len:
                expanded_blocks.extend(self._tg_split_by_utf16(block, max_len))
            else:
                expanded_blocks.append(block)

        for block in expanded_blocks:
            block = (block or "").strip()
            if not block:
                continue
            if not current_text:
                current_text = block
                current_entities = [
                    MessageEntity(
                        type="expandable_blockquote",
                        offset=0,
                        length=self._tg_utf16_len(block),
                    )
                ]
                current_len = self._tg_utf16_len(block)
                continue
            prefix = "\n\n"
            add_text = prefix + block
            add_len = self._tg_utf16_len(add_text)
            if current_len + add_len > max_len:
                flush_group(current_text, current_entities)
                current_text = block
                current_entities = [
                    MessageEntity(
                        type="expandable_blockquote",
                        offset=0,
                        length=self._tg_utf16_len(block),
                    )
                ]
                current_len = self._tg_utf16_len(block)
                continue
            offset = self._tg_utf16_len(current_text + prefix)
            current_text += add_text
            current_entities.append(
                MessageEntity(
                    type="expandable_blockquote",
                    offset=offset,
                    length=self._tg_utf16_len(block),
                )
            )
            current_len += add_len

        flush_group(current_text, current_entities)

        sent = 0
        for text, entities in groups:
            await tg_bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities or None,
                message_thread_id=message_thread_id,
                parse_mode=None,
            )
            sent += 1
        return sent > 0

    async def handle(self, ctx: OutContext) -> StepResult:
        if ctx.chain and self._plain_text_len(ctx.chain) > self.cfg.threshold:
            platform_name = ctx.event.get_platform_name()

            if self._is_onebot_platform(platform_name):
                uin, name = await self._resolve_node_identity(ctx.event)
                if not uin:
                    # 拿不到机器人自身 QQ 时不转发，避免节点归属错乱
                    return StepResult()

                content = [
                    component
                    for component in ctx.chain
                    if not isinstance(component, Reply)
                ]
                nodes = Nodes([])
                nodes.nodes.append(Node(uin=uin, name=name, content=content))
                ctx.chain[:] = [nodes]
                return StepResult(msg="已将消息转换为转发节点")

            elif platform_name == "telegram":
                text = self._plain_text(ctx.chain)
                messages = self._tg_split_by_utf16(text, self._tg_single_message_limit)
                success = await self._send_tg_expandable_blocks(ctx.event, messages)
                if success:
                    ctx.event.stop_event()
                    ctx.chain.clear()
                    return StepResult(msg="已使用 Telegram 折叠引用发送")

        return StepResult()

    def _is_onebot_platform(self, platform_name: str) -> bool:
        normalized = str(platform_name or "").lower()
        return any(
            key in normalized
            for key in ("aiocqhttp", "onebot", "cqhttp", "napcat", "lagrange")
        )
