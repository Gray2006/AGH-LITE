"""内容寻址的响应缓存。

只做一件事：同一请求（模型 + 消息 + 参数）第二次出现时，不再付一次钱。
键是规范化 JSON 的 sha256，响应与用量一起落 SQLite。

边界（写进文档，别指望它更多）：
- 不保证语义等价，只保证输入逐字节规范化后一致；
- 有副作用的工具调用链不适合缓存，调用方自己判断；
- TTL 默认 7 天，过期即视为未命中。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pricing import ModelPrice, lookup_price

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _strip_volatile(value: Any) -> Any:
    """去掉不影响模型输出的字段，减少无谓的缓存未命中。"""
    if isinstance(value, Mapping):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in {"id", "created", "created_at", "timestamp", "cache_control"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


@dataclass(frozen=True)
class CacheEntry:
    key: str
    model: str
    response: Any
    prompt_tokens: int
    completion_tokens: int
    created_at: int
    hits: int


@dataclass(frozen=True)
class CacheStats:
    entries: int
    hits: int
    prompt_tokens_saved: int
    completion_tokens_saved: int
    db_bytes: int
    oldest_at: int | None
    newest_at: int | None
    saved_usd: float | None = None
    unpriced_models: list[str] = field(default_factory=list)


class ResponseCache:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int | None = 7 * 24 * 3600,
        clock: Any = None,
    ) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正或 None")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: int(time.time()))
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ------------------------------------------------------------ 生命周期
    def _migrate(self) -> None:
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS responses (
                    key TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT '',
                    response TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    last_hit_at INTEGER
                )
                """
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )
            self._db.execute(
                "INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ResponseCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 键
    def make_key(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any] | None = None,
        *,
        namespace: str = "chat",
    ) -> str:
        payload = {
            "v": SCHEMA_VERSION,
            "ns": namespace,
            "model": model,
            "messages": _strip_volatile(list(messages)),
            "params": _strip_volatile(dict(params or {})),
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------ 读写
    def get(self, key: str) -> CacheEntry | None:
        row = self._db.execute("SELECT * FROM responses WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        now = int(self._clock())
        if self.ttl_seconds is not None and now - int(row["created_at"]) > self.ttl_seconds:
            with self._db:
                self._db.execute("DELETE FROM responses WHERE key = ?", (key,))
            return None
        with self._db:
            self._db.execute(
                "UPDATE responses SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
                (now, key),
            )
        return CacheEntry(
            key=key,
            model=str(row["model"]),
            response=json.loads(row["response"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            created_at=int(row["created_at"]),
            hits=int(row["hits"]) + 1,
        )

    def put(
        self,
        key: str,
        response: Any,
        *,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        overwrite: bool = False,
    ) -> bool:
        """写入；已存在且 ``overwrite=False`` 时不动原条目，返回 False。"""
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("用量不能为负")
        statement = (
            "INSERT INTO responses (key, model, response, prompt_tokens, completion_tokens, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " model=excluded.model, response=excluded.response,"
            " prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens,"
            " created_at=excluded.created_at, hits=0, last_hit_at=NULL"
            if overwrite
            else "INSERT OR IGNORE INTO responses"
            " (key, model, response, prompt_tokens, completion_tokens, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
        )
        with self._db:
            cursor = self._db.execute(
                statement,
                (
                    key,
                    model,
                    canonical_json(response),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(self._clock()),
                ),
            )
        return cursor.rowcount > 0

    def stats(self, prices: Mapping[str, ModelPrice] | None = None) -> CacheStats:
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS hits,"
            " COALESCE(SUM(hits * prompt_tokens), 0) AS psaved,"
            " COALESCE(SUM(hits * completion_tokens), 0) AS csaved,"
            " MIN(created_at) AS oldest, MAX(created_at) AS newest"
            " FROM responses"
        ).fetchone()
        saved_usd: float | None = None
        unpriced: list[str] = []
        by_model = self._db.execute(
            "SELECT model, COALESCE(SUM(hits * prompt_tokens), 0) AS ps,"
            " COALESCE(SUM(hits * completion_tokens), 0) AS cs"
            " FROM responses GROUP BY model"
        ).fetchall()
        total = 0.0
        priced = False
        for item in by_model:
            price = lookup_price(str(item["model"]), prices)
            if price is None:
                unpriced.append(str(item["model"]))
                continue
            total += (
                int(item["ps"]) * price.input_per_m + int(item["cs"]) * price.output_per_m
            ) / 1_000_000
            priced = True
        if priced:
            saved_usd = total
        try:
            db_bytes = self.path.stat().st_size
        except OSError:
            db_bytes = 0
        return CacheStats(
            entries=int(row["n"]),
            hits=int(row["hits"]),
            prompt_tokens_saved=int(row["psaved"]),
            completion_tokens_saved=int(row["csaved"]),
            db_bytes=db_bytes,
            oldest_at=int(row["oldest"]) if row["oldest"] is not None else None,
            newest_at=int(row["newest"]) if row["newest"] is not None else None,
            saved_usd=saved_usd,
            unpriced_models=sorted(unpriced),
        )

    def purge(self, older_than_seconds: int | None = None) -> int:
        """按年龄清理；默认只清过期条目。返回删除条数。"""
        now = int(self._clock())
        if older_than_seconds is None:
            cutoff = now - (self.ttl_seconds if self.ttl_seconds is not None else 0)
            if self.ttl_seconds is None:
                return 0
        else:
            if older_than_seconds < 0:
                raise ValueError("older_than_seconds 不能为负")
            cutoff = now - older_than_seconds
        with self._db:
            cursor = self._db.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,))
        return cursor.rowcount

    def get_or_none(self, key: str) -> Any:
        entry = self.get(key)
        return entry.response if entry is not None else None
