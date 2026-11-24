"""环境自检：把「为什么跑不起来」变成一句人话。"""

from __future__ import annotations

import importlib.util
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from .sessions import SessionReadError, find_session_files, resolve_dsh_home


@dataclass
class Check:
    name: str
    status: str  # ok / warn / fail
    detail: str


def _port_free(port: int) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True, f"127.0.0.1:{port} 可用"
        except OSError as exc:
            return False, f"127.0.0.1:{port} 被占用（{exc}），代理可用 --port 换一个"


def run_checks(home: str | Path | None = None, *, proxy_port: int = 8787) -> list[Check]:
    checks: list[Check] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(Check("Python", "ok", f"{version}（需要 3.12+）"))

    try:
        resolved = resolve_dsh_home(home)
    except (FileNotFoundError, SessionReadError) as exc:
        checks.append(Check("DSH 数据目录", "fail", f"{exc}；用 --home 指定，或先跑一次 DSH"))
        resolved = None
    else:
        checks.append(Check("DSH 数据目录", "ok", str(resolved)))
        sessions = resolved / "sessions"
        if sessions.is_dir():
            files = find_session_files(sessions, limit=1)
            count = "至少 1 个" if files else "0 个（还没用过 DSH 可忽略）"
            checks.append(Check("会话目录", "ok", f"{sessions}，{count}会话"))
        else:
            checks.append(Check("会话目录", "warn", f"没找到 {sessions}"))
        if resolved.is_dir():
            checks.append(Check("数据目录可写", "ok" if _writable(resolved) else "warn", str(resolved)))

    zstd = importlib.util.find_spec("zstandard") is not None
    checks.append(
        Check(
            "zstandard",
            "ok" if zstd else "warn",
            "已安装" if zstd else "没装；读 .jsonl.zstd 会话会失败，pip install 'agh-lite[dsh]'",
        )
    )

    free, detail = _port_free(proxy_port)
    checks.append(Check("代理端口", "ok" if free else "warn", detail))
    return checks


def _writable(path: Path) -> bool:
    try:
        probe = path / ".agh-lite-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def format_checks(checks: list[Check]) -> str:
    label = {"ok": "[ok]", "warn": "[!]", "fail": "[x]"}
    lines = ["agh-lite 自检", ""]
    for check in checks:
        lines.append(f"{label.get(check.status, '[?]')} {check.name}：{check.detail}")
    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]
    lines.append("")
    if failures:
        lines.append(f"{len(failures)} 项必须修，修完再跑一次。")
    elif warnings:
        lines.append(f"{len(warnings)} 项建议处理，不影响基本使用。")
    else:
        lines.append("都正常。")
    return "\n".join(lines)
