from astrbot.core.message.components import (
    At,
    Face,
    Image,
    Plain,
    Reply,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class ReplyStep(BaseStep):
    name = StepName.REPLY
    unsupported_platforms = {"dingtalk"}

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.reply

    def _component_name(self, comp) -> str:
        return type(comp).__name__

    def _can_reply_chain(self, chain) -> bool:
        allowed = {"Plain", "Image", "Face", "At"}
        return all(self._component_name(comp) in allowed for comp in chain)

    def _message_id(self, ctx: OutContext) -> str:
        return str(getattr(ctx.event.message_obj, "message_id", "") or "")

    def _real_message_items(self, queue) -> list[str]:
        return [str(item) for item in queue if not str(item).startswith("__bot_reply__")]

    async def handle(self, ctx: OutContext) -> StepResult:
        platform_name = ctx.event.get_platform_name()
        if platform_name in self.unsupported_platforms:
            return StepResult(msg=f"平台不支持智能引用，已跳过: {platform_name}")

        if self.cfg.threshold > 0 and self._can_reply_chain(ctx.chain):
            msg_id = self._message_id(ctx)
            if not msg_id:
                return StepResult()

            queue = ctx.group.msg_queue
            queue_str = self._real_message_items(queue)
            if msg_id in queue_str:
                idx = queue_str.index(msg_id)
                pushed = len(queue_str) - idx - 1
                if pushed >= self.cfg.threshold:
                    ctx.chain.insert(0, Reply(id=msg_id))
                    if self.cfg.include_at and isinstance(ctx.event, AiocqhttpMessageEvent):
                        ctx.chain.insert(1, At(qq=ctx.event.get_sender_id()))
                        # 在 At 后添加带零宽空格包裹的空格，确保与后续内容有间距
                        ctx.chain.insert(2, Plain(text="\u200b \u200b"))
                    # 仅移除已引用的消息及其之前的记录，保留之后的消息用于后续引用判断
                    while queue and str(queue[0]) != msg_id:
                        queue.popleft()
                    if queue and str(queue[0]) == msg_id:
                        queue.popleft()
                    # 追加 bot 插嘴标记，确保后续回复也能判断“被插嘴”
                    if getattr(ctx.group, "last_reply_mark_msg_id", None) != msg_id:
                        queue.append(f"__bot_reply__{msg_id}")
                        setattr(ctx.group, "last_reply_mark_msg_id", msg_id)
                    return StepResult(msg=f"已插入Reply组件, 引用消息{msg_id}")
        return StepResult()
