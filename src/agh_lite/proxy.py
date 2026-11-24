"""可选：本地 OpenAI 兼容代理，自动裁剪旧工具结果并缓存重复请求。

为什么需要它：DSH 的插件扩展点只能改路由（``agent/request`` 返回 LlmCallConfig），
改不了完整 prompt；内置压缩只在压力越线时才触发。想让「每一次请求」都少带一点，
最省事的做法是让模型调用先经过一个本地代理：

    DSH provider baseUrl -> http://127.0.0.1:8787/v1 -> 上游

代理只做两件事，都可单独关闭：

1. **裁剪**：把较早的 ``role=tool`` 长结果截成头尾两段，保留最近 N 条不动；
2. **缓存**：非流式请求按请求体哈希命中，第二次不再打到上游。

默认只监听 127.0.0.1，不记录请求正文，不落盘 Authorization。
这不是 MITM：用户显式把 provider 指过来，随时可以改回去。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .cache import ResponseCache

TRIM_MARKER = "\n…[agh-lite 已裁剪 {chars} 字符]…\n"
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class ProxyConfig:
    upstream: str
    host: str = "127.0.0.1"
    port: int = 8787
    trim: bool = True
    max_tool_chars: int = 4_000
    keep_recent: int = 8
    stream_usage: bool = False
    cache: bool = True
    cache_path: str | Path | None = None
    cache_ttl_seconds: int = 7 * 24 * 3600
    cache_max_entries: int | None = 5_000
    ledger: bool = True
    ledger_path: str | Path | None = None
    timeout: float = 300.0

    def __post_init__(self) -> None:
        if not self.upstream.startswith(("http://", "https://")):
            raise ValueError("upstream 必须是 http(s) 地址")
        if self.max_tool_chars < 200:
            raise ValueError("max_tool_chars 至少 200")
        if self.keep_recent < 0:
            raise ValueError("keep_recent 不能为负")


@dataclass
class TrimStats:
    messages: int = 0
    trimmed: int = 0
    chars_removed: int = 0


@dataclass
class ProxyLedgerStats:
    requests: int = 0
    cache_hits: int = 0
    trimmed_requests: int = 0
    chars_removed: int = 0
    tokens_saved_cache: int = 0
    tokens_saved_trim: int = 0
    cost_saved_usd: float | None = None
    unpriced_models: list[str] = field(default_factory=list)
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "trimmed_requests": self.trimmed_requests,
            "chars_removed": self.chars_removed,
            "tokens_saved_cache": self.tokens_saved_cache,
            "tokens_saved_trim": self.tokens_saved_trim,
            "cost_saved_usd": None if self.cost_saved_usd is None else round(self.cost_saved_usd, 4),
            "unpriced_models": self.unpriced_models,
            "by_model": {
                model: {
                    **{key: int(value) for key, value in row.items() if key != "cost_usd"},
                    "cost_usd": round(row["cost_usd"], 4),
                }
                for model, row in sorted(self.by_model.items())
            },
        }


def _usage_tokens(payload: Any) -> tuple[int, int]:
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return (
        int(prompt) if isinstance(prompt, (int, float)) else 0,
        int(completion) if isinstance(completion, (int, float)) else 0,
    )


def load_ledger(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def summarize_ledger(
    records: list[dict[str, Any]],
    prices: dict[str, Any] | None = None,
) -> ProxyLedgerStats:
    from .pricing import lookup_price

    stats = ProxyLedgerStats()
    cost = 0.0
    priced = False
    unpriced: set[str] = set()
    for item in records:
        stats.requests += 1
        trimmed_chars = int(item.get("trimmed_chars", 0) or 0)
        if trimmed_chars:
            stats.trimmed_requests += 1
            stats.chars_removed += trimmed_chars
        trim_tokens = int(item.get("trimmed_tokens", 0) or 0)
        stats.tokens_saved_trim += trim_tokens
        model = str(item.get("model") or "")
        price = lookup_price(model, prices) if model else None
        row = stats.by_model.setdefault(
            model or "unknown",
            {"requests": 0.0, "cache_hits": 0.0, "tokens_saved": 0.0, "cost_usd": 0.0},
        )
        row["requests"] += 1
        if item.get("cache_hit"):
            stats.cache_hits += 1
            row["cache_hits"] += 1
            prompt = int(item.get("prompt_tokens", 0) or 0)
            completion = int(item.get("completion_tokens", 0) or 0)
            saved = prompt + completion
            stats.tokens_saved_cache += saved
            row["tokens_saved"] += saved
            if price is not None:
                amount = (prompt * price.input_per_m + completion * price.output_per_m) / 1_000_000
                cost += amount
                row["cost_usd"] += amount
                priced = True
            elif model:
                unpriced.add(model)
        elif trim_tokens:
            row["tokens_saved"] += trim_tokens
            if price is not None:
                amount = trim_tokens * price.input_per_m / 1_000_000
                cost += amount
                row["cost_usd"] += amount
                priced = True
            elif model:
                unpriced.add(model)
    stats.cost_saved_usd = cost if priced else None
    stats.unpriced_models = sorted(unpriced)
    return stats


@dataclass
class ProxyStats:
    requests: int = 0
    trimmed_requests: int = 0
    chars_removed: int = 0
    cache_hits: int = 0
    upstream_calls: int = 0
    errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        trimmed: bool = False,
        removed: int = 0,
        cache_hit: bool = False,
        upstream: bool = False,
        error: bool = False,
    ) -> None:
        with self.lock:
            self.requests += 1
            if trimmed:
                self.trimmed_requests += 1
                self.chars_removed += removed
            if cache_hit:
                self.cache_hits += 1
            if upstream:
                self.upstream_calls += 1
            if error:
                self.errors += 1

    def to_dict(self) -> dict[str, int]:
        with self.lock:
            return {
                "requests": self.requests,
                "trimmed_requests": self.trimmed_requests,
                "chars_removed": self.chars_removed,
                "cache_hits": self.cache_hits,
                "upstream_calls": self.upstream_calls,
                "errors": self.errors,
            }


def _trim_text(text: str, limit: int) -> tuple[str, int]:
    if len(text) <= limit:
        return text, 0
    head = limit // 2
    tail = limit - head
    removed = len(text) - limit
    return text[:head] + TRIM_MARKER.format(chars=removed) + text[-tail:], removed


def trim_messages(
    messages: list[Any],
    *,
    max_tool_chars: int = 4_000,
    keep_recent: int = 8,
) -> TrimStats:
    """只裁较早的 tool 结果；最近 keep_recent 条、非 tool 消息一律不动。"""
    stats = TrimStats(messages=len(messages))
    cutoff = max(0, len(messages) - keep_recent)
    for index, message in enumerate(messages):
        if index >= cutoff or not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str):
            new, removed = _trim_text(content, max_tool_chars)
            if removed:
                message["content"] = new
                stats.trimmed += 1
                stats.chars_removed += removed
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                new, removed = _trim_text(text, max_tool_chars)
                if removed:
                    block["text"] = new
                    stats.trimmed += 1
                    stats.chars_removed += removed
    return stats


def _rewrite_body(body: bytes, config: ProxyConfig) -> tuple[bytes, TrimStats]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, TrimStats()
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return body, TrimStats()
    stats = trim_messages(
        payload["messages"],
        max_tool_chars=config.max_tool_chars,
        keep_recent=config.keep_recent,
    )
    if not stats.trimmed:
        return body, stats
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), stats


def _parse_json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _with_stream_usage(body: bytes) -> bytes:
    """流式响应默认不带 usage；给上游加一个标准参数，末尾就会多一个 usage 分片。"""
    payload = _parse_json(body)
    if not isinstance(payload, dict) or not payload.get("stream"):
        return body
    options = payload.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    if options.get("include_usage") is True:
        return body
    options["include_usage"] = True
    payload["stream_options"] = options
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _target_path(base_path: str, request_path: str) -> str:
    """上游 base 自带路径前缀时，别把请求里的同名前缀再拼一遍。"""
    path = request_path if request_path.startswith("/") else f"/{request_path}"
    if base_path and (path == base_path or path.startswith(base_path + "/")):
        return path
    return f"{base_path}{path}" if base_path else path


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ProxyConfig, cache: ResponseCache | None) -> None:
        self.config = config
        self.cache = cache
        self.stats = ProxyStats()
        self._cache_lock = threading.Lock()
        self._ledger_lock = threading.Lock()
        super().__init__((config.host, config.port), _Handler)

    def record_ledger(self, record: dict[str, Any]) -> None:
        if not self.config.ledger:
            return
        path = self.config.ledger_path or (Path.home() / ".dsh" / "agh-lite-proxy.jsonl")
        try:
            with self._ledger_lock:
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
                with Path(path).expanduser().open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # 用连接关闭结束流式响应，避开 chunked 拼装
    server: _ProxyServer

    def log_message(self, format: str, *args: Any) -> None:
        return  # 不把请求打到 stderr，避免干扰 DSH

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/agh-lite/health":
            self._send_json(200, {"ok": True, **self.server.stats.to_dict()})
            return
        self._relay("GET", b"")

    def do_POST(self) -> None:
        if self.path.rstrip("/") == "/agh-lite/health":
            self._send_json(405, {"ok": False, "error": "use GET"})
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(411, {"error": "需要 Content-Length"})
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "Content-Length 非法"})
            return
        body = self.rfile.read(length)
        config = self.server.config
        stats = TrimStats()
        if config.trim:
            body, stats = _rewrite_body(body, config)
        payload = _parse_json(body)
        model = str(payload.get("model") or "") if isinstance(payload, dict) else ""
        stream = bool(isinstance(payload, dict) and payload.get("stream"))
        if stream and config.stream_usage:
            body = _with_stream_usage(body)
            payload = _parse_json(body)
            stream = bool(isinstance(payload, dict) and payload.get("stream"))
        key = hashlib.sha256(body).hexdigest()
        cache = self.server.cache
        if config.cache and cache is not None and not stream:
            with self.server._cache_lock:
                hit = cache.get(key)
            if hit is not None:
                self.server.stats.record(cache_hit=True, trimmed=bool(stats.trimmed), removed=stats.chars_removed)
                prompt, completion = _usage_tokens(hit.response)
                self.server.record_ledger(
                    {
                        "ts": int(time.time() * 1000),
                        "model": model,
                        "stream": False,
                        "cache_hit": True,
                        "trimmed_chars": stats.chars_removed,
                        "trimmed_tokens": round(stats.chars_removed / 3.6),
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                    }
                )
                self._send_json(200, hit.response)
                return
        self.server.stats.record(trimmed=bool(stats.trimmed), removed=stats.chars_removed)
        self._relay(
            "POST",
            body,
            cache_key=key if config.cache and not stream else None,
            model=model,
            stream=stream,
            trim=stats,
        )

    # ------------------------------------------------------------ 内部
    def _is_stream(self, body: bytes) -> bool:
        payload = _parse_json(body)
        return bool(isinstance(payload, dict) and payload.get("stream"))

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _relay(
        self,
        method: str,
        body: bytes,
        cache_key: str | None = None,
        *,
        model: str = "",
        stream: bool = False,
        trim: TrimStats | None = None,
    ) -> None:
        trim = trim or TrimStats()
        config = self.server.config
        parts = urlsplit(config.upstream)
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        target = _target_path(parts.path.rstrip("/"), path)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP
            and key.lower() not in {"host", "content-length", "expect"}
        }
        headers["Host"] = parts.netloc
        if method != "GET":
            # 裁剪后正文可能变短，必须重算长度，否则上游会一直等剩下的字节
            headers["Content-Length"] = str(len(body))
        connection_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        port = parts.port or (443 if parts.scheme == "https" else 80)
        connection = connection_cls(parts.hostname, port, timeout=config.timeout)
        try:
            connection.request(method, target, body=body if method != "GET" else None, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self.server.stats.record(error=True)
            self._send_json(502, {"error": f"上游不可达：{exc}"})
            connection.close()
            return

        self.server.stats.record(upstream=True)
        response_status = response.status
        response_headers = response.getheaders()
        content_type = response.getheader("Content-Type") or "application/json; charset=utf-8"
        is_json = "application/json" in content_type
        streaming = self._is_stream(body)
        if streaming or not is_json:
            self.send_response(response_status)
            for key, value in response_headers:
                if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                    self.send_header(key, value)
            self.end_headers()
            usage = (0, 0)
            parse_buffer = ""
            try:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if streaming:
                        parse_buffer += chunk.decode("utf-8", "ignore")
                        while "\n\n" in parse_buffer:
                            raw, parse_buffer = parse_buffer.split("\n\n", 1)
                            for line in raw.replace("\r\n", "\n").splitlines():
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if not data or data == "[DONE]":
                                    continue
                                try:
                                    event = json.loads(data)
                                except json.JSONDecodeError:
                                    continue
                                found = _usage_tokens(event)
                                if found != (0, 0):
                                    usage = found
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                response.close()
                connection.close()
            if streaming:
                self.server.record_ledger(
                    {
                        "ts": int(time.time() * 1000),
                        "model": model,
                        "stream": True,
                        "cache_hit": False,
                        "trimmed_chars": trim.chars_removed,
                        "trimmed_tokens": round(trim.chars_removed / 3.6),
                        "prompt_tokens": usage[0],
                        "completion_tokens": usage[1],
                    }
                )
            return

        data = response.read()
        response.close()
        connection.close()
        payload = _parse_json(data)
        if cache_key is not None and response_status == 200 and payload is not None:
            cache = self.server.cache
            if cache is not None:
                with self.server._cache_lock:
                    cache.put(cache_key, payload)
        prompt, completion = _usage_tokens(payload)
        self.server.record_ledger(
            {
                "ts": int(time.time() * 1000),
                "model": model,
                "stream": stream,
                "cache_hit": False,
                "trimmed_chars": trim.chars_removed,
                "trimmed_tokens": round(trim.chars_removed / 3.6),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            }
        )
        self.send_response(response_status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(config: ProxyConfig) -> _ProxyServer:
    cache: ResponseCache | None = None
    if config.cache:
        path = config.cache_path or (Path.home() / ".dsh" / "agh-lite-proxy-cache.sqlite")
        cache = ResponseCache(
            path,
            ttl_seconds=config.cache_ttl_seconds,
            max_entries=config.cache_max_entries,
        )
    return _ProxyServer(config, cache)


def serve(config: ProxyConfig) -> None:
    server = build_server(config)
    print(f"agh-lite proxy 监听 http://{config.host}:{config.port} -> {config.upstream}")
    trim_state = "开" if config.trim else "关"
    print(f"裁剪：{trim_state}（tool 结果 > {config.max_tool_chars} 字，保留最近 {config.keep_recent} 条）")
    print(f"缓存：{'开' if config.cache else '关'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        if server.cache is not None:
            server.cache.close()
