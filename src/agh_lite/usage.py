"""DSH 真实用量账本对账。

``$DSH_HOME/.dshw-usage.json`` 是 DSH 自己记的逐笔花费，结构（实测）：

    {"date": "...", "lastBalance": ..., "todayUsage": ...,
     "history": {"2026-10-25": 34.60, ...},
     "events": [{"ts": 1792901103344, "day": "2026-10-25",
                 "model": "deepseek-flash", "cost": 0.01406408, "tokens": 327994}]}

本模块只读它，把事件按时间/模型聚合，用来和审计的估算费用对账。
字段缺失时跳过该事件，不猜。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USAGE_FILENAME = ".dshw-usage.json"


class UsageReadError(Exception):
    pass


@dataclass(frozen=True)
class UsageEvent:
    ts: int
    day: str
    model: str
    cost: float
    tokens: int


@dataclass
class UsageSummary:
    events: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    by_day: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    skipped: int = 0

    @property
    def avg_cost_per_million(self) -> float:
        return self.total_cost / self.total_tokens * 1_000_000 if self.total_tokens else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "total_cost": round(self.total_cost, 6),
            "total_tokens": self.total_tokens,
            "by_day": {day: round(cost, 6) for day, cost in sorted(self.by_day.items())},
            "by_model": {
                model: {
                    "cost": round(row["cost"], 6),
                    "tokens": int(row["tokens"]),
                    "events": int(row["events"]),
                }
                for model, row in sorted(self.by_model.items())
            },
            "skipped": self.skipped,
        }


def find_usage_file(home: str | Path) -> Path | None:
    """在 DSH 数据目录里找用量账本；不存在返回 None。"""
    candidate = Path(home) / USAGE_FILENAME
    return candidate if candidate.is_file() else None


def load_usage(path: str | Path) -> list[UsageEvent]:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageReadError(f"读不了用量账本：{path}（{exc}）") from exc
    raw_events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(raw_events, list):
        raise UsageReadError(f"用量账本里没有 events 列表：{path}")
    events: list[UsageEvent] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        cost = item.get("cost")
        ts = item.get("ts")
        if not isinstance(cost, (int, float)) or not isinstance(ts, (int, float)):
            continue
        tokens = item.get("tokens", 0)
        events.append(
            UsageEvent(
                ts=int(ts),
                day=str(item.get("day") or ""),
                model=str(item.get("model") or "unknown"),
                cost=float(cost),
                tokens=int(tokens) if isinstance(tokens, (int, float)) else 0,
            )
        )
    return events


def summarize(events: list[UsageEvent], *, since_ms: int | None = None) -> UsageSummary:
    summary = UsageSummary()
    for event in events:
        if since_ms is not None and event.ts < since_ms:
            continue
        summary.events += 1
        summary.total_cost += event.cost
        summary.total_tokens += event.tokens
        if event.day:
            summary.by_day[event.day] = summary.by_day.get(event.day, 0.0) + event.cost
        row = summary.by_model.setdefault(event.model, {"cost": 0.0, "tokens": 0.0, "events": 0.0})
        row["cost"] += event.cost
        row["tokens"] += event.tokens
        row["events"] += 1
    return summary
