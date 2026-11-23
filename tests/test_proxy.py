from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from agh_lite.proxy import ProxyConfig, build_server, load_ledger, summarize_ledger, trim_messages


def test_trim_old_tool_result_only() -> None:
    long_text = "x" * 10_000
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "tool", "content": long_text},
        {"role": "user", "content": long_text},
        {"role": "tool", "content": long_text},
    ]
    stats = trim_messages(messages, max_tool_chars=1_000, keep_recent=2)
    assert stats.trimmed == 1
    assert "已裁剪" in messages[1]["content"]
    assert len(messages[1]["content"]) < 1_200
    assert messages[2]["content"] == long_text  # 非 tool 不动
    assert messages[3]["content"] == long_text  # 最近 N 条不动


def test_trim_list_content_blocks() -> None:
    messages = [
        {"role": "tool", "content": [{"type": "text", "text": "y" * 5_000}]},
        {"role": "user", "content": "now"},
    ]
    trim_messages(messages, max_tool_chars=500, keep_recent=1)
    assert "已裁剪" in messages[0]["content"][0]["text"]


def test_short_results_untouched() -> None:
    messages = [{"role": "tool", "content": "ok"}]
    stats = trim_messages(messages, keep_recent=0)
    assert stats.trimmed == 0
    assert messages[0]["content"] == "ok"


class _Upstream(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict]] = []

    def log_message(self, *args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        _Upstream.calls.append(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in (b"data: one\n\n", b"data: [DONE]\n\n"):
                self.wfile.write(piece)
                self.wfile.flush()
            return
        data = json.dumps(
            {
                "id": "resp",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def upstream():
    _Upstream.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(tmp_path, upstream):
    config = ProxyConfig(
        upstream=upstream,
        port=0,
        max_tool_chars=1_000,
        keep_recent=2,
        cache_path=tmp_path / "proxy-cache.sqlite",
        ledger_path=tmp_path / "proxy-ledger.jsonl",
    )
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()
    server.server_close()
    if server.cache is not None:
        server.cache.close()


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_proxy_trims_and_caches(proxy) -> None:
    base, _server = proxy
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "tool", "content": "z" * 8_000},
            {"role": "user", "content": "question"},
            {"role": "tool", "content": "recent"},
        ],
    }
    first = _post(base, payload)
    assert first["choices"][0]["message"]["content"] == "ok"
    assert "已裁剪" in _Upstream.calls[0]["messages"][1]["content"]
    assert _Upstream.calls[0]["messages"][3]["content"] == "recent"

    second = _post(base, payload)
    assert second["choices"][0]["message"]["content"] == "ok"
    assert len(_Upstream.calls) == 1  # 第二次走缓存
    with urllib.request.urlopen(base + "/agh-lite/health", timeout=5) as response:
        health = json.loads(response.read())
    assert health["cache_hits"] == 1
    assert health["trimmed_requests"] >= 1

    records = load_ledger(_server.config.ledger_path)
    assert len(records) == 2
    assert records[0]["trimmed_chars"] > 0
    assert records[1]["cache_hit"] is True
    summary = summarize_ledger(records)
    assert summary.cache_hits == 1
    assert summary.tokens_saved_cache > 0
    assert summary.tokens_saved_trim > 0


def test_summarize_ledger_prices_savings() -> None:
    records = [
        {
            "model": "deepseek-v4",
            "cache_hit": True,
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "trimmed_chars": 0,
            "trimmed_tokens": 0,
        },
        {
            "model": "deepseek-v4",
            "cache_hit": False,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "trimmed_chars": 3_600,
            "trimmed_tokens": 1_000,
        },
    ]
    summary = summarize_ledger(records)
    assert summary.requests == 2
    assert summary.tokens_saved_cache == 2_000_000
    assert summary.tokens_saved_trim == 1_000
    # 缓存：1M 输入 0.55 + 1M 输出 1.70；裁剪：1K 输入 0.55/1M
    assert summary.cost_saved_usd == pytest.approx(2.25 + 0.00055)


def test_proxy_streams_and_does_not_cache(proxy) -> None:
    base, _ = proxy
    payload = {
        "model": "m",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    for _ in range(2):
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode()
        assert "data: one" in text and "[DONE]" in text
    assert len(_Upstream.calls) == 2
