"""共享测试夹具：构造一个形态接近真实 DSH 的合成会话。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _event(kind: str, data: dict[str, Any], time_ms: int) -> dict[str, Any]:
    return {"type": kind, "time": time_ms, "data": data}


def build_events(*, tool_result_chars: int = 4_000) -> list[dict[str, Any]]:
    """一个会话：两轮、两个相同 read 调用、一条超大 shell 结果、一次报错。"""
    big_text = "x" * tool_result_chars
    huge_text = "y" * 40_000
    return [
        _event("session", {"id": "sess-1", "cwd": "G:\\work", "createdAt": 1_700_000_000_000}, 1_000),
        _event(
            "request/header",
            {
                "header": {
                    "config": {"provider": "deepseek", "model": "deepseek-v4"},
                    "tools": [
                        {"name": "read", "description": "d" * 200},
                        {"name": "shell", "description": "s" * 200},
                    ],
                }
            },
            1_100,
        ),
        _event(
            "system/message",
            {"message": {"role": "system", "content": "你是助手。" * 100}},
            1_200,
        ),
        _event("request/context", {"contextWindow": 131_072}, 1_300),
        _event("turn/start", {"turn": 1}, 1_400),
        _event(
            "assistant/message",
            {
                "turn": 1,
                "step": 1,
                "usage": {"inputTokens": 1000, "cacheReadTokens": 2000, "outputTokens": 100, "totalTokens": 3100},
                "message": {"content": [{"type": "text", "text": "第一轮"}]},
            },
            1_500,
        ),
        _event(
            "tool/call",
            {"callId": "c1", "name": "read", "arguments": '{"file_path":"a.py"}', "turn": 1, "step": 1},
            1_600,
        ),
        _event(
            "tool/result",
            {"message": {"toolCallId": "c1", "content": [{"type": "text", "text": big_text}]}},
            1_700,
        ),
        _event(
            "assistant/message",
            {
                "turn": 1,
                "step": 2,
                "usage": {"inputTokens": 500, "cacheReadTokens": 2500, "outputTokens": 50, "totalTokens": 3050},
                "message": {"content": [{"type": "text", "text": "第二轮"}]},
            },
            1_800,
        ),
        _event(
            "tool/call",
            {"callId": "c2", "name": "read", "arguments": '{"file_path":"a.py"}', "turn": 1, "step": 2},
            1_900,
        ),
        _event(
            "tool/result",
            {"message": {"toolCallId": "c2", "content": [{"type": "text", "text": big_text}]}},
            2_000,
        ),
        _event(
            "tool/call",
            {"callId": "c3", "name": "shell", "arguments": '{"command":"cat big"}', "turn": 1, "step": 2},
            2_100,
        ),
        _event(
            "tool/result",
            {"message": {"toolCallId": "c3", "content": [{"type": "text", "text": huge_text}]}},
            2_200,
        ),
        _event(
            "tool/result",
            {
                "message": {"toolCallId": "c4", "content": [{"type": "text", "text": "boom"}], "isError": True},
                "error": {"message": "failed"},
            },
            2_300,
        ),
        _event("turn/end", {"turn": 1}, 2_400),
    ]


@pytest.fixture
def events() -> list[dict[str, Any]]:
    return build_events()


@pytest.fixture
def write_session(tmp_path: Path):
    """返回 builder：写出明文或 zstd 会话文件，默认 .jsonl。"""

    def _write(
        events: list[dict[str, Any]] | None = None,
        *,
        name: str = "session.v4.jsonl",
        workspace: str = "ws",
        session_id: str = "sess-1",
        zstd: bool = False,
    ) -> Path:
        base = tmp_path / "sessions" / workspace / session_id
        base.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in (events or build_events()))
        payload += "\n"
        if zstd:
            zstandard = pytest.importorskip("zstandard")
            target = base / (name + ".zstd")
            target.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
        else:
            target = base / name
            target.write_text(payload, encoding="utf-8")
        return target

    return _write
