from __future__ import annotations

import pytest

from agh_lite.pack import (
    PackBudgetExceeded,
    Segment,
    dedupe,
    describe,
    pack,
)


def test_exact_duplicate_is_dropped() -> None:
    kept, dropped = dedupe([Segment(id="a", text="hello world"), Segment(id="b", text=" hello   world ")])
    assert [segment.id for segment in kept] == ["a"]
    assert dropped[0].id == "b"
    assert dropped[0].reason.startswith("duplicate:")


def test_near_duplicate_is_dropped() -> None:
    base = "dsh 会话记录里有一半 token 花在重复读取同一个文件上" * 12
    near = base.replace("一半", "许多", 1)
    kept, dropped = dedupe(
        [Segment(id="a", text=base), Segment(id="b", text=near)], threshold=0.8
    )
    assert [segment.id for segment in kept] == ["a"]
    assert dropped[0].reason.startswith("near_duplicate:")


def test_distinct_texts_survive() -> None:
    kept, dropped = dedupe(
        [
            Segment(id="a", text="aa" * 300),
            Segment(id="b", text="你好" * 300),
        ]
    )
    assert len(kept) == 2 and not dropped


def test_pack_respects_budget_and_priority() -> None:
    segments = [
        Segment(id="low", text="x", tokens=60, priority=0.2),
        Segment(id="high", text="y", tokens=60, priority=0.9),
    ]
    result = pack(segments, budget=100, protect_last=0)
    assert [segment.id for segment in result.segments] == ["high"]
    assert result.total_tokens == 60
    assert [item.id for item in result.dropped] == ["low"]


def test_pack_prefers_recent_on_tie() -> None:
    segments = [
        Segment(id="old", text="a", tokens=50, priority=0.5, timestamp=1.0),
        Segment(id="new", text="b", tokens=50, priority=0.5, timestamp=9.0),
    ]
    result = pack(segments, budget=60, protect_last=0)
    assert [segment.id for segment in result.segments] == ["new"]


def test_pack_keeps_pinned_and_system() -> None:
    segments = [
        Segment(id="system", text="rules", kind="system", priority=0.0),
        Segment(id="pinned", text="must", pinned=True, priority=0.1),
        Segment(id="filler", text="f" * 400, tokens=100, priority=0.9),
    ]
    result = pack(segments, budget=40, protect_last=0)
    ids = [segment.id for segment in result.segments]
    assert "system" in ids and "pinned" in ids


def test_pack_protects_last() -> None:
    segments = [
        Segment(id="old", text="old", tokens=10, priority=0.0),
        Segment(id="new", text="new", tokens=10, priority=1.0),
    ]
    result = pack(segments, budget=10, protect_last=1)
    assert [segment.id for segment in result.segments] == ["new"]


def test_pack_preserves_input_order() -> None:
    segments = [
        Segment(id="a", text="a", tokens=10, priority=0.9),
        Segment(id="b", text="b", tokens=10, priority=0.1),
        Segment(id="c", text="c", tokens=10, priority=0.5),
    ]
    result = pack(segments, budget=20, protect_last=0)
    assert [segment.id for segment in result.segments] == ["a", "c"]


def test_pack_over_budget_protected_and_strict() -> None:
    segments = [Segment(id="sys", text="s", kind="system", tokens=50), Segment(id="x", text="x", tokens=10)]
    result = pack(segments, budget=20, protect_last=0)
    assert result.over_budget
    with pytest.raises(PackBudgetExceeded):
        pack(segments, budget=20, protect_last=0, strict=True)


def test_pack_counts_duplicates() -> None:
    segments = [Segment(id="a", text="same"), Segment(id="b", text="same"), Segment(id="c", text="other")]
    result = pack(segments, budget=100)
    assert result.duplicates == 1
    assert result.kept == 2


def test_pack_rejects_bad_budget() -> None:
    with pytest.raises(ValueError):
        pack([], budget=0)
    with pytest.raises(ValueError):
        Segment(id="", text="x")
    with pytest.raises(ValueError):
        Segment(id="a", text="x", priority=1.5)


def test_render_and_messages() -> None:
    result = pack([Segment(id="a", text="hello", role="user"), Segment(id="b", text="ctx", role="tool")], budget=100)
    assert result.render() == "hello\n\nctx"
    assert result.to_messages() == [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "ctx"},
    ]


def test_describe_mentions_budget() -> None:
    result = pack([Segment(id="a", text="hello")], budget=100)
    assert "预算" in describe(result)
