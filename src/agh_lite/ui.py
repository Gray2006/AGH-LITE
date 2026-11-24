"""傻瓜式本地面板：一个页面完成扫描、报告、代理开关。

只监听 127.0.0.1，不引入前端依赖；页面是内联 HTML + fetch。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditReport, audit_home
from .doctor import format_checks, run_checks
from .pricing import format_usd
from .proxy import ProxyConfig, build_server
from .sessions import resolve_dsh_home
from .tokens import format_tokens
from .ui_page import PAGE

FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='8' fill='#1f6feb'/>"
    "<text x='16' y='23' font-size='19' text-anchor='middle' fill='white' "
    "font-family='sans-serif'>a</text></svg>"
)


@dataclass
class UIState:
    home: Path
    report_path: Path
    proxy_port: int = 8787
    proxy: Any = None
    proxy_thread: threading.Thread | None = None
    last_audit: dict[str, Any] | None = None
    last_audit_at: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _UIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: _UIServer

    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send_html(200, PAGE.replace("{{version}}", __version__))
        elif self.path == "/favicon.svg":
            self._send_svg(FAVICON)
        elif self.path == "/api/status":
            self._send_json(200, self._status())
        elif self.path == "/api/doctor":
            checks = run_checks(self.server.state.home, proxy_port=self.server.state.proxy_port)
            self._send_json(
                200,
                {
                    "ok": all(check.status != "fail" for check in checks),
                    "lines": format_checks(checks).splitlines(),
                },
            )
        elif self.path == "/report":
            path = self.server.state.report_path
            if path.is_file():
                self._send_html(200, path.read_text(encoding="utf-8"))
            else:
                self._send_json(404, {"error": "还没有报告，先生成一份"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "JSON 解析失败"})
            return
        if self.path == "/api/audit":
            self._send_json(200, self._audit(days=_parse_days(body)))
        elif self.path == "/api/report":
            days = _parse_days(body)
            summary = self._audit(days=days)
            if "error" in summary:
                self._send_json(500, summary)
                return
            report = self._build_report(days=days)
            self.server.state.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.server.state.report_path.write_text(report.render_html(), encoding="utf-8")
            self._send_json(200, {"ok": True, "path": str(self.server.state.report_path)})
        elif self.path == "/api/proxy/start":
            self._send_json(200, self._start_proxy(str(body.get("upstream") or "")))
        elif self.path == "/api/proxy/stop":
            self._send_json(200, self._stop_proxy())
        else:
            self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------ 内部
    def _send_html(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_svg(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _status(self) -> dict[str, Any]:
        state = self.server.state
        running = state.proxy is not None
        stats = state.proxy.stats.to_dict() if state.proxy is not None else {}
        return {
            "home": str(state.home),
            "proxy": {
                "running": running,
                "port": state.proxy_port,
                "url": f"http://127.0.0.1:{state.proxy.server_address[1]}/v1" if running else "",
                "stats": stats,
            },
            "audit": state.last_audit,
            "last_audit_at": state.last_audit_at,
        }

    def _build_report(self, *, days: int | None) -> AuditReport:
        return audit_home(self.server.state.home, days=days)

    def _audit(self, *, days: int | None) -> dict[str, Any]:
        try:
            report = self._build_report(days=days)
        except Exception as exc:  # 面板把错误显示给用户
            return {"error": f"扫描失败：{exc}"}
        summary = {
            "sessions": report.totals.sessions,
            "tokens": format_tokens(report.totals.total_tokens),
            "cost": format_usd(report.totals.cost_usd) if report.totals.has_cost else "未配置价格",
            "savings": format_tokens(report.potential_savings),
            "savings_usd": (
                format_usd(report.calibrated_savings_usd or report.potential_savings_usd)
                if (report.calibrated_savings_usd is not None or report.potential_savings_usd is not None)
                else "未配置价格"
            ),
            "findings": [
                {
                    "title": finding.title,
                    "severity": finding.severity,
                    "estimated": format_tokens(finding.estimated_tokens),
                }
                for finding in report.findings[:5]
            ],
            "workspaces": [
                {
                    "name": row["workspace"],
                    "sessions": row["sessions"],
                    "tokens": format_tokens(row["tokens"]),
                }
                for row in report.workspace_rows(5)
            ],
        }
        self.server.state.last_audit = summary
        self.server.state.last_audit_at = int(time.time() * 1000)
        return summary

    def _start_proxy(self, upstream: str) -> dict[str, Any]:
        state = self.server.state
        if state.proxy is not None:
            return {"ok": True, "url": f"http://127.0.0.1:{state.proxy.server_address[1]}/v1"}
        if not upstream:
            return {"error": "先填上游地址"}
        try:
            config = ProxyConfig(upstream=upstream, port=state.proxy_port)
            server = build_server(config)
        except (ValueError, OSError) as exc:
            return {"error": f"启动失败（端口 {state.proxy_port} 可能被占用）：{exc}"}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state.proxy = server
        state.proxy_thread = thread
        return {"ok": True, "url": f"http://127.0.0.1:{server.server_address[1]}/v1"}

    def _stop_proxy(self) -> dict[str, Any]:
        state = self.server.state
        if state.proxy is None:
            return {"ok": True}
        state.proxy.shutdown()
        state.proxy.server_close()
        if state.proxy.cache is not None:
            state.proxy.cache.close()
        state.proxy = None
        state.proxy_thread = None
        return {"ok": True}


def _parse_days(body: dict[str, Any]) -> int | None:
    try:
        days = int(body.get("days", 30) or 0)
    except (TypeError, ValueError):
        days = 30
    return days if days > 0 else None


class _UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: UIState) -> None:
        self.state = state
        super().__init__(address, _UIHandler)


def serve(
    *,
    home: str | Path | None = None,
    port: int = 8790,
    proxy_port: int = 8787,
    open_browser: bool = True,
) -> None:
    resolved = resolve_dsh_home(home)
    report_path = Path.home() / ".dsh" / "agh-lite-report.html"
    state = UIState(home=resolved, report_path=report_path, proxy_port=proxy_port)
    server = _UIServer(("127.0.0.1", port), state)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"agh-lite 面板：{url}")
    print("按 Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        if state.proxy is not None:
            state.proxy.shutdown()
            state.proxy.server_close()
