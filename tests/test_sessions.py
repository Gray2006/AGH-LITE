from __future__ import annotations

import json

import pytest

from agh_lite.sessions import (
    SESSION_SUFFIXES,
    SessionReadError,
    find_session_files,
    iter_events,
    load_session,
    resolve_dsh_home,
)
from conftest import build_events


def test_load_session_totals(write_session) -> None:
    record = load_session(write_session())
    assert record.session_id == "sess-1"
    assert record.cwd == "G:\\work"
    assert record.model == "deepseek-v4"
    assert record.context_window == 131_072
    assert record.turns == 1
    assert len(record.steps) == 2
    assert record.prompt_tokens == 6_000
    assert record.output_tokens == 150
    assert record.total_tokens == 6_150
    assert record.cache_hit_ratio == pytest.approx(4_500 / 6_000)
    assert record.duration_ms == 1_400
    assert record.started_at == 1_000
    assert record.ended_at == 2_400
    assert record.system_chars > 0
    assert record.tool_schema_chars > 0
    assert len(record.tool_names) == 2


def test_steps_carry_model_and_model_usage(write_session) -> None:
    record = load_session(write_session())
    assert all(step.model == "deepseek-v4" for step in record.steps)
    usage = record.model_usage()
    assert usage["deepseek-v4"] == (1_500, 4_500, 150, 6_150)


def test_duplicate_calls_and_repeated_reads(write_session) -> None:
    record = load_session(write_session())
    duplicates = record.duplicate_calls()
    assert len(duplicates) == 1
    assert duplicates[0][1] == 2
    assert duplicates[0][2] > 0
    reads = record.repeated_file_reads()
    assert reads[0][0] == "a.py"
    assert reads[0][1] == 2


def test_tool_results_and_errors(write_session) -> None:
    record = load_session(write_session())
    assert len(record.tool_results) == 4  # c4 没有对应 tool/call，但仍计入结果
    assert record.error_results == 1
    assert record.tool_result_chars > 40_000


def test_compression_calls_counted(write_session) -> None:
    events = [
        {"type": "tool/call", "time": 1, "data": {"callId": "z", "name": "compress", "arguments": "{}"}},
    ]
    record = load_session(write_session(events, name="c.jsonl", session_id="sess-c"))
    assert record.compression_calls == 1


def test_auto_compaction_counted_and_system_is_max(write_session) -> None:
    events = [
        {"type": "system/message", "time": 1, "data": {"message": {"content": "x" * 100}}},
        {"type": "system/message", "time": 2, "data": {"message": {"content": "y" * 300}}},
        {"type": "compaction/start", "time": 3, "data": {"compactionId": "c1"}},
        {"type": "compaction/end", "time": 4, "data": {"compactionId": "c1"}},
    ]
    record = load_session(write_session(events, name="d.jsonl", session_id="sess-d"))
    assert record.compactions == 1
    assert record.system_chars == 300


def test_bad_lines_are_skipped(write_session) -> None:
    path = write_session(build_events())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n\n")
    record = load_session(path)
    assert record.total_tokens == 6_150


def test_zstd_session_is_read(write_session) -> None:
    pytest.importorskip("zstandard")
    record = load_session(write_session(zstd=True))
    assert record.total_tokens == 6_150


def test_multi_frame_zstd_session_is_read(tmp_path) -> None:
    zstandard = pytest.importorskip("zstandard")
    lines = [json.dumps(event, ensure_ascii=False) for event in build_events()]
    # 真实会话可能分多帧写入：前两行一帧，其余一帧
    first = zstandard.ZstdCompressor().compress(("\n".join(lines[:2]) + "\n").encode("utf-8"))
    second = zstandard.ZstdCompressor().compress(("\n".join(lines[2:]) + "\n").encode("utf-8"))
    base = tmp_path / "sessions" / "ws" / "multi"
    base.mkdir(parents=True)
    path = base / "session.v4.jsonl.zstd"
    path.write_bytes(first + second)
    record = load_session(path)
    assert record.total_tokens == 6_150


def test_iter_events_reads_plain(write_session) -> None:
    events = list(iter_events(write_session()))
    assert events[0]["type"] == "session"


def test_find_session_files_sorted_and_limited(write_session) -> None:
    first = write_session(name="a.jsonl", session_id="s1")
    second = write_session(name="b.jsonl", session_id="s2")
    root = first.parents[2]
    files = find_session_files(root)
    assert set(files) == {first, second}
    assert len(find_session_files(root, limit=1)) == 1
    with pytest.raises(ValueError):
        find_session_files(root, limit=0)
    small = find_session_files(root, max_bytes=1)
    assert small == []


def test_find_session_files_rejects_non_home(tmp_path) -> None:
    with pytest.raises(SessionReadError):
        find_session_files(tmp_path)


def test_resolve_dsh_home_explicit(write_session) -> None:
    write_session()
    sessions_dir = next(p for p in write_session().parents if p.name == "sessions")
    assert resolve_dsh_home(sessions_dir) == sessions_dir.parent


def test_resolve_dsh_home_missing(tmp_path) -> None:
    with pytest.raises(SessionReadError):
        resolve_dsh_home(tmp_path)


def test_suffixes() -> None:
    assert ".jsonl.zstd" in SESSION_SUFFIXES
    assert ".jsonl.zst" in SESSION_SUFFIXES


def test_describe_contains_tokens(write_session) -> None:
    record = load_session(write_session())
    text = record.describe()
    assert "6.15K" in text or "tokens" in text


def test_header_and_system_changes_are_counted(write_session, events) -> None:
    extra = list(events)
    extra.append(
        {
            "type": "request/header",
            "time": 2_500,
            "data": {
                "header": {
                    "config": {"provider": "deepseek", "model": "deepseek-v4"},
                    "tools": [{"name": "new"}],
                }
            },
        }
    )
    extra.append(
        {
            "type": "system/message",
            "time": 2_600,
            "data": {"message": {"role": "system", "content": "新提示词"}},
        }
    )
    record = load_session(write_session(extra))
    assert record.header_changes == 1
    assert record.system_changes == 1


def test_duplicate_calls_need_same_result(write_session, events) -> None:
    # 同一个 read 调两次，但结果不同：不算重复浪费
    changed = []
    for event in events:
        if event["type"] == "tool/result" and event["data"]["message"]["toolCallId"] == "c2":
            event = {
                "type": "tool/result",
                "time": event["time"],
                "data": {"message": {"toolCallId": "c2", "content": [{"type": "text", "text": "不同的结果"}]}},
            }
        changed.append(event)
    record = load_session(write_session(changed))
    assert record.duplicate_calls() == []


def test_system_tokens_use_cjk_profile(write_session) -> None:
    record = load_session(write_session())
    # 合成会话的 system 是中文，按 CJK 口径应明显高于 chars/3.6
    assert record.system_tokens > record.system_chars / 3.6


def test_volatile_system_changes_are_separated(write_session, events) -> None:
    base = events[2]["data"]["message"]["content"]
    extra = list(events)
    extra.append(
        {
            "type": "system/message",
            "time": 2_600,
            "data": {"message": {"role": "system", "content": base + "\n现在 2026-11-11 19:30:38"}},
        }
    )
    record = load_session(write_session(extra))
    assert record.system_changes == 0  # 只有时间戳变化
    assert record.system_volatile_changes == 1
