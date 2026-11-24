from __future__ import annotations

import pytest

from agh_lite.cache import ResponseCache


@pytest.fixture
def clock() -> list[int]:
    return [1_000_000]


@pytest.fixture
def cache(tmp_path, clock) -> ResponseCache:
    store = ResponseCache(tmp_path / "cache.sqlite", clock=lambda: clock[0])
    yield store
    store.close()


def test_put_then_get(cache: ResponseCache) -> None:
    key = cache.make_key("deepseek-v4", [{"role": "user", "content": "hi"}])
    assert cache.put(key, {"text": "hello"}, prompt_tokens=10, completion_tokens=5) is True
    entry = cache.get(key)
    assert entry is not None
    assert entry.response == {"text": "hello"}
    assert entry.prompt_tokens == 10
    assert entry.hits == 1


def test_get_missing_returns_none(cache: ResponseCache) -> None:
    assert cache.get("nope") is None


def test_max_entries_evicts_oldest(tmp_path) -> None:
    tick = [0]

    def clock() -> int:
        tick[0] += 1
        return tick[0]

    limited = ResponseCache(tmp_path / "limited.sqlite", max_entries=2, clock=clock)
    try:
        for index in range(3):
            key = limited.make_key("m", [{"role": "user", "content": f"x{index}"}])
            limited.put(key, {"n": index})
        assert limited.stats().entries == 2
        oldest = limited.make_key("m", [{"role": "user", "content": "x0"}])
        assert limited.get(oldest) is None
    finally:
        limited.close()


def test_stats_count_hits_and_saved_tokens(cache: ResponseCache) -> None:
    key = cache.make_key("m", [{"role": "user", "content": "x"}])
    cache.put(key, "ok", model="m", prompt_tokens=100, completion_tokens=20)
    cache.get(key)
    cache.get(key)
    stats = cache.stats()
    assert stats.entries == 1
    assert stats.hits == 2
    assert stats.prompt_tokens_saved == 200
    assert stats.completion_tokens_saved == 40
    assert stats.db_bytes > 0
    assert stats.saved_usd is None  # 模型 "m" 没有价格
    assert stats.unpriced_models == ["m"]


def test_stats_saved_usd_with_priced_model(cache: ResponseCache) -> None:
    key = cache.make_key("deepseek-v4", [{"role": "user", "content": "x"}])
    cache.put(key, "ok", model="deepseek-v4", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    cache.get(key)
    stats = cache.stats()
    # deepseek-v4：输入 0.55、输出 1.70，各 1M
    assert stats.saved_usd == pytest.approx(2.25)


def test_key_ignores_volatile_fields(cache: ResponseCache) -> None:
    first = cache.make_key("m", [{"role": "user", "content": "x", "id": "a", "timestamp": 1}])
    second = cache.make_key("m", [{"role": "user", "content": "x", "id": "b", "timestamp": 2}])
    assert first == second


def test_key_changes_with_params(cache: ResponseCache) -> None:
    base = cache.make_key("m", [{"role": "user", "content": "x"}], {"temperature": 0.2})
    other = cache.make_key("m", [{"role": "user", "content": "x"}], {"temperature": 0.9})
    assert base != other


def test_put_does_not_overwrite_by_default(cache: ResponseCache) -> None:
    key = cache.make_key("m", [{"role": "user", "content": "x"}])
    cache.put(key, "first")
    assert cache.put(key, "second") is False
    assert cache.get(key).response == "first"


def test_put_can_overwrite(cache: ResponseCache) -> None:
    key = cache.make_key("m", [{"role": "user", "content": "x"}])
    cache.put(key, "first")
    assert cache.put(key, "second", overwrite=True) is True
    assert cache.get(key).response == "second"


def test_ttl_expiry_removes_entry(tmp_path, clock) -> None:
    store = ResponseCache(tmp_path / "c.sqlite", ttl_seconds=10, clock=lambda: clock[0])
    try:
        key = store.make_key("m", [{"role": "user", "content": "x"}])
        store.put(key, "ok")
        clock[0] += 11
        assert store.get(key) is None
        assert store.stats().entries == 0
    finally:
        store.close()


def test_purge_older_than(cache: ResponseCache, clock) -> None:
    key = cache.make_key("m", [{"role": "user", "content": "x"}])
    cache.put(key, "ok")
    clock[0] += 1_000
    assert cache.purge(older_than_seconds=500) == 1
    assert cache.stats().entries == 0


def test_purge_without_ttl_returns_zero(tmp_path) -> None:
    store = ResponseCache(tmp_path / "c.sqlite", ttl_seconds=None)
    try:
        assert store.purge() == 0
    finally:
        store.close()


def test_context_manager(tmp_path) -> None:
    path = tmp_path / "c.sqlite"
    with ResponseCache(path) as store:
        key = store.make_key("m", [{"role": "user", "content": "x"}])
        store.put(key, "ok")
    with ResponseCache(path) as store:
        assert store.get(key) is not None


def test_invalid_ttl() -> None:
    with pytest.raises(ValueError):
        ResponseCache(":memory:", ttl_seconds=0)
