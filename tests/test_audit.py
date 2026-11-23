from __future__ import annotations

import json

import pytest

from agh_lite.audit import OVERSIZED_RESULT_TOKENS, audit_home, audit_records
from agh_lite.sessions import load_session
from conftest import build_events


def test_audit_home_totals(tmp_path, write_session) -> None:
    write_session()
    report = audit_home(tmp_path)
    assert report.totals.sessions == 1
    assert report.totals.total_tokens == 6_150
    assert report.totals.prompt_tokens == 6_000
    assert report.totals.cache_read_tokens == 4_500
    assert report.totals.output_tokens == 150
    assert report.totals.error_results == 1
    assert report.days is None


def test_audit_finds_duplicates_and_oversized(tmp_path, write_session) -> None:
    write_session()
    report = audit_home(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert "duplicate_tool_calls" in codes
    assert "oversized_tool_outputs" in codes
    assert "repeated_file_reads" in codes


def test_potential_savings_is_capped(tmp_path, write_session) -> None:
    write_session()
    report = audit_home(tmp_path)
    assert report.potential_savings <= int(report.totals.total_tokens * 0.25)


def test_markdown_renders_chinese_report(tmp_path, write_session) -> None:
    write_session()
    text = audit_home(tmp_path).render_markdown()
    assert "# DSH Token 审计报告" in text
    assert "## 口径与边界" in text
    assert "重复" in text


def test_json_roundtrip(tmp_path, write_session) -> None:
    write_session()
    payload = json.loads(json.dumps(audit_home(tmp_path).to_dict(), ensure_ascii=False))
    assert payload["totals"]["sessions"] == 1
    assert payload["findings"]
    assert payload["top_sessions"][0]["session_id"] == "sess-1"


def test_empty_home_has_no_findings(tmp_path) -> None:
    (tmp_path / "sessions").mkdir()
    report = audit_home(tmp_path)
    assert report.totals.sessions == 0
    assert report.findings == []
    assert "无数据" in report.render_markdown()


def test_broken_zstd_is_skipped(tmp_path) -> None:
    base = tmp_path / "sessions" / "ws" / "bad"
    base.mkdir(parents=True)
    (base / "session.v4.jsonl.zstd").write_bytes(b"not zstd")
    report = audit_home(tmp_path)
    assert report.totals.sessions == 0
    assert report.skipped


def test_low_cache_and_static_prefix(tmp_path, write_session) -> None:
    # 构造一个 prompt 很大、缓存命中低、静态前缀很重的会话
    events = [
        {"type": "session", "time": 1, "data": {"id": "big", "cwd": "x", "createdAt": 1}},
        {
            "type": "request/header",
            "time": 2,
            "data": {
                "header": {
                    "config": {"model": "m"},
                    "tools": [{"name": "t", "description": "d" * 60_000}],
                }
            },
        },
        {"type": "system/message", "time": 3, "data": {"message": {"content": "s" * 60_000}}},
        {
            "type": "assistant/message",
            "time": 4,
            "data": {
                "turn": 1,
                "step": 1,
                "usage": {
                    "inputTokens": 90_000,
                    "cacheReadTokens": 10_000,
                    "outputTokens": 500,
                    "totalTokens": 100_500,
                },
                "message": {"content": "ok"},
            },
        },
    ]
    write_session(events, session_id="big")
    report = audit_home(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert "low_prefix_cache" in codes
    assert "static_prefix_bloat" in codes


def test_oversized_threshold_constant() -> None:
    assert OVERSIZED_RESULT_TOKENS == 8_000


def test_audit_records_directly(tmp_path, write_session) -> None:
    record = load_session(write_session())
    report = audit_records([record], home=tmp_path, days=7)
    assert report.totals.sessions == 1
    assert report.days == 7


def test_audit_costs_and_model_breakdown(tmp_path, write_session) -> None:
    write_session()
    report = audit_home(tmp_path)
    assert report.totals.has_cost
    assert report.totals.priced_tokens == 6_150
    # deepseek-v4：1500 未命中 + 4500 缓存 + 150 输出
    expected = (1_500 * 0.55 + 4_500 * 0.055 + 150 * 1.70) / 1_000_000
    assert report.totals.cost_usd == pytest.approx(expected)
    assert report.totals.model_usage["deepseek-v4"]["total"] == 6_150
    assert report.potential_savings_usd is not None
    assert all(finding.estimated_usd is not None for finding in report.findings if finding.estimated_tokens)


def test_unknown_model_is_not_priced(tmp_path, write_session) -> None:
    events = [
        {"type": "session", "time": 1, "data": {"id": "x", "cwd": "x", "createdAt": 1}},
        {
            "type": "request/header",
            "time": 2,
            "data": {"header": {"config": {"model": "totally-unknown"}}},
        },
        {
            "type": "assistant/message",
            "time": 3,
            "data": {
                "turn": 1,
                "step": 1,
                "usage": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
                "message": {"content": "ok"},
            },
        },
    ]
    write_session(events, session_id="unknown")
    report = audit_home(tmp_path)
    assert not report.totals.has_cost
    assert "totally-unknown" in report.totals.unknown_models
    assert report.potential_savings_usd is None


def test_days_uses_session_event_time(tmp_path, write_session) -> None:
    now_ms = int(__import__("time").time() * 1000)
    write_session(session_id="old")  # fixture 的事件时间在 1970 年
    recent = [dict(event) for event in build_events()]
    for event in recent:
        event["time"] = now_ms - 60_000
    write_session(recent, session_id="recent")
    report = audit_home(tmp_path, days=1)
    assert report.totals.sessions == 1
    assert report.sessions[0].session_id == "sess-1"
