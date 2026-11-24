"""傻瓜式本地面板：一个页面完成扫描、报告、代理开关。

只监听 127.0.0.1，不引入前端依赖；页面是内联 HTML + fetch。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .audit import AuditReport, audit_home
from .pricing import format_usd
from .proxy import ProxyConfig, build_server
from .sessions import resolve_dsh_home
from .tokens import format_tokens

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agh-lite 面板</title>
<style>
body{font-family:system-ui,"Segoe UI",sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;
color:#1f2328;line-height:1.6;background:#f6f8fa}
h1{font-size:1.5rem;margin-bottom:.2rem}h2{font-size:1.05rem;margin:0 0 .6rem}
.card{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:1rem 1.2rem;margin:1rem 0}
button{font-size:.95rem;padding:.45rem .9rem;border-radius:6px;border:1px solid #1f6feb;
background:#1f6feb;color:#fff;cursor:pointer}button.ghost{background:#fff;color:#1f6feb}
button:disabled{opacity:.5;cursor:default}input{font-size:.95rem;padding:.4rem .5rem;
border:1px solid #d0d7de;border-radius:6px;width:100%;box-sizing:border-box}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.muted{color:#57606a;font-size:.9rem}.ok{color:#1a7f37}.bad{color:#cf222e}
pre{background:#f6f8fa;padding:.6rem;border-radius:6px;overflow:auto;font-size:.85rem}
.big{font-size:1.3rem;font-weight:600}
</style></head><body>
<h1>agh-lite</h1>
<p class="muted">DSH 个人版 token 工具 · 数据目录 <code id="home">…</code></p>

<div class="card">
  <h2>1. 看看钱花在哪</h2>
  <div class="row"><button id="scan">扫描我的会话</button><span id="scanState" class="muted"></span></div>
  <div id="scanResult"></div>
</div>

<div class="card">
  <h2>2. 生成一份报告</h2>
  <div class="row"><button id="report">生成 HTML 报告</button><span id="reportState" class="muted"></span></div>
  <div id="reportResult"></div>
</div>

<div class="card">
  <h2>3. 自动省 token（本地代理）</h2>
  <p class="muted">把 DSH 的模型 provider 指向这个地址，代理会自动裁掉旧的超长工具结果、缓存重复请求。</p>
  <label class="muted">上游地址</label>
  <input id="upstream" value="https://api.deepseek.com">
  <div class="row" style="margin-top:.6rem">
    <button id="startProxy">启动代理</button>
    <button id="stopProxy" class="ghost">停止</button>
    <button id="copyProxy" class="ghost">复制地址</button>
    <span id="proxyState" class="muted"></span>
  </div>
  <div id="proxyInfo"></div>
</div>

<details class="card"><summary>怎么把 DSH 接到代理上</summary>
<ol class="muted">
<li>启动代理后，地址是 <code>http://127.0.0.1:8787/v1</code>。</li>
<li>在 DSH 里加一个 OpenAI 兼容 provider，baseUrl 填这个地址，key 随便填。</li>
<li>不想用了就删掉这个 provider，或点上面的停止。</li>
</ol></details>

<script>
const $ = (id) => document.getElementById(id);
async function api(path, body) {
  const res = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  return res.json();
}
async function refresh() {
  const s = await api('/api/status');
  $('home').textContent = s.home;
  if (s.proxy.running) {
    $('proxyState').textContent = '运行中 ' + s.proxy.url;
    $('proxyState').className = 'ok';
    const p = s.proxy.stats;
    $('proxyInfo').innerHTML = '<p class="muted">请求 ' + p.requests + ' · 缓存命中 ' +
      p.cache_hits + ' · 裁剪 ' + p.trimmed_requests + ' 次</p>';
  } else {
    $('proxyState').textContent = '未启动'; $('proxyState').className = 'muted';
    $('proxyInfo').textContent = '';
  }
}
$('scan').onclick = async () => {
  $('scan').disabled = true; $('scanState').textContent = '扫描中，十几秒…';
  try {
    const r = await api('/api/audit', {days: 30});
    $('scanState').textContent = '';
    if (r.error) { $('scanResult').textContent = r.error; return; }
    let html = '<p><span class="big">' + r.sessions + '</span> 个会话 · ' +
      r.tokens + ' · 估算费用 ' + r.cost + '</p>';
    html += '<p>估算可省 <b>' + r.savings + '</b>（' + r.savings_usd + '）</p>';
    if (r.findings.length) {
      html += '<ul>' + r.findings.map(f => '<li>[' + f.severity + '] ' + f.title +
        '：约 ' + f.estimated + '</li>').join('') + '</ul>';
    }
    if (r.workspaces && r.workspaces.length) {
      html += '<p class="muted">最烧 token 的项目：</p><ul>' + r.workspaces.map(w =>
        '<li>' + w.name + '：' + w.sessions + ' 个会话 · ' + w.tokens + '</li>').join('') + '</ul>';
    }
    $('scanResult').innerHTML = html;
  } catch (e) { $('scanState').textContent = '失败：' + e; }
  $('scan').disabled = false;
};
$('report').onclick = async () => {
  $('report').disabled = true; $('reportState').textContent = '生成中…';
  const r = await api('/api/report', {});
  $('report').disabled = false;
  if (r.error) { $('reportState').textContent = r.error; return; }
  $('reportState').textContent = '已生成';
  $('reportResult').innerHTML = '<a href="/report" target="_blank">打开报告</a>';
};
$('startProxy').onclick = async () => {
  $('proxyState').textContent = '启动中…';
  const r = await api('/api/proxy/start', {upstream: $('upstream').value});
  if (r.error) { $('proxyState').textContent = r.error; return; }
  refresh();
};
$('stopProxy').onclick = async () => { await api('/api/proxy/stop', {}); refresh(); };
$('copyProxy').onclick = async () => {
  const url = 'http://127.0.0.1:8787/v1';
  try {
    await navigator.clipboard.writeText(url);
    $('copyProxy').textContent = '已复制';
  } catch (e) {
    window.prompt('复制这个地址', url);
  }
  setTimeout(() => { $('copyProxy').textContent = '复制地址'; }, 1500);
};
refresh();
</script></body></html>"""


@dataclass
class UIState:
    home: Path
    report_path: Path
    proxy: Any = None
    proxy_thread: threading.Thread | None = None
    last_audit: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _UIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: _UIServer

    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send_html(200, PAGE)
        elif self.path == "/api/status":
            self._send_json(200, self._status())
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
                "url": f"http://127.0.0.1:{state.proxy.server_address[1]}/v1" if running else "",
                "stats": stats,
            },
            "audit": state.last_audit,
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
        return summary

    def _start_proxy(self, upstream: str) -> dict[str, Any]:
        state = self.server.state
        if state.proxy is not None:
            return {"ok": True, "url": f"http://127.0.0.1:{state.proxy.server_address[1]}/v1"}
        if not upstream:
            return {"error": "先填上游地址"}
        try:
            config = ProxyConfig(upstream=upstream, port=8787)
            server = build_server(config)
        except (ValueError, OSError) as exc:
            return {"error": f"启动失败：{exc}"}
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


def serve(*, home: str | Path | None = None, port: int = 8790, open_browser: bool = True) -> None:
    resolved = resolve_dsh_home(home)
    report_path = Path.home() / ".dsh" / "agh-lite-report.html"
    state = UIState(home=resolved, report_path=report_path)
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
