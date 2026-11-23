from __future__ import annotations

import pytest

from agh_lite.budget import BudgetExceeded, TokenBudget


def make_budget(**kwargs) -> TokenBudget:
    clock = {"now": 1_000}
    budget = TokenBudget(1_000, clock=lambda: clock["now"], **kwargs)
    budget._test_clock = clock  # type: ignore[attr-defined]
    return budget


def test_reserve_holds_capacity() -> None:
    budget = make_budget()
    budget.reserve(owner="main", max_tokens=400)
    snapshot = budget.snapshot()
    assert snapshot.reserved_tokens == 400
    assert snapshot.available_tokens == 600
    assert snapshot.active_leases == 1


def test_reserve_beyond_available_raises() -> None:
    budget = make_budget()
    with pytest.raises(BudgetExceeded) as exc:
        budget.reserve(owner="main", max_tokens=1_001)
    assert exc.value.needed_tokens == 1_001
    assert exc.value.available_tokens == 1_000


def test_reserve_rejects_non_positive() -> None:
    budget = make_budget()
    with pytest.raises(ValueError):
        budget.reserve(owner="main", max_tokens=0)


def test_settle_records_actual_and_frees_reservation() -> None:
    budget = make_budget()
    lease = budget.reserve(owner="main", max_tokens=400)
    assert budget.settle(lease, actual_tokens=120) == (120, 0.0)
    snapshot = budget.snapshot()
    assert snapshot.committed_tokens == 120
    assert snapshot.reserved_tokens == 0
    assert snapshot.available_tokens == 880


def test_settle_is_idempotent_by_lease() -> None:
    budget = make_budget()
    lease = budget.reserve(owner="main", max_tokens=400)
    budget.settle(lease, actual_tokens=100)
    budget.settle(lease, actual_tokens=100)
    assert budget.snapshot().committed_tokens == 100


def test_reserve_and_settle_idempotent_by_key() -> None:
    budget = make_budget()
    first = budget.reserve(owner="main", max_tokens=300, key="req-1")
    retry = budget.reserve(owner="main", max_tokens=300, key="req-1")
    assert retry.lease_id == first.lease_id
    budget.settle(first, actual_tokens=10, key="req-1")
    assert budget.settle(first, actual_tokens=10, key="req-1") == (10, 0.0)
    assert budget.snapshot().committed_tokens == 10


def test_over_reserved_is_recorded_but_charged() -> None:
    budget = make_budget()
    lease = budget.reserve(owner="main", max_tokens=100)
    budget.settle(lease, actual_tokens=150)
    assert budget.snapshot().committed_tokens == 150
    assert [item.kind for item in budget.violations] == ["over_reserved"]


def test_over_budget_is_recorded() -> None:
    budget = make_budget()
    first = budget.reserve(owner="main", max_tokens=800)
    second = budget.reserve(owner="main", max_tokens=200)
    budget.settle(first, actual_tokens=800)
    budget.settle(second, actual_tokens=200)
    kinds = [item.kind for item in budget.violations]
    assert "over_budget" not in kinds
    budget2 = make_budget()
    lease = budget2.reserve(owner="main", max_tokens=900)
    budget2.settle(lease, actual_tokens=1_200)
    assert [item.kind for item in budget2.violations] == ["over_reserved", "over_budget"]


def test_release_frees_capacity() -> None:
    budget = make_budget()
    lease = budget.reserve(owner="main", max_tokens=600)
    budget.release(lease)
    assert budget.snapshot().available_tokens == 1_000


def test_reconcile_reclaims_expired_leases() -> None:
    budget = make_budget(default_ttl_ms=100)
    budget.reserve(owner="main", max_tokens=500)
    budget._test_clock["now"] += 101  # type: ignore[index]
    assert len(budget.reconcile()) == 1
    assert budget.snapshot().reserved_tokens == 0


def test_reconcile_keeps_live_leases() -> None:
    budget = make_budget(default_ttl_ms=10_000)
    budget.reserve(owner="main", max_tokens=500)
    budget._test_clock["now"] += 10  # type: ignore[index]
    assert budget.reconcile() == []


def test_usd_limit_is_enforced() -> None:
    budget = make_budget(limit_usd=0.01)
    with pytest.raises(BudgetExceeded) as exc:
        budget.reserve(owner="main", max_tokens=10, reserved_usd=0.02)
    assert exc.value.unit == "usd"
    assert "usd" in str(exc.value)
    assert exc.value.needed == pytest.approx(0.02)


def test_unknown_lease_settle_raises() -> None:
    budget = make_budget()
    with pytest.raises(KeyError):
        budget.settle("lease-missing", actual_tokens=1)


def test_save_and_load_roundtrip(tmp_path) -> None:
    budget = make_budget()
    lease = budget.reserve(owner="main", max_tokens=300, key="job-1")
    budget.settle(lease, actual_tokens=200, actual_usd=0.5)
    pending = budget.reserve(owner="main", max_tokens=100, key="job-2")
    budget.save(tmp_path / "budget.json")

    restored = TokenBudget.load(tmp_path / "budget.json")
    snapshot = restored.snapshot()
    assert snapshot.committed_tokens == 200
    assert snapshot.committed_usd == pytest.approx(0.5)
    assert snapshot.reserved_tokens == 100
    # 幂等键在重启后仍然有效：同一个 key 不会重复扣
    assert restored.reserve(owner="main", max_tokens=100, key="job-1").lease_id == lease.lease_id
    assert restored.settle(lease, actual_tokens=200, actual_usd=0.5) == (200, 0.5)
    assert restored.settle(pending, actual_tokens=100) == (100, 0.0)
    assert restored.snapshot().committed_tokens == 300


def test_load_rejects_unknown_version(tmp_path) -> None:
    path = tmp_path / "budget.json"
    path.write_text('{"version": 99, "limit_tokens": 10}', encoding="utf-8")
    with pytest.raises(ValueError):
        TokenBudget.load(path)
