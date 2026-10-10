from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_ROOT.parent
for path in (PLUGIN_ROOT, WORKSPACE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from devtools.astrbot_stub import install


@pytest.fixture(scope="module")
def split_api():
    with install() as modules:
        class BaseMessageComponent:
            pass

        class Plain(BaseMessageComponent):
            def __init__(self, text="", **kwargs):
                self.text = text

        class At(BaseMessageComponent):
            def __init__(self, qq="", name="", **kwargs):
                self.qq = qq
                self.name = name

        class Reply(BaseMessageComponent):
            def __init__(self, **kwargs):
                pass

        class Image(BaseMessageComponent):
            def __init__(self, **kwargs):
                pass

        class Face(BaseMessageComponent):
            def __init__(self, id=None, **kwargs):
                self.id = id

        for module_name in (
            "astrbot.api.message_components",
            "astrbot.core.message.components",
        ):
            module = modules[module_name]
            for name, value in {
                "BaseMessageComponent": BaseMessageComponent,
                "Plain": Plain,
                "At": At,
                "Reply": Reply,
                "Image": Image,
                "Face": Face,
            }.items():
                setattr(module, name, value)

        import core

        step_package = types.ModuleType("core.step")
        step_package.__path__ = [str(PLUGIN_ROOT / "core" / "step")]
        sys.modules["core.step"] = step_package
        core.step = step_package

        from core.config import SplitConfig
        from core.model import OutContext
        from core.step.split import Segment, SplitStep

        class FakeEvent:
            def __init__(self, platform_name: str = "aiocqhttp"):
                self.platform_name = platform_name

            def get_platform_name(self) -> str:
                return self.platform_name

        def make_config(char_list: list[str], max_count: int = 0) -> SplitConfig:
            return SplitConfig(
                {
                    "max_length": 0,
                    "char_list": char_list,
                    "max_count": max_count,
                    "per_char_delay": 0.1,
                    "delay_scope_str": "1~2",
                    "show_typing": False,
                    "tail_punc": [],
                }
            )

        def make_context(chain: list[BaseMessageComponent]) -> OutContext:
            return OutContext(
                event=FakeEvent(),
                chain=chain,
                is_llm=True,
                plain="",
                gid="",
                uid="1",
                bid="100",
                group=SimpleNamespace(),
                timestamp=0,
            )

        def make_step(config: SplitConfig):
            sent = []

            async def send_message(*args, **kwargs):
                sent.append((args, kwargs))

            step = SplitStep(
                SimpleNamespace(
                    split=config,
                    context=SimpleNamespace(send_message=send_message),
                )
            )
            return step, sent

        yield SimpleNamespace(
            Plain=Plain,
            At=At,
            Reply=Reply,
            Segment=Segment,
            SplitStep=SplitStep,
            make_config=make_config,
            make_context=make_context,
            make_step=make_step,
        )


@pytest.mark.parametrize("separator", [" ", "\t", "\u3000", "\n", "\r\n", "\r"])
def test_backslash_s_splits_all_whitespace(split_api, separator):
    config = split_api.make_config(["\\s"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain(f"你好{separator}世界")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert ctx.split_segments is not None
    assert [[component.text for component in segment] for segment in ctx.split_segments] == [
        ["你好"],
        ["世界"],
    ]


def test_english_word_space_stays_protected(split_api):
    config = split_api.make_config(["\\s"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain("hello world")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert ctx.split_segments is None
    assert [component.text for component in ctx.chain] == ["hello world"]


def test_leading_whitespace_does_not_create_empty_segment(split_api):
    config = split_api.make_config(["\\s"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain("  你好 世界")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert [[component.text for component in segment] for segment in ctx.split_segments] == [
        ["你好"],
        ["世界"],
    ]


def test_literal_backslash_n_still_splits(split_api):
    config = split_api.make_config(["\\n"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain("a\\nb")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert ctx.split_segments is not None
    assert [[component.text for component in segment] for segment in ctx.split_segments] == [
        ["a"],
        ["b"],
    ]


def test_question_mark_is_not_an_implicit_split(split_api):
    config = split_api.make_config(["\\s", "\\n"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain("a?b")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert ctx.split_segments is None
    assert [component.text for component in ctx.chain] == ["a?b"]


def test_max_count_zero_is_unlimited(split_api):
    config = split_api.make_config(["\\s"], max_count=0)
    step, _ = split_api.make_step(config)
    segments = step._split_chain([split_api.Plain("一 二 三 四")])

    assert len(segments) == 4


def test_max_count_two_limits_segments(split_api):
    config = split_api.make_config(["\\s"], max_count=2)
    step, _ = split_api.make_step(config)
    segments = step._split_chain([split_api.Plain("一 二 三 四")])

    assert len(segments) == 2


def test_strip_plain_cleans_empty_and_edges(split_api):
    segment = split_api.Segment(
        [
            split_api.At(qq="1"),
            split_api.Plain("\u200b"),
            split_api.Plain(" \n\\n foo \n"),
            split_api.Plain("   "),
            split_api.Reply(),
        ]
    )

    segment.strip_plain()

    assert [type(component).__name__ for component in segment.components] == [
        "At",
        "Plain",
        "Reply",
    ]
    assert segment.components[1].text == " foo"


def test_handle_does_not_send_and_backfills_full_chain(split_api):
    config = split_api.make_config(["\\s"])
    step, sent = split_api.make_step(config)
    ctx = split_api.make_context([split_api.Plain("你好 世界 再见")])

    asyncio.run(step.handle(ctx))

    assert sent == []
    assert len(ctx.split_segments) == 3
    assert len(ctx.split_delays) == 2
    assert [component.text for component in ctx.chain] == ["你好", "世界", "再见"]
