from __future__ import annotations

import pytest

from agh_lite.tokens import (
    DEFAULT_PROFILE,
    TokenProfile,
    estimate_messages_tokens,
    estimate_text_tokens,
    format_tokens,
)


def test_empty_text_is_zero() -> None:
    assert estimate_text_tokens("") == 0


def test_cjk_estimate_uses_ceil() -> None:
    assert estimate_text_tokens("你好世界") == 3  # 4 / 1.6 = 2.5 -> 3


def test_ascii_estimate() -> None:
    assert estimate_text_tokens("abcd") == 2  # 4 / 3.6 -> 1.12 -> 2


def test_mixed_text_counts_each_bucket() -> None:
    # 2 汉字 + 4 ascii: 2/1.6 + 4/3.6 = 2.36 -> 3
    assert estimate_text_tokens("汉字abcd") == 3


def test_profile_validates_rates() -> None:
    with pytest.raises(ValueError):
        TokenProfile(cjk_chars_per_token=0)
    with pytest.raises(ValueError):
        TokenProfile(message_overhead=-1)


def test_message_overhead_and_role() -> None:
    assert DEFAULT_PROFILE.estimate_message({"role": "user", "content": "abcd"}) == 2 + 1 + 4


def test_message_without_role() -> None:
    assert DEFAULT_PROFILE.estimate_message({"content": "abcd"}) == 2 + 4


def test_content_blocks() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "abcd"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "你好"}]},
    ]
    estimate = estimate_messages_tokens(messages)
    assert estimate == (2 + 1 + 4) * 2


def test_image_block_is_flat_estimate() -> None:
    assert DEFAULT_PROFILE.estimate_content([{"type": "image"}]) == 800


def test_format_tokens() -> None:
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1_234) == "1.23K"
    assert format_tokens(1_234_567) == "1.23M"
    assert format_tokens(1_234_567_890) == "1.23B"
