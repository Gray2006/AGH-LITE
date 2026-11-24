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


def test_usage_reconciliation(tmp_path, write_session) -> None:
    write_session()
    now_ms = int(__import__("time").time() * 1000)
    usage = {
        "events": [
            {"ts": now_ms - 1_000, "day": "2026-11-24", "model": "deepseek-v4", "cost": 0.01, "tokens": 6_150}
        ]
    }
    (tmp_path / ".dshw-usage.json").write_text(json.dumps(usage), encoding="utf-8")
    report = audit_home(tmp_path)
    assert report.usage is not None
    assert report.usage.events == 1
    assert report.usage.total_cost == pytest.approx(0.01)
    assert report.estimate_delta_usd is not None
    text = report.render_markdown()
    assert "与真实账单对账" in text
    assert "实际花费" in text
    assert report.calibration_factor is not None
    assert report.calibration_factor > 1
    assert "按真实账单校准" in text
    page = report.render_html()
    assert "<svg" in page
    assert "按天用量" in page


def test_usage_can_be_disabled(tmp_path, write_session) -> None:
    write_session()
    (tmp_path / ".dshw-usage.json").write_text('{"events": []}', encoding="utf-8")
    assert audit_home(tmp_path, usage=False).usage is None


def test_days_limit_applies_after_time_filter(tmp_path, write_session) -> None:
    now_ms = int(__import__("time").time() * 1000)
    for index, offset in enumerate((3_600_000, 7_200_000)):
        events = [dict(event) for event in build_events()]
        for event in events:
            event["time"] = now_ms - offset
        write_session(events, session_id=f"recent-{index}")
    write_session(session_id="old")
    report = audit_home(tmp_path, days=1, limit=1)
    assert report.totals.sessions == 1
    assert "recent-0" in report.sessions[0].path


def _unstable_events() -> list[dict]:
    events: list[dict] = [
        {"type": "session", "time": 1_000, "data": {"id": "sess-u", "cwd": "G:\\work"}},
        {
            "type": "request/header",
            "time": 1_100,
            "data": {"header": {"config": {"provider": "p", "model": "m"}, "tools": [{"name": "a"}]}},
        },
        {"type": "system/message", "time": 1_200, "data": {"message": {"role": "system", "content": "s0"}}},
    ]
    for step in range(1, 11):
        events.append(
            {
                "type": "assistant/message",
                "time": 2_000 + step,
                "data": {
                    "turn": 1,
                    "step": step,
                    "usage": {"inputTokens": 1_000, "cacheReadTokens": 100, "outputTokens": 10},
                    "message": {"content": [{"type": "text", "text": "x"}]},
                },
            }
        )
        events.append(
            {
                "type": "request/header",
                "time": 3_000 + step,
                "data": {
                    "header": {
                        "config": {"provider": "p", "model": "m"},
                        "tools": [{"name": f"t{step}"}],
                    }
                },
            }
        )
        events.append(
            {
                "type": "system/message",
                "time": 4_000 + step,
                "data": {"message": {"role": "system", "content": f"s{step}"}},
            }
        )
    return events


def test_unstable_prefix_rules(tmp_path, write_session) -> None:
    write_session(_unstable_events(), session_id="unstable")
    report = audit_home(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert "unstable_tools" in codes
    assert "unstable_system" in codes


def test_workspace_rows(tmp_path, write_session) -> None:
    first = build_events()
    first[0]["data"]["cwd"] = "G:\\alpha"
    second = build_events()
    second[0]["data"]["cwd"] = "G:\\beta"
    write_session(first, session_id="a")
    write_session(second, session_id="b")
    report = audit_home(tmp_path)
    workspaces = {row["workspace"]: row for row in report.workspace_rows()}
    assert set(workspaces) == {"G:\\alpha", "G:\\beta"}
    assert sum(row["sessions"] for row in workspaces.values()) == 2
    text = report.render_markdown()
    assert "按 workspace" in text


def test_volatile_system_prompt_rule(tmp_path, write_session) -> None:
    events = _unstable_events()
    # 把 system 变化改成「只变时间戳」：语义不变，但每步原始文本不同
    index = 0
    for event in events:
        if event["type"] == "system/message":
            index += 1
            event["data"]["message"]["content"] = f"规则不变\n现在 2026-11-11 19:30:{index:02d}"
    write_session(events, session_id="volatile")
    report = audit_home(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert "volatile_system_prompt" in codes


def test_html_report_is_self_contained(tmp_path, write_session) -> None:
    write_session()
    report = audit_home(tmp_path)
    page = report.render_html()
    assert page.startswith("<!doctype html>")
    assert "<table>" in page
    assert "<script" not in page
    assert "DSH Token 审计报告" in page
