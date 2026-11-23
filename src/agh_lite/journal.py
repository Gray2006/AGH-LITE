"""哈希链账本。

个人版只记「真的发生了什么」：缓存命中、预算结算、打包、审计。
每条记录带前一条的哈希，事后能查有没有被悄悄改过。
不提供时间戳证明，也不防有写权限的人重写整条链——它防的是「改一条看不出来」。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest(prev: str, body: str) -> str:
    return hashlib.sha256(f"{prev}\n{body}".encode()).hexdigest()


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    ts: int
    kind: str
    payload: dict[str, Any]
    prev: str
    hash: str

    def body(self) -> str:
        return _canonical(
            {"seq": self.seq, "ts": self.ts, "kind": self.kind, "payload": self.payload, "prev": self.prev}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "payload": self.payload,
            "prev": self.prev,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int
    broken_at: int | None = None
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return f"链完整：{self.count} 条记录"
        return f"链断裂于第 {self.broken_at} 条：{self.reason}"


class JournalCorrupt(Exception):
    pass


class HashChainJournal:
    def __init__(self, path: str | Path, *, clock: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._seq = 0
        self._prev = GENESIS
        self._warm_up()

    def _warm_up(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        last: JournalEntry | None = None
        for entry in self._read_entries(strict=True):
            last = entry
        if last is not None:
            self._seq = last.seq + 1
            self._prev = last.hash

    def _read_entries(self, *, strict: bool = False) -> Iterator[JournalEntry]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    entry = JournalEntry(
                        seq=int(raw["seq"]),
                        ts=int(raw["ts"]),
                        kind=str(raw["kind"]),
                        payload=dict(raw["payload"]),
                        prev=str(raw["prev"]),
                        hash=str(raw["hash"]),
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    if strict:
                        raise JournalCorrupt(f"第 {line_no} 行无法解析: {exc}") from exc
                    continue
                yield entry

    def append(self, kind: str, payload: dict[str, Any] | None = None) -> JournalEntry:
        if not kind:
            raise ValueError("kind 不能为空")
        entry = JournalEntry(
            seq=self._seq,
            ts=int(self._clock()),
            kind=kind,
            payload=dict(payload or {}),
            prev=self._prev,
            hash="",
        )
        digest = _digest(entry.prev, entry.body())
        entry = JournalEntry(**{**entry.as_dict(), "hash": digest})
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(entry.as_dict()) + "\n")
        self._seq += 1
        self._prev = digest
        return entry

    def entries(self, limit: int | None = None) -> list[JournalEntry]:
        all_entries = list(self._read_entries())
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit 必须为正")
            return all_entries[-limit:]
        return all_entries

    def verify(self) -> VerifyResult:
        prev = GENESIS
        count = 0
        for expected_seq, entry in enumerate(self._read_entries()):
            count += 1
            if entry.prev != prev:
                return VerifyResult(False, count, entry.seq, "prev 与上一条哈希不一致")
            if entry.seq != expected_seq:
                return VerifyResult(False, count, entry.seq, f"序号不连续（期望 {expected_seq}）")
            recomputed = _digest(entry.prev, entry.body())
            if recomputed != entry.hash:
                return VerifyResult(False, count, entry.seq, "记录哈希与内容不符")
            prev = entry.hash
        return VerifyResult(True, count)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        entries = self.entries()
        for entry in entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return {"entries": len(entries), "kinds": counts, "last_hash": self._prev}
