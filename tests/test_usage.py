from __future__ import annotations

import json

import pytest

from agh_lite.usage import (
    UsageReadError,
    find_usage_file,
    load_usage,
    summarize,
)


def write_usage(path, events, **extra) -> None:
    payload = {"date": "2026-11-24", "todayUsage": 0, "events": events, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_and_summarize(tmp_path) -> None:
    path = tmp_path / "usage.json"
    write_usage(
        path,
        [
            {"ts": 1_792_900_000_000, "day": "2026-11-23", "model": "m1", "cost": 0.5, "tokens": 1000},
            {"ts": 1_792_950_000_000, "day": "2026-11-23", "model": "m1", "cost": 0.25, "tokens": 500},
            {"ts": 1_792_980_000_000, "day": "2026-11-24", "model": "m2", "cost": 0.75, "tokens": 2000},
        ],
    )
    events = load_usage(path)
    assert len(events) == 3
    summary = summarize(events)
    assert summary.events == 3
    assert summary.total_cost == pytest.approx(1.5)
    assert summary.total_tokens == 3500
    assert summary.by_day["2026-11-23"] == pytest.approx(0.75)
    assert summary.tokens_by_day["2026-11-23"] == 1500
    assert summary.tokens_by_day["2026-11-24"] == 2000
    assert summary.by_model["m2"]["tokens"] == 2000
    assert summary.avg_cost_per_million == pytest.approx(1.5 / 3500 * 1_000_000)


def test_since_filter(tmp_path) -> None:
    path = tmp_path / "usage.json"
    write_usage(
        path,
        [
            {"ts": 1000, "day": "old", "model": "m", "cost": 1.0, "tokens": 10},
            {"ts": 5000, "day": "new", "model": "m", "cost": 2.0, "tokens": 20},
        ],
    )
    summary = summarize(load_usage(path), since_ms=2000)
    assert summary.events == 1
    assert summary.total_cost == pytest.approx(2.0)


def test_bad_events_are_skipped(tmp_path) -> None:
    path = tmp_path / "usage.json"
    write_usage(path, [{"ts": "x", "cost": 1}, {"ts": 1, "cost": 1, "model": "m", "tokens": 2}])
    events = load_usage(path)
    assert len(events) == 1


def test_missing_events_key_raises(tmp_path) -> None:
    path = tmp_path / "usage.json"
    path.write_text('{"date": "x"}', encoding="utf-8")
    with pytest.raises(UsageReadError):
        load_usage(path)


def test_find_usage_file(tmp_path) -> None:
    assert find_usage_file(tmp_path) is None
    (tmp_path / ".dshw-usage.json").write_text("{}", encoding="utf-8")
    assert find_usage_file(tmp_path) is not None
