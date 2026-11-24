"""读取 DSH 会话，抽取「花了多少、花在哪」。

会话文件在 ``$DSH_HOME/sessions/<workspace>/<session>/session.v4.jsonl.zstd``。
本模块只读，不写 DSH 任何文件；没有 ``zstandard`` 时降级读明文 ``.jsonl``。

关键口径（与 DSH usage 对齐）：
- ``inputTokens`` 是不含缓存的新 prompt；
- ``cacheReadTokens`` 是命中前缀缓存的 prompt；
- ``totalTokens`` = input + cacheRead + output。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tokens import DEFAULT_PROFILE, TokenProfile, estimate_text_tokens, format_tokens

SESSION_SUFFIXES = {".jsonl", ".jsonl.zstd", ".jsonl.zst"}


class SessionReadError(Exception):
    pass


def resolve_dsh_home(explicit: str | Path | None = None) -> Path:
    """按 参数 → $DSH_HOME → ~/.dsh 的顺序找 DSH 数据目录。"""
    candidates: list[Path] = []
    if explicit is not None:
        # 显式给了路径就只认它，静默回退到别的目录会让用户以为审计了 A 实际读了 B
        candidates.append(Path(explicit))
    else:
        env = os.environ.get("DSH_HOME")
        if env:
            candidates.append(Path(env))
        candidates.append(Path.home() / ".dsh")
    for candidate in candidates:
        if (candidate / "sessions").is_dir():
            return candidate
        if candidate.name == "sessions" and candidate.is_dir():
            return candidate.parent
    raise SessionReadError(
        "找不到 DSH 会话目录：试过 " + "、".join(str(c) for c in candidates)
    )


def _is_session_file(path: Path) -> bool:
    name = path.name
    return any(name.endswith(suffix) for suffix in SESSION_SUFFIXES)


def find_session_files(
    root: str | Path,
    *,
    since: float | None = None,
    max_bytes: int | None = None,
    limit: int | None = None,
) -> list[Path]:
    """递归找会话文件，按修改时间从新到旧；``limit`` 截断在扫描后。"""
    base = Path(root)
    sessions_dir = base if base.name == "sessions" else base / "sessions"
    if not sessions_dir.is_dir():
        raise SessionReadError(f"不是 DSH 数据目录：{base}")
    files: list[Path] = []
    for path in sessions_dir.rglob("*"):
        if not path.is_file() or not _is_session_file(path):
            continue
        if since is not None and path.stat().st_mtime < since:
            continue
        if max_bytes is not None and path.stat().st_size > max_bytes:
            continue
        files.append(path)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit 必须为正")
        files = files[:limit]
    return files


def _open_lines(path: Path) -> Iterator[str]:
    if path.name.endswith((".zstd", ".zst")):
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise SessionReadError(
                "读 .zstd 会话需要 zstandard：pip install 'agh-lite[dsh]'"
            ) from exc
        with path.open("rb") as raw:
            reader = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
            try:
                data = reader.read()
            except zstandard.ZstdError as exc:
                raise SessionReadError(f"zstd 解压失败：{path}（{exc}）") from exc
            finally:
                reader.close()
        yield from data.decode("utf-8", errors="replace").splitlines(keepends=True)
    else:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield from handle


def iter_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """逐行产事件；坏行跳过（真实会话里偶有截断）。"""
    for line in _open_lines(Path(path)):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str
    turn: int
    step: int

    @property
    def args_key(self) -> str:
        return f"{self.name}:{self.arguments.strip()}"


@dataclass
class ToolResult:
    call_id: str
    chars: int
    tokens: int
    is_error: bool


@dataclass
class StepUsage:
    turn: int
    step: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    reasoning_chars: int = 0
    text_chars: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens


@dataclass
class SessionRecord:
    path: Path
    session_id: str
    cwd: str
    created_at: int
    provider: str
    model: str
    started_at: int = 0
    ended_at: int = 0
    context_window: int | None = None
    system_chars: int = 0
    tool_schema_chars: int = 0
    tool_names: list[str] = field(default_factory=list)
    steps: list[StepUsage] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    turns: int = 0
    duration_ms: int = 0
    compression_calls: int = 0
    compactions: int = 0
    llm_retries: int = 0
    error_results: int = 0
    events: int = 0
    header_changes: int = 0
    system_changes: int = 0
    system_volatile_changes: int = 0
    tool_changes: int = 0
    tool_added: Counter[str] = field(default_factory=Counter)
    tool_removed: Counter[str] = field(default_factory=Counter)
    _last_header_hash: str = field(default="", repr=False)
    _last_system_hash: str = field(default="", repr=False)
    _last_system_raw_hash: str = field(default="", repr=False)
    _last_tool_names: frozenset[str] = field(default_factory=frozenset, repr=False)
    profile: TokenProfile = DEFAULT_PROFILE

    # ------------------------------------------------------------ 派生量
    @property
    def prompt_tokens(self) -> int:
        return sum(step.prompt_tokens for step in self.steps)

    @property
    def output_tokens(self) -> int:
        return sum(step.output_tokens for step in self.steps)

    @property
    def cache_read_tokens(self) -> int:
        return sum(step.cache_read_tokens for step in self.steps)

    @property
    def total_tokens(self) -> int:
        return sum(step.total_tokens or (step.prompt_tokens + step.output_tokens) for step in self.steps)

    @property
    def cache_hit_ratio(self) -> float:
        prompt = self.prompt_tokens
        return self.cache_read_tokens / prompt if prompt else 0.0

    @property
    def tool_result_chars(self) -> int:
        return sum(result.chars for result in self.tool_results.values())

    @property
    def tool_result_tokens(self) -> int:
        return sum(result.tokens for result in self.tool_results.values())

    @property
    def tool_schema_tokens(self) -> int:
        if not self.tool_schema_chars:
            return 0
        return max(1, round(self.tool_schema_chars / self.profile.ascii_chars_per_token))

    @property
    def system_tokens(self) -> int:
        return max(1, round(self.system_chars / self.profile.ascii_chars_per_token)) if self.system_chars else 0

    @property
    def max_prompt_tokens(self) -> int:
        return max((step.prompt_tokens for step in self.steps), default=0)

    @property
    def tool_counts(self) -> Counter[str]:
        return Counter(call.name for call in self.tool_calls)

    def model_usage(self) -> dict[str, tuple[int, int, int, int]]:
        """按模型聚合：``model -> (input, cache_read, output, total)``。"""
        out: dict[str, list[int]] = {}
        for step in self.steps:
            key = step.model or self.model or "unknown"
            bucket = out.setdefault(key, [0, 0, 0, 0])
            bucket[0] += step.input_tokens
            bucket[1] += step.cache_read_tokens
            bucket[2] += step.output_tokens
            bucket[3] += step.total_tokens or (step.prompt_tokens + step.output_tokens)
        return {key: (values[0], values[1], values[2], values[3]) for key, values in out.items()}

    def duplicate_calls(self) -> list[tuple[str, int, int]]:
        """返回重复调用：``(args_key, 次数, 重复结果 tokens)``。"""
        groups: dict[str, list[ToolCall]] = {}
        for call in self.tool_calls:
            groups.setdefault(call.args_key, []).append(call)
        out: list[tuple[str, int, int]] = []
        for key, calls in groups.items():
            if len(calls) <= 1:
                continue
            extra_tokens = sum(
                self.tool_results[c.call_id].tokens
                for c in calls[1:]
                if c.call_id in self.tool_results
            )
            out.append((key, len(calls), extra_tokens))
        out.sort(key=lambda item: item[2], reverse=True)
        return out

    def repeated_file_reads(self) -> list[tuple[str, int, int]]:
        """同一路径被 read 多次：``(path, 次数, 重复 tokens)``。"""
        groups: dict[tuple[str, str, str], list[ToolCall]] = {}
        for call in self.tool_calls:
            if call.name not in {"read", "read_file", "view"}:
                continue
            try:
                arguments = json.loads(call.arguments)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(arguments, dict):
                continue
            path = arguments.get("file_path") or arguments.get("path")
            if isinstance(path, str) and path:
                key = (path, str(arguments.get("offset", "")), str(arguments.get("limit", "")))
                groups.setdefault(key, []).append(call)
        out: list[tuple[str, int, int]] = []
        for (path, _, _), calls in groups.items():
            if len(calls) <= 1:
                continue
            extra_tokens = sum(
                self.tool_results[c.call_id].tokens
                for c in calls[1:]
                if c.call_id in self.tool_results
            )
            out.append((path, len(calls), extra_tokens))
        out.sort(key=lambda item: item[2], reverse=True)
        return out

    def describe(self) -> str:
        return (
            f"{self.session_id.removeprefix('session-')[:8]} {format_tokens(self.total_tokens)} tokens "
            f"({self.turns} turns/{len(self.steps)} steps, 缓存命中 {self.cache_hit_ratio:.0%})"
        )


_WORKSPACE_ESCAPE = re.compile(r"~([0-9A-Fa-f]{4})")


def _decode_workspace(name: str) -> str:
    """DSH 把 cwd 编码成目录名（~5DE5 这种），解回来便于按项目聚合。"""
    decoded = _WORKSPACE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), name)
    return decoded.strip("-") or name


def _fingerprint(value: Any) -> str:
    """稳定的内容指纹：用来判断前缀（工具集 / system prompt）有没有变。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_VOLATILE_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    re.compile(r"\b\d{10,13}\b"),
]


def _normalize_volatile(text: str) -> str:
    """把时间戳一类每步都变、但不改变语义的内容折叠，避免把动态注入误判成 prompt 改写。"""
    # 整行都是时间/环境快照的直接丢掉；行内残留的时间戳再抹掉
    lines = [line for line in text.splitlines() if not any(p.search(line) for p in _VOLATILE_PATTERNS)]
    text = "\n".join(lines)
    for pattern in _VOLATILE_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                total += len(str(block.get("text", "")))
        return total
    return 0


def _text_and_tokens(content: Any, profile: TokenProfile) -> tuple[int, int]:
    """一次遍历得到字符数与 token 估算，避免为长结果再拼一份完整字符串。"""
    if isinstance(content, str):
        return len(content), estimate_text_tokens(content, profile)
    if isinstance(content, list):
        chars = tokens = 0
        for block in content:
            if isinstance(block, str):
                chars += len(block)
                tokens += estimate_text_tokens(block, profile)
            elif isinstance(block, dict):
                text = str(block.get("text", ""))
                chars += len(text)
                tokens += estimate_text_tokens(text, profile)
        return chars, tokens
    return 0, 0


def _reasoning_and_text_chars(content: Any) -> tuple[int, int]:
    reasoning = text = 0
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in {"reasoning", "thinking"}:
                reasoning += len(str(block.get("text", "")))
            elif kind == "text":
                text += len(str(block.get("text", "")))
    elif isinstance(content, str):
        text = len(content)
    return reasoning, text


def load_session(path: str | Path, *, profile: TokenProfile | None = None) -> SessionRecord:
    path = Path(path)
    profile = profile or DEFAULT_PROFILE
    record = SessionRecord(
        path=path,
        session_id=path.parent.name,
        cwd="",
        created_at=0,
        provider="",
        model="",
        profile=profile,
    )
    step_map: dict[tuple[int, int], StepUsage] = {}
    first_time: int | None = None
    last_time: int | None = None
    current_model = ""

    for event in iter_events(path):
        record.events += 1
        event_type = event.get("type")
        raw_data = event.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        timestamp = event.get("time")
        if isinstance(timestamp, int):
            first_time = timestamp if first_time is None else min(first_time, timestamp)
            last_time = timestamp if last_time is None else max(last_time, timestamp)

        if event_type == "session":
            record.session_id = str(data.get("id", record.session_id))
            record.cwd = str(data.get("cwd", ""))
            created = data.get("createdAt")
            record.created_at = int(created) if isinstance(created, (int, float)) else 0
        elif event_type == "subagent/descriptor":
            record.provider = str(data.get("agentProvider") or data.get("provider") or record.provider)
            record.model = str(data.get("agentModel") or record.model)
        elif event_type == "request/header":
            header = data.get("header") if isinstance(data.get("header"), dict) else {}
            config = header.get("config") if isinstance(header.get("config"), dict) else {}
            record.provider = str(config.get("provider", record.provider))
            record.model = str(config.get("model", record.model))
            current_model = record.model
            tools = header.get("tools")
            if isinstance(tools, list):
                record.tool_names = [
                    str(tool.get("name", "")) for tool in tools if isinstance(tool, dict) and tool.get("name")
                ]
                record.tool_schema_chars = max(
                    record.tool_schema_chars,
                    len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))),
                )
            fingerprint = _fingerprint({"config": config, "tools": tools})
            if record._last_header_hash and fingerprint != record._last_header_hash:
                record.header_changes += 1
            record._last_header_hash = fingerprint
            names = frozenset(record.tool_names)
            if record._last_tool_names and names != record._last_tool_names:
                record.tool_changes += 1
                for name in names - record._last_tool_names:
                    record.tool_added[name] += 1
                for name in record._last_tool_names - names:
                    record.tool_removed[name] += 1
            record._last_tool_names = names
        elif event_type == "request/context":
            window = data.get("contextWindow")
            if isinstance(window, (int, float)):
                record.context_window = int(window)
            record.provider = str(data.get("provider", record.provider))
            record.model = str(data.get("model", record.model))
            current_model = record.model
        elif event_type == "model/selection":
            record.provider = str(data.get("provider", record.provider))
            record.model = str(data.get("model", record.model))
            current_model = record.model
        elif event_type == "system/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            # 真实 DSH 每一步都会写一条 system/message，取最大而不是累加
            record.system_chars = max(record.system_chars, _content_chars(message.get("content")))
            raw_text = _content_text(message.get("content"))
            raw_hash = _fingerprint(raw_text)
            stable_hash = _fingerprint(_normalize_volatile(raw_text))
            if record._last_system_raw_hash and raw_hash != record._last_system_raw_hash:
                if stable_hash != record._last_system_hash:
                    record.system_changes += 1
                else:
                    record.system_volatile_changes += 1
            record._last_system_raw_hash = raw_hash
            record._last_system_hash = stable_hash
        elif event_type == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            turn = int(data.get("turn", 0) or 0)
            step = int(data.get("step", 0) or 0)
            current = step_map.setdefault((turn, step), StepUsage(turn=turn, step=step))
            if current_model:
                current.model = current_model
            if usage:
                current.input_tokens = max(current.input_tokens, int(usage.get("inputTokens", 0) or 0))
                current.output_tokens = max(current.output_tokens, int(usage.get("outputTokens", 0) or 0))
                current.cache_read_tokens = max(
                    current.cache_read_tokens, int(usage.get("cacheReadTokens", 0) or 0)
                )
                current.total_tokens = max(current.total_tokens, int(usage.get("totalTokens", 0) or 0))
            reasoning, text = _reasoning_and_text_chars(message.get("content"))
            current.reasoning_chars += reasoning
            current.text_chars += text
        elif event_type == "tool/call":
            call_id = str(data.get("callId") or data.get("id") or "")
            name = str(data.get("name") or "")
            raw_args = data.get("arguments")
            if isinstance(raw_args, (dict, list)):
                raw_args = json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
            arguments = raw_args if isinstance(raw_args, str) else ""
            call = ToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
                turn=int(data.get("turn", 0) or 0),
                step=int(data.get("step", 0) or 0),
            )
            record.tool_calls.append(call)
            if name == "compress":
                record.compression_calls += 1
        elif event_type == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            call_id = str(message.get("toolCallId") or data.get("toolCallId") or "")
            content = message.get("content")
            chars, tokens = _text_and_tokens(content, profile)
            is_error = bool(data.get("error")) or bool(message.get("isError"))
            record.tool_results[call_id] = ToolResult(
                call_id=call_id,
                chars=chars,
                tokens=tokens,
                is_error=is_error,
            )
            if is_error:
                record.error_results += 1
        elif event_type == "compaction/start":
            record.compactions += 1
        elif event_type == "llm/retry":
            record.llm_retries += 1
        elif event_type in {"turn/start", "turn/end"}:
            record.turns = max(record.turns, int(data.get("turn", 0) or 0))

    record.steps = [step_map[key] for key in sorted(step_map)]
    if not record.cwd:
        # 真实会话的 session 事件不一定带 cwd，用 sessions/<workspace>/<session>/ 的目录名兜底
        workspace = path.parent.parent.name if path.parent.parent.name != "sessions" else ""
        record.cwd = _decode_workspace(workspace)
    if record.turns == 0 and record.steps:
        record.turns = len({step.turn for step in record.steps})
    if first_time is not None and last_time is not None:
        record.started_at = first_time
        record.ended_at = last_time
        record.duration_ms = last_time - first_time
    return record
