"""合并转发节点 uin 归属回归测试。

背景：主动发送（``context.send_message`` / ``event.send``）走 ``_ActiveSendEvent``
时拿不到机器人真实 QQ，合并转发节点的 uin 会变成 aiocqhttp 伪造属性的
``functools.partial`` 字符串，QQ 客户端把节点归属显示成会话对象（用户本人）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_ROOT.parent
for path in (PLUGIN_ROOT, WORKSPACE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from devtools.astrbot_stub import install


_MISSING = object()

# 线上主动发送时 get_self_id() 实际拿到的垃圾字符串（aiocqhttp __getattr__ 伪造）
GARBAGE_SELF_ID = (
    "functools.partial(<bound method CQHttp.call_action of "
    "<aiocqhttp.CQHttp object at 0x000000000000>>, 'self_id')"
)
LONG_TEXT = "主动消息合并转发节点 uin 归属回归测试文本" * 2


@contextmanager
def _stub_pipeline_module() -> Iterator[None]:
    """注入最小 ``core.pipeline`` 替身。

    ``main.py`` 顶层只依赖 ``.core.config`` / ``.core.model`` /
    ``.core.pipeline``；真实 pipeline 会牵出 emoji、jieba 等本机未安装的
    三方依赖。被测的 ``_ActiveSendEvent`` 不依赖 Pipeline 的任何行为，
    因此用占位模块隔离。
    """
    module_name = "astrbot_plugin_outputpro_fix.core.pipeline"
    saved = sys.modules.get(module_name, _MISSING)
    module = types.ModuleType(module_name)

    class Pipeline:
        def __init__(self, config: Any = None):
            self.plugin_config = config

    module.Pipeline = Pipeline  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        yield
    finally:
        if saved is _MISSING:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = saved


@pytest.fixture(scope="module")
def forward_api():
    with install() as modules:

        class BaseMessageComponent:
            pass

        class Plain(BaseMessageComponent):
            def __init__(self, text: str = "", **kwargs: Any):
                self.text = text

        class Reply(BaseMessageComponent):
            def __init__(self, **kwargs: Any):
                pass

        class Node(BaseMessageComponent):
            def __init__(
                self,
                uin: str = "",
                name: str = "",
                content=None,
                **kwargs: Any,
            ):
                self.uin = uin
                self.name = name
                self.content = list(content or [])

        class Nodes(BaseMessageComponent):
            def __init__(self, nodes=None, **kwargs: Any):
                self.nodes = list(nodes or [])

        components_module = modules["astrbot.core.message.components"]
        for name, value in {
            "BaseMessageComponent": BaseMessageComponent,
            "Plain": Plain,
            "Reply": Reply,
            "Node": Node,
            "Nodes": Nodes,
        }.items():
            setattr(components_module, name, value)

        import core

        step_package = types.ModuleType("core.step")
        step_package.__path__ = [str(PLUGIN_ROOT / "core" / "step")]
        sys.modules["core.step"] = step_package
        core.step = step_package

        from core.config import ForwardConfig
        from core.model import OutContext, StepResult
        from core.step import forward as forward_module
        from core.step.forward import ForwardStep

        class FakeBot:
            """模拟 aiocqhttp 的 CQHttp：未定义属性返回可调用对象。"""

            def __init__(self, login_info: Any = None):
                self._login_info = login_info
                self.login_info_calls = 0

            async def get_login_info(self):
                self.login_info_calls += 1
                if isinstance(self._login_info, Exception):
                    raise self._login_info
                return self._login_info

        class FakeEvent:
            def __init__(
                self,
                self_id: str = "",
                bot: Any = None,
                platform_name: str = "aiocqhttp",
                expose_bot: bool = True,
            ):
                self._self_id = self_id
                self.platform_name = platform_name
                if expose_bot:
                    self.bot = bot

            def get_self_id(self) -> str:
                return self._self_id

            def get_platform_name(self) -> str:
                return self.platform_name

        def make_step(node_name: str = "", threshold: int = 20) -> ForwardStep:
            config = SimpleNamespace(
                forward=ForwardConfig({"threshold": threshold, "node_name": node_name})
            )
            return ForwardStep(config)

        def make_context(chain: list[Any], event: Any) -> OutContext:
            return OutContext(
                event=event,
                chain=chain,
                is_llm=False,
                plain="",
                gid="",
                uid="",
                bid="",
                group=SimpleNamespace(),
                timestamp=0,
            )

        yield SimpleNamespace(
            Plain=Plain,
            Node=Node,
            Nodes=Nodes,
            ForwardStep=ForwardStep,
            forward_module=forward_module,
            FakeBot=FakeBot,
            FakeEvent=FakeEvent,
            StepResult=StepResult,
            make_step=make_step,
            make_context=make_context,
        )


@pytest.fixture(scope="module")
def plugin_main():
    module_name = "astrbot_plugin_outputpro_fix.main"
    saved_main = sys.modules.get(module_name, _MISSING)

    with install(), _stub_pipeline_module():
        from astrbot_plugin_outputpro_fix import main as plugin_main_module

        yield plugin_main_module

    if saved_main is _MISSING:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = saved_main


def _single_node(ctx) -> Any:
    assert len(ctx.chain) == 1
    nodes = ctx.chain[0]
    assert type(nodes).__name__ == "Nodes"
    assert len(nodes.nodes) == 1
    return nodes.nodes[0]


def test_active_send_uses_login_info_when_self_id_is_garbage(forward_api):
    bot = forward_api.FakeBot({"user_id": 10001, "nickname": "yuki"})
    event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)
    step = forward_api.make_step(node_name="")
    ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)

    result = asyncio.run(step.handle(ctx))

    node = _single_node(ctx)
    assert node.uin == "10001"
    assert node.name == "yuki"
    assert [type(component).__name__ for component in node.content] == ["Plain"]
    assert result.msg == "已将消息转换为转发节点"
    assert bot.login_info_calls == 1


@pytest.mark.parametrize(
    "bot_kind",
    ["missing", "raises", "no_user_id", "not_a_mapping"],
)
def test_unresolvable_identity_skips_forward(forward_api, caplog, bot_kind):
    chain = [forward_api.Plain(LONG_TEXT)]
    bot = None

    if bot_kind == "missing":
        event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, expose_bot=False)
        # 事件不暴露 bot，任何登录信息调用都无从发生
        assert getattr(event, "bot", None) is None
    elif bot_kind == "raises":
        bot = forward_api.FakeBot(RuntimeError("boom"))
        event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)
    elif bot_kind == "no_user_id":
        bot = forward_api.FakeBot({"nickname": "yuki"})
        event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)
    else:
        bot = forward_api.FakeBot("not-a-mapping")
        event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)

    step = forward_api.make_step(node_name="")
    ctx = forward_api.make_context(chain, event)

    with caplog.at_level(logging.WARNING, logger="astrbot"):
        result = asyncio.run(step.handle(ctx))

    # 不构造 Nodes，链保持原样
    assert [type(component).__name__ for component in ctx.chain] == ["Plain"]
    assert ctx.chain[0].text == LONG_TEXT
    # 返回空 StepResult
    assert isinstance(result, forward_api.StepResult)
    assert not result.msg
    assert not result.abort
    # uin 解析只尝试一次登录信息接口，不做重试
    if bot is not None:
        assert bot.login_info_calls == 1
    # 至少留一条可排查的 warning
    warnings = [record.getMessage() for record in caplog.records]
    assert any(
        "合并转发" in message and "aiocqhttp" in message for message in warnings
    ), warnings


def test_login_info_timeout_skips_forward_quickly(forward_api, monkeypatch, caplog):
    """协议端已连接但无响应时，get_login_info 必须有上限超时，不能拖住主动发送。"""

    class SlowBot:
        def __init__(self):
            self.login_info_calls = 0

        async def get_login_info(self):
            self.login_info_calls += 1
            await asyncio.sleep(10)
            return {"user_id": 10001, "nickname": "yuki"}

    monkeypatch.setattr(forward_api.forward_module, "_LOGIN_INFO_TIMEOUT_SEC", 0.05)
    assert forward_api.forward_module._LOGIN_INFO_TIMEOUT_SEC == 0.05

    bot = SlowBot()
    event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)
    step = forward_api.make_step(node_name="")
    ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)

    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        started = time.monotonic()
        result = asyncio.run(step.handle(ctx))
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert bot.login_info_calls == 1
    assert [type(component).__name__ for component in ctx.chain] == ["Plain"]
    assert ctx.chain[0].text == LONG_TEXT
    assert isinstance(result, forward_api.StepResult)
    assert not result.msg
    # 超时按 debug 记录，便于排查且不刷屏
    messages = [record.getMessage() for record in caplog.records]
    assert any("超时" in message for message in messages), messages


def test_call_login_info_does_not_swallow_cancelled_error(forward_api):
    """任务取消语义不能被吞掉。"""

    class HangingBot:
        async def get_login_info(self):
            await asyncio.sleep(10)
            return {"user_id": 10001}

    async def scenario():
        task = asyncio.ensure_future(
            forward_api.ForwardStep._call_login_info(HangingBot())
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_sync_login_info_return_is_accepted(forward_api):
    """非 awaitable 的同步返回分支保持可用。"""

    class SyncBot:
        def get_login_info(self):
            return {"user_id": 10001, "nickname": "yuki"}

    event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=SyncBot())
    step = forward_api.make_step(node_name="")
    ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)

    asyncio.run(step.handle(ctx))

    node = _single_node(ctx)
    assert node.uin == "10001"
    assert node.name == "yuki"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10001", "10001"),
        (10001, "10001"),
        (" 10001 ", "10001"),
        ("0", ""),
        ("", ""),
        ("   ", ""),
        ("10001abc", ""),
        ("١٠٠٠١", ""),
        (True, ""),
        (None, ""),
    ],
)
def test_normalize_uin(forward_api, value, expected):
    step = forward_api.make_step(node_name="")

    assert step._normalize_uin(value) == expected


def test_valid_self_id_fast_path(forward_api):
    bot = forward_api.FakeBot({"user_id": 10001, "nickname": "yuki"})
    event = forward_api.FakeEvent(self_id="10001", bot=bot)
    step = forward_api.make_step(node_name="")

    first_ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)
    asyncio.run(step.handle(first_ctx))
    node = _single_node(first_ctx)
    assert node.uin == "10001"
    assert node.name == "yuki"

    # 同一 uin 的昵称走缓存，不重复调用登录信息接口
    second_ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)
    asyncio.run(step.handle(second_ctx))
    assert _single_node(second_ctx).uin == "10001"
    assert bot.login_info_calls == 1


def test_configured_node_name_has_priority(forward_api):
    bot = forward_api.FakeBot({"user_id": 10001, "nickname": "yuki"})
    event = forward_api.FakeEvent(self_id=GARBAGE_SELF_ID, bot=bot)
    step = forward_api.make_step(node_name="custom")
    ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)

    asyncio.run(step.handle(ctx))

    node = _single_node(ctx)
    assert node.uin == "10001"
    assert node.name == "custom"


def test_passive_path_does_not_call_login_info_when_name_configured(forward_api):
    bot = forward_api.FakeBot({"user_id": 10001, "nickname": "yuki"})
    event = forward_api.FakeEvent(self_id="10001", bot=bot)
    step = forward_api.make_step(node_name="custom")
    ctx = forward_api.make_context([forward_api.Plain(LONG_TEXT)], event)

    asyncio.run(step.handle(ctx))

    assert _single_node(ctx).uin == "10001"
    assert bot.login_info_calls == 0


def test_active_send_event_filters_forged_self_id(plugin_main):
    class ForgedBot:
        """模拟 aiocqhttp bot：任何未知属性都返回可调用对象。"""

        def __getattr__(self, item: str):
            if item.startswith("__"):
                raise AttributeError(item)
            return lambda *args, **kwargs: None

    forged_bot = ForgedBot()

    class FakePlatform:
        def __init__(self, self_id: Any = None):
            self.bot = forged_bot
            if self_id is not None:
                self.self_id = self_id

        def meta(self):
            return SimpleNamespace(id="aiocqhttp", name="aiocqhttp")

        def __getattr__(self, item: str):
            if item.startswith("__"):
                raise AttributeError(item)
            return lambda *args, **kwargs: None

    class FakeContext:
        def __init__(self, platform):
            self.platform_manager = SimpleNamespace(platform_insts=[platform])

    umo = "aiocqhttp:FriendMessage:10001"

    forged_event = plugin_main._ActiveSendEvent(
        umo, [], FakeContext(FakePlatform())
    )
    assert forged_event.get_self_id() == ""
    assert forged_event.bot is forged_bot

    real_event = plugin_main._ActiveSendEvent(
        umo, [], FakeContext(FakePlatform(self_id="10001"))
    )
    assert real_event.get_self_id() == "10001"
    assert real_event.bot is forged_bot


def test_real_aiocqhttp_forged_self_id_is_rejected(plugin_main):
    """用真实 aiocqhttp 库验证 ``__getattr__`` 伪造属性会被类型白名单挡掉。"""
    aiocqhttp = pytest.importorskip("aiocqhttp")
    bot = aiocqhttp.CQHttp(
        use_ws_reverse=True,
        import_name="aiocqhttp",
        access_token=None,
    )

    forged = getattr(bot, "self_id", None)
    assert not isinstance(forged, (str, int))

    class RealBotPlatform:
        def __init__(self):
            self.bot = bot

        def meta(self):
            return SimpleNamespace(id="aiocqhttp", name="aiocqhttp")

    class FakeContext:
        def __init__(self):
            self.platform_manager = SimpleNamespace(
                platform_insts=[RealBotPlatform()]
            )

    event = plugin_main._ActiveSendEvent(
        "aiocqhttp:FriendMessage:10001", [], FakeContext()
    )

    assert event.get_self_id() == ""
    assert event.bot is bot
