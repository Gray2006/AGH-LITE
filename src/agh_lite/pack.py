"""上下文打包：在预算内保留高价值片段，先去掉重复。

个人用 DSH 最贵的不是「问得难」，是同一段内容被读第二遍、第三遍。
``pack`` 做三件事：
1. 精确去重（规范化空白后逐字相同）；
2. 近似去重（字符 6-gram Jaccard ≥ 阈值）；
3. 按优先级 + 新旧程度贪心装包，钉住的片段必留。

它不总结、不改写，只做选择；被丢掉的片段带原因返回，方便复核。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .tokens import DEFAULT_PROFILE, TokenProfile, estimate_text_tokens, format_tokens

ROLE_ALIASES = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "tool": "user",
    "context": "user",
    "memory": "user",
    "file": "user",
    "note": "user",
}

_KIND_WEIGHT = {
    "system": 0.45,
    "user": 0.25,
    "memory": 0.15,
    "file": 0.0,
    "tool": -0.10,
    "note": 0.0,
}


@dataclass
class Segment:
    id: str
    text: str
    role: str = "context"
    kind: str = "note"
    priority: float = 0.5
    pinned: bool = False
    timestamp: float | None = None
    tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("segment.id 不能为空")
        if self.priority < 0 or self.priority > 1:
            raise ValueError(f"priority 应在 [0,1]: {self.priority}")


@dataclass(frozen=True)
class Dropped:
    id: str
    reason: str
    tokens: int


@dataclass
class PackResult:
    segments: list[Segment]
    dropped: list[Dropped]
    total_tokens: int
    budget: int
    over_budget: bool = False
    duplicates: int = 0

    @property
    def kept(self) -> int:
        return len(self.segments)

    def render(self, separator: str = "\n\n") -> str:
        return separator.join(segment.text for segment in self.segments)

    def to_messages(self) -> list[dict[str, str]]:
        return [
            {"role": ROLE_ALIASES.get(segment.role, "user"), "content": segment.text}
            for segment in self.segments
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "dropped": len(self.dropped),
            "duplicates": self.duplicates,
            "total_tokens": self.total_tokens,
            "budget": self.budget,
            "over_budget": self.over_budget,
            "kept_ids": [segment.id for segment in self.segments],
            "dropped_ids": [item.id for item in self.dropped],
        }


class PackBudgetExceeded(Exception):
    def __init__(self, result: PackResult) -> None:
        super().__init__(
            f"钉住/受保护片段已超预算：{result.total_tokens} > {result.budget} tokens"
        )
        self.result = result


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _shingles(text: str, size: int = 6, max_shingles: int = 4000) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= size:
        return {compact} if compact else set()
    step = max(1, math.ceil((len(compact) - size + 1) / max_shingles))
    return {compact[i : i + size] for i in range(0, len(compact) - size + 1, step)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe(
    segments: Sequence[Segment], *, threshold: float = 0.85, max_near_checks: int = 600
) -> tuple[list[Segment], list[Dropped]]:
    """按输入顺序去重，保留第一次出现的片段。"""
    if not 0 <= threshold <= 1:
        raise ValueError("threshold 应在 [0,1]")
    kept: list[Segment] = []
    dropped: list[Dropped] = []
    exact: dict[str, str] = {}
    representatives: list[tuple[str, set[str]]] = []

    for segment in segments:
        normalized = _normalize(segment.text)
        if normalized in exact:
            dropped.append(Dropped(segment.id, f"duplicate:{exact[normalized]}", segment.tokens))
            continue
        if len(normalized) >= 160 and len(representatives) < max_near_checks:
            shingles = _shingles(normalized)
            duplicate_of = None
            for other_id, other_shingles in representatives:
                if _jaccard(shingles, other_shingles) >= threshold:
                    duplicate_of = other_id
                    break
            if duplicate_of is not None:
                dropped.append(Dropped(segment.id, f"near_duplicate:{duplicate_of}", segment.tokens))
                continue
        exact[normalized] = segment.id
        if len(normalized) >= 160:
            representatives.append((segment.id, _shingles(normalized)))
        kept.append(segment)
    return kept, dropped


def pack(
    segments: Iterable[Segment],
    budget: int,
    *,
    protect_last: int = 1,
    threshold: float = 0.85,
    strict: bool = False,
    profile: TokenProfile | None = None,
) -> PackResult:
    """按预算装包。

    - ``system`` / ``pinned`` / 最后 ``protect_last`` 条先占预算；
    - 其余按优先级从高到低装入，装不下即丢弃；
    - ``strict=True`` 时，受保护片段本身超预算就抛 ``PackBudgetExceeded``。
    """
    if budget <= 0:
        raise ValueError("budget 必须为正")
    profile = profile or DEFAULT_PROFILE
    original = list(segments)
    for segment in original:
        if segment.tokens <= 0:
            segment.tokens = estimate_text_tokens(segment.text, profile)

    unique, duplicates = dedupe(original, threshold=threshold)
    dropped: list[Dropped] = list(duplicates)

    protected_ids: set[str] = set()
    for segment in unique:
        if segment.pinned or segment.kind == "system" or segment.role == "system":
            protected_ids.add(segment.id)
    if protect_last > 0:
        for segment in unique[-protect_last:]:
            protected_ids.add(segment.id)

    protected = [segment for segment in unique if segment.id in protected_ids]
    candidates = [segment for segment in unique if segment.id not in protected_ids]
    protected_tokens = sum(segment.tokens for segment in protected)
    over_budget = protected_tokens > budget

    if over_budget:
        selected = list(protected)
        dropped.extend(
            Dropped(segment.id, "budget", segment.tokens) for segment in candidates
        )
    else:
        stamps = [s.timestamp for s in unique if s.timestamp is not None]
        selected = _greedy(protected, candidates, budget, stamps)
        selected_ids = {segment.id for segment in selected}
        dropped.extend(
            Dropped(segment.id, "budget", segment.tokens)
            for segment in candidates
            if segment.id not in selected_ids
        )

    used = sum(segment.tokens for segment in selected)
    selected_ids = {segment.id for segment in selected}
    ordered = [segment for segment in original if segment.id in selected_ids]
    result = PackResult(
        segments=ordered,
        dropped=dropped,
        total_tokens=used,
        budget=budget,
        over_budget=over_budget,
        duplicates=len(duplicates),
    )
    if over_budget and strict:
        raise PackBudgetExceeded(result)
    return result


def _greedy(
    protected: Sequence[Segment],
    candidates: Sequence[Segment],
    budget: int,
    stamps: Sequence[float],
) -> list[Segment]:
    if stamps:
        low, high = min(stamps), max(stamps)
        span = high - low if high > low else 1.0
    else:
        low = high = 0.0
        span = 1.0

    def score(segment: Segment) -> tuple[float, float, float]:
        recency = 0.0
        if segment.timestamp is not None and stamps:
            # 半衰期衰减（借 AGH forgetting 的思路）：最新 0.3，一个时间跨度前减半
            age = (high - segment.timestamp) / span
            recency = 0.3 * (0.5**age)
        value = segment.priority + _KIND_WEIGHT.get(segment.kind, 0.0) + recency
        # 同分时短片段优先，减少被一个巨块吃掉整包的概率
        return (value, -segment.tokens, 0.0)

    selected = list(protected)
    used = sum(segment.tokens for segment in protected)
    for segment in sorted(candidates, key=score, reverse=True):
        if used + segment.tokens > budget:
            continue
        selected.append(segment)
        used += segment.tokens
    return selected


def describe(result: PackResult) -> str:
    lines = [
        f"预算 {format_tokens(result.budget)} tokens，装入 {result.kept} 段共 "
        f"{format_tokens(result.total_tokens)} tokens（去重 {result.duplicates} 段，丢弃 {len(result.dropped)} 段）"
    ]
    for item in result.dropped:
        lines.append(f"  - {item.id}: {item.reason} ({format_tokens(item.tokens)})")
    return "\n".join(lines)
