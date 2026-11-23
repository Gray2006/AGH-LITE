"""模型价格表与费用估算。

AGH 的 ``billing.py`` 只负责「结算后怎么扣款」；个人版更需要另一半：
**这些 token 到底值多少钱**。DSH 会话不记费用，所以这里用价格表把
``usage`` 换算成美元，并明确标注「估算，不是账单」。

价格单位：美元 / 百万 tokens。默认表只放常见模型，按 2026-11 的公开价
量级取整；真实价格会变，用 ``--pricing`` 传一份自己的表覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """每百万 tokens 的价格。"""

    input_per_m: float
    cache_read_per_m: float
    output_per_m: float
    currency: str = "USD"

    def cost(self, *, input_tokens: int, cache_read_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_m
            + cache_read_tokens * self.cache_read_per_m
            + output_tokens * self.output_per_m
        ) / MILLION


# 键按小写子串匹配，越具体的放前面；未命中的模型不猜价格。
DEFAULT_PRICES: dict[str, ModelPrice] = {
    # DeepSeek V3.2 量级：缓存命中约为未命中的 1/10
    "deepseek-v4.1-flash": ModelPrice(0.28, 0.028, 0.42),
    "deepseek-v4-flash": ModelPrice(0.28, 0.028, 0.42),
    "deepseek-v4": ModelPrice(0.55, 0.055, 1.70),
    "deepseek-v3": ModelPrice(0.28, 0.028, 0.42),
    "deepseek-chat": ModelPrice(0.28, 0.028, 0.42),
    "deepseek-reasoner": ModelPrice(0.55, 0.14, 2.20),
    "deepseek-flash": ModelPrice(0.28, 0.028, 0.42),
    # OpenAI
    "gpt-4o-mini": ModelPrice(0.15, 0.075, 0.60),
    "gpt-4o": ModelPrice(2.50, 1.25, 10.00),
    "o4-mini": ModelPrice(1.10, 0.275, 4.40),
    # Anthropic
    "claude-sonnet": ModelPrice(3.00, 0.30, 15.00),
    "claude-haiku": ModelPrice(0.80, 0.08, 4.00),
    # Google
    "gemini-2.5-flash": ModelPrice(0.30, 0.075, 2.50),
    "gemini-2.5-pro": ModelPrice(1.25, 0.31, 10.00),
    # 智谱免费档：按 0 计，避免「有 tokens 却没有费用」被当成漏算
    "glm-4-flash": ModelPrice(0.0, 0.0, 0.0),
}


def normalize_model(model: str) -> str:
    """``deepseek/deepseek-v4.1-flash`` → ``deepseek-v4.1-flash``。"""
    return model.strip().lower().rsplit("/", 1)[-1]


def lookup_price(model: str, prices: Mapping[str, ModelPrice] | None = None) -> ModelPrice | None:
    table = prices or DEFAULT_PRICES
    name = normalize_model(model)
    if not name:
        return None
    if name in table:
        return table[name]
    for key, price in table.items():
        if key in name:
            return price
    return None


def load_prices(path: str | Path, base: Mapping[str, ModelPrice] | None = None) -> dict[str, ModelPrice]:
    """读用户价格表并与默认表合并。

    文件格式：``{"models": {"model-key": {"input": 0.28, "cache_read": 0.028, "output": 0.42}}}``
    """
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    models = raw.get("models", raw) if isinstance(raw, dict) else {}
    if not isinstance(models, dict):
        raise ValueError("价格表格式应为 {\"models\": {...}}")
    merged = dict(base or DEFAULT_PRICES)
    for key, value in models.items():
        if not isinstance(value, dict):
            raise ValueError(f"{key} 的价格应为对象")
        merged[str(key).lower()] = ModelPrice(
            input_per_m=float(value.get("input", 0.0)),
            cache_read_per_m=float(value.get("cache_read", value.get("input", 0.0))),
            output_per_m=float(value.get("output", 0.0)),
            currency=str(value.get("currency", "USD")),
        )
    return merged


def format_usd(amount: float | None) -> str:
    if amount is None:
        return "未知"
    if amount >= 100:
        return f"${amount:,.0f}"
    if amount >= 1:
        return f"${amount:,.2f}"
    return f"${amount:.3f}"


def prices_to_dict(prices: Mapping[str, ModelPrice]) -> dict[str, Any]:
    return {
        "models": {
            key: {
                "input": price.input_per_m,
                "cache_read": price.cache_read_per_m,
                "output": price.output_per_m,
                "currency": price.currency,
            }
            for key, price in prices.items()
        }
    }
