"""预算硬闸门。

从 AGH 的 ``Ledger`` 简化而来，只保留个人版真正用得上的部分：

- 调用前预授权 ``max_tokens``，拿到租约才能发请求；
- 结算按实际用量记账，超出预留记违规但照实扣；
- 租约带 TTL，进程崩溃后由 ``reconcile`` 回收；
- ``key`` 让预留与结算幂等，重试不会重复扣。

额度窗口（日/月）由调用方负责重置，本类只管一个窗口内的账。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExceeded(Exception):
    """可用额度不足以覆盖这次预留。"""

    def __init__(
        self,
        needed: float,
        available: float,
        *,
        unit: str = "tokens",
    ) -> None:
        self.needed = needed
        self.available = available
        self.unit = unit
        if unit == "tokens":
            # 兼容旧属性名
            self.needed_tokens = int(needed)
            self.available_tokens = int(available)
        suffix = "" if unit == "tokens" else " usd"
        super().__init__(f"预算不足：需要 {needed:,}{suffix}，可用 {available:,}{suffix}")


@dataclass
class Lease:
    lease_id: str
    owner: str
    max_tokens: int
    reserved_usd: float
    created_ms: int
    ttl_ms: int
    settled: bool = False
    actual_tokens: int = 0
    actual_usd: float = 0.0
    key: str | None = None

    @property
    def expires_ms(self) -> int:
        return self.created_ms + self.ttl_ms


@dataclass
class Violation:
    kind: str
    owner: str
    detail: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    limit_tokens: int
    committed_tokens: int
    reserved_tokens: int
    available_tokens: int
    limit_usd: float | None
    committed_usd: float
    active_leases: int
    violations: int

    @property
    def used_ratio(self) -> float:
        if self.limit_tokens <= 0:
            return 1.0
        return min(1.0, self.committed_tokens / self.limit_tokens)


class TokenBudget:
    """单窗口 token / 美元预算。

    ``limit_usd=None`` 时不限制美元，只按 token 卡。
    """

    def __init__(
        self,
        limit_tokens: int,
        limit_usd: float | None = None,
        *,
        clock: Callable[[], int] | None = None,
        default_ttl_ms: int = 600_000,
    ) -> None:
        if limit_tokens <= 0:
            raise ValueError("limit_tokens 必须为正")
        if limit_usd is not None and limit_usd <= 0:
            raise ValueError("limit_usd 必须为正或 None")
        if default_ttl_ms <= 0:
            raise ValueError("default_ttl_ms 必须为正")
        self.limit_tokens = limit_tokens
        self.limit_usd = limit_usd
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.default_ttl_ms = default_ttl_ms
        self._leases: dict[str, Lease] = {}
        self._by_key: dict[str, str] = {}
        self._settled_by_key: dict[str, tuple[int, float]] = {}
        self._committed_tokens = 0
        self._committed_usd = 0.0
        self.violations: list[Violation] = []

    # ------------------------------------------------------------ 内部
    def _outstanding_tokens(self) -> int:
        return sum(lease.max_tokens for lease in self._leases.values() if not lease.settled)

    def _outstanding_usd(self) -> float:
        return sum(lease.reserved_usd for lease in self._leases.values() if not lease.settled)

    def snapshot(self) -> Snapshot:
        available = max(
            0, self.limit_tokens - self._committed_tokens - self._outstanding_tokens()
        )
        return Snapshot(
            limit_tokens=self.limit_tokens,
            committed_tokens=self._committed_tokens,
            reserved_tokens=self._outstanding_tokens(),
            available_tokens=available,
            limit_usd=self.limit_usd,
            committed_usd=round(self._committed_usd, 8),
            active_leases=sum(1 for lease in self._leases.values() if not lease.settled),
            violations=len(self.violations),
        )

    # ------------------------------------------------------------ 预留
    def reserve(
        self,
        *,
        owner: str,
        max_tokens: int,
        reserved_usd: float = 0.0,
        key: str | None = None,
        ttl_ms: int | None = None,
    ) -> Lease:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须为正：调用前必须预授权")
        if reserved_usd < 0:
            raise ValueError("reserved_usd 不能为负")
        if key is not None and key in self._by_key:
            return self._leases[self._by_key[key]]

        available = self.snapshot().available_tokens
        if max_tokens > available:
            raise BudgetExceeded(max_tokens, available)
        if self.limit_usd is not None:
            available_usd = self.limit_usd - self._committed_usd - self._outstanding_usd()
            if reserved_usd > available_usd:
                raise BudgetExceeded(
                    round(reserved_usd, 6),
                    round(max(0.0, available_usd), 6),
                    unit="usd",
                )

        lease = Lease(
            lease_id=f"lease-{uuid.uuid4().hex[:10]}",
            owner=owner,
            max_tokens=max_tokens,
            reserved_usd=reserved_usd,
            created_ms=self._clock(),
            ttl_ms=ttl_ms if ttl_ms is not None else self.default_ttl_ms,
            key=key,
        )
        self._leases[lease.lease_id] = lease
        if key is not None:
            self._by_key[key] = lease.lease_id
        return lease

    # ------------------------------------------------------------ 结算
    def settle(
        self,
        lease: Lease | str,
        *,
        actual_tokens: int,
        actual_usd: float = 0.0,
        key: str | None = None,
    ) -> tuple[int, float]:
        """结算并返回 (实际 tokens, 实际美元)。重复结算不重复扣。"""
        if actual_tokens < 0 or actual_usd < 0:
            raise ValueError("实际用量不能为负")
        lease_id = lease.lease_id if isinstance(lease, Lease) else lease
        target = self._leases.get(lease_id)
        if target is None:
            raise KeyError(f"未知租约: {lease_id}")

        effective_key = key or target.key
        if effective_key is not None and effective_key in self._settled_by_key:
            return self._settled_by_key[effective_key]
        if target.settled:
            return target.actual_tokens, target.actual_usd

        if actual_tokens > target.max_tokens or actual_usd > target.reserved_usd + 1e-12:
            self.violations.append(
                Violation(
                    kind="over_reserved",
                    owner=target.owner,
                    detail={
                        "reserved_tokens": target.max_tokens,
                        "actual_tokens": actual_tokens,
                        "reserved_usd": target.reserved_usd,
                        "actual_usd": actual_usd,
                    },
                )
            )

        target.settled = True
        target.actual_tokens = actual_tokens
        target.actual_usd = actual_usd
        self._committed_tokens += actual_tokens
        self._committed_usd += actual_usd
        if effective_key is not None:
            self._settled_by_key[effective_key] = (actual_tokens, actual_usd)

        if self._committed_tokens > self.limit_tokens:
            self.violations.append(
                Violation(
                    kind="over_budget",
                    owner=target.owner,
                    detail={"committed_tokens": self._committed_tokens},
                )
            )
        if self.limit_usd is not None and self._committed_usd > self.limit_usd:
            self.violations.append(
                Violation(
                    kind="over_budget_usd",
                    owner=target.owner,
                    detail={"committed_usd": round(self._committed_usd, 8)},
                )
            )
        return actual_tokens, actual_usd

    # ------------------------------------------------------------ 回收
    def release(self, lease: Lease | str) -> None:
        """释放预留但不记账（调用没发生）。"""
        lease_id = lease.lease_id if isinstance(lease, Lease) else lease
        target = self._leases.get(lease_id)
        if target is None:
            raise KeyError(f"未知租约: {lease_id}")
        target.settled = True

    def reconcile(self, now_ms: int | None = None) -> list[str]:
        """回收超时租约，幂等。"""
        now = now_ms if now_ms is not None else self._clock()
        reclaimed: list[str] = []
        for lease in self._leases.values():
            if not lease.settled and lease.expires_ms <= now:
                lease.settled = True
                reclaimed.append(lease.lease_id)
        return reclaimed

    # ------------------------------------------------------------ 落盘
    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "limit_tokens": self.limit_tokens,
            "limit_usd": self.limit_usd,
            "default_ttl_ms": self.default_ttl_ms,
            "committed_tokens": self._committed_tokens,
            "committed_usd": self._committed_usd,
            "settled_by_key": {key: list(value) for key, value in self._settled_by_key.items()},
            "leases": [
                {
                    "lease_id": lease.lease_id,
                    "owner": lease.owner,
                    "max_tokens": lease.max_tokens,
                    "reserved_usd": lease.reserved_usd,
                    "created_ms": lease.created_ms,
                    "ttl_ms": lease.ttl_ms,
                    "settled": lease.settled,
                    "actual_tokens": lease.actual_tokens,
                    "actual_usd": lease.actual_usd,
                    "key": lease.key,
                }
                for lease in self._leases.values()
            ],
            "violations": [
                {"kind": item.kind, "owner": item.owner, "detail": item.detail}
                for item in self.violations
            ],
        }

    def save(self, path: str | Path) -> None:
        """原子写盘：先写临时文件再替换，崩溃不会留半个 JSON。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    @classmethod
    def load(cls, path: str | Path, *, clock: Callable[[], int] | None = None) -> TokenBudget:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != 1:
            raise ValueError("预算文件版本不支持")
        budget = cls(
            limit_tokens=int(data["limit_tokens"]),
            limit_usd=data.get("limit_usd"),
            clock=clock,
            default_ttl_ms=int(data.get("default_ttl_ms", 600_000)),
        )
        budget._committed_tokens = int(data.get("committed_tokens", 0))
        budget._committed_usd = float(data.get("committed_usd", 0.0))
        for item in data.get("leases", []):
            lease = Lease(
                lease_id=str(item["lease_id"]),
                owner=str(item["owner"]),
                max_tokens=int(item["max_tokens"]),
                reserved_usd=float(item.get("reserved_usd", 0.0)),
                created_ms=int(item["created_ms"]),
                ttl_ms=int(item["ttl_ms"]),
                settled=bool(item.get("settled", False)),
                actual_tokens=int(item.get("actual_tokens", 0)),
                actual_usd=float(item.get("actual_usd", 0.0)),
                key=item.get("key"),
            )
            budget._leases[lease.lease_id] = lease
            if lease.key is not None:
                # 已结算的租约也保留 key 映射：重启后同 key 的 reserve 仍返回原租约
                budget._by_key[lease.key] = lease.lease_id
        for key, value in data.get("settled_by_key", {}).items():
            budget._settled_by_key[str(key)] = (int(value[0]), float(value[1]))
        budget.violations = [
            Violation(kind=str(item["kind"]), owner=str(item["owner"]), detail=item.get("detail", {}))
            for item in data.get("violations", [])
        ]
        return budget
