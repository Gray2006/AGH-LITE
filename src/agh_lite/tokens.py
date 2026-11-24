"""Token 估算。

DSH 会话里记录的是权威用量（``usage.inputTokens`` 等）；估算只用于
「离线没有 usage 的文本」与「打包前试算」。两者刻意分开：
审计报告优先用真实用量，估算值只做相对比较，不会被当成账单。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 中文、日文假名、韩文、全角标点：DeepSeek tokenizer 大约 1.6 字/token
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK 部首补充
    (0x3000, 0x303F),  # CJK 标点
    (0x3040, 0x30FF),  # 假名
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 基本区
    (0xAC00, 0xD7AF),  # 韩文
    (0xF900, 0xFAFF),  # 兼容表意
    (0xFF00, 0xFFEF),  # 全角
    (0x20000, 0x2FA1F),  # 扩展 B 及以上
)


def _is_cjk(code: int) -> bool:
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


_CJK_PATTERN = re.compile("[" + "".join(f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _CJK_RANGES) + "]")


def _count_cjk(text: str) -> int:
    """用正则让 C 层扫一遍，比逐字符 ord 快一个数量级。"""
    return sum(len(match) for match in _CJK_PATTERN.findall(text))


@dataclass(frozen=True, slots=True)
class TokenProfile:
    """可调的估算参数。默认值按 DeepSeek 系 tokenizer 标定，偏保守。"""

    cjk_chars_per_token: float = 1.6
    ascii_chars_per_token: float = 3.6
    other_chars_per_token: float = 2.4
    message_overhead: int = 4
    role_overhead: int = 1

    def __post_init__(self) -> None:
        for name in ("cjk_chars_per_token", "ascii_chars_per_token", "other_chars_per_token"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} 必须为正: {value}")
        if self.message_overhead < 0 or self.role_overhead < 0:
            raise ValueError("overhead 不能为负")

    def estimate_text(self, text: str) -> int:
        """估算一段文本的 token 数；空文本为 0。"""
        if not text:
            return 0
        non_ascii = len(text) - len(text.encode("ascii", "ignore"))
        cjk = _count_cjk(text)
        ascii_chars = len(text) - non_ascii
        other = non_ascii - cjk
        tokens = (
            cjk / self.cjk_chars_per_token
            + ascii_chars / self.ascii_chars_per_token
            + other / self.other_chars_per_token
        )
        return max(1, math.ceil(tokens))

    def estimate_content(self, content: Any) -> int:
        """估算一条消息的 content：字符串或 block 列表。"""
        if content is None:
            return 0
        if isinstance(content, str):
            return self.estimate_text(content)
        if isinstance(content, Mapping):
            return self.estimate_text(str(content.get("text", ""))) + 2
        if isinstance(content, Sequence):
            total = 0
            for block in content:
                if isinstance(block, str):
                    total += self.estimate_text(block)
                elif isinstance(block, Mapping):
                    block_type = block.get("type")
                    if block_type == "text":
                        total += self.estimate_text(str(block.get("text", "")))
                    elif block_type in {"image", "image_url"}:
                        total += 800  # 粗估一张图，按分辨率差异很大
                    elif block_type in {"tool-call", "tool_use"}:
                        total += self.estimate_text(str(block.get("arguments", "")))
                    elif block_type in {"reasoning", "thinking"}:
                        total += self.estimate_text(str(block.get("text", "")))
                    else:
                        total += self.estimate_text(str(block))
            return total
        return self.estimate_text(str(content))

    def estimate_message(self, message: Mapping[str, Any]) -> int:
        total = self.estimate_content(message.get("content"))
        role = message.get("role")
        if isinstance(role, str) and role:
            total += self.role_overhead
        name = message.get("name")
        if isinstance(name, str) and name:
            total += self.estimate_text(name)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, Sequence):
            for call in tool_calls:
                if isinstance(call, Mapping):
                    total += self.estimate_text(str(call.get("function", call)))
        return total + self.message_overhead

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        return sum(self.estimate_message(m) for m in messages)


DEFAULT_PROFILE = TokenProfile()


def estimate_text_tokens(text: str, profile: TokenProfile | None = None) -> int:
    return (profile or DEFAULT_PROFILE).estimate_text(text)


def estimate_messages_tokens(
    messages: Sequence[Mapping[str, Any]], profile: TokenProfile | None = None
) -> int:
    return (profile or DEFAULT_PROFILE).estimate_messages(messages)


def format_tokens(tokens: int) -> str:
    """人类可读：1234 -> 1.2K，1234567 -> 1.23M。"""
    value = float(tokens)
    for unit in ("", "K", "M", "B"):
        if abs(value) < 1000 or unit == "B":
            if unit == "":
                return str(int(value))
            return f"{value:.2f}{unit}" if value < 100 else f"{value:.1f}{unit}"
        value /= 1000
    return f"{value:.1f}B"
