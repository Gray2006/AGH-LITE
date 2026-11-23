from __future__ import annotations

import json

import pytest

from agh_lite.pricing import (
    DEFAULT_PRICES,
    ModelPrice,
    format_usd,
    load_prices,
    lookup_price,
    normalize_model,
    prices_to_dict,
)


def test_normalize_strips_provider() -> None:
    assert normalize_model("DeepSeek/DeepSeek-V4.1-Flash") == "deepseek-v4.1-flash"
    assert normalize_model("  gpt-4o  ") == "gpt-4o"


def test_lookup_exact_and_prefix() -> None:
    assert lookup_price("deepseek-v4.1-flash") is DEFAULT_PRICES["deepseek-v4.1-flash"]
    assert lookup_price("openai/gpt-4o") is DEFAULT_PRICES["gpt-4o"]
    assert lookup_price("unknown-model") is None


def test_price_cost() -> None:
    price = ModelPrice(input_per_m=1.0, cache_read_per_m=0.1, output_per_m=2.0)
    cost = price.cost(input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(3.1)


def test_load_prices_merges_and_overrides(tmp_path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps({"models": {"my-model": {"input": 3.0, "cache_read": 0.3, "output": 4.0}}}),
        encoding="utf-8",
    )
    prices = load_prices(path)
    assert lookup_price("my-model", prices) == ModelPrice(3.0, 0.3, 4.0)
    # 内置项仍然在
    assert "deepseek-v4" in prices
    assert "my-model" in prices_to_dict(prices)["models"]


def test_load_prices_bad_shape(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"models": {"x": 1}}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_prices(path)


def test_format_usd() -> None:
    assert format_usd(None) == "未知"
    assert format_usd(0.0012) == "$0.001"
    assert format_usd(12.3456) == "$12.35"
    assert format_usd(1234.5) == "$1,234"
