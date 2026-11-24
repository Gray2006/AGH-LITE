"""面板页面的 HTML（单独放，方便改样式）。"""

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agh-lite 面板</title>
<link rel="icon" href="/favicon.svg">
<style>
:root{
  --bg:#f6f8fa;--card:#ffffff;--text:#1f2328;--muted:#57606a;--border:#d0d7de;
  --accent:#1f6feb;--ok:#1a7f37;--bad:#cf222e;--warn:#bf8700;--soft:#eaeef2;
  --shadow:0 1px 3px rgba(31,35,40,.06);--radius:12px;
}
@media (prefers-color-scheme: dark){
  :root{--bg:#0d1117;--card:#161b22;--text:#e6edf3;--muted:#8b949e;--border:#30363d;
    --accent:#4493f8;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--soft:#21262d;
    --shadow:0 1px 3px rgba(0,0,0,.4)}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.6;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  font-variant-numeric:tabular-nums}
.wrap{max-width:860px;margin:0 auto;padding:2rem 1rem 4rem}
header{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start;flex-wrap:wrap;margin-bottom:1.2rem}
.brand{display:flex;gap:.7rem;align-items:center}
.logo{width:38px;height:38px;border-radius:10px;background:var(--accent);color:#fff;
  display:grid;place-items:center;font-size:1.25rem;font-weight:700}
h1{font-size:1.3rem;margin:0;letter-spacing:.2px}
.sub{color:var(--muted);font-size:.88rem;margin:.1rem 0 0}
code{background:var(--soft);padding:.1rem .35rem;border-radius:5px;font-size:.85em}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:1.1rem 1.25rem;margin:1rem 0;box-shadow:var(--shadow)}
h2{font-size:1rem;margin:0 0 .7rem;display:flex;align-items:center;gap:.5rem}
h2 .num{width:1.45rem;height:1.45rem;border-radius:50%;background:var(--soft);color:var(--muted);
  font-size:.8rem;display:grid;place-items:center}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
button{font:inherit;font-size:.92rem;padding:.45rem .95rem;border-radius:8px;cursor:pointer;
  border:1px solid var(--accent);background:var(--accent);color:#fff;transition:.15s}
button:hover{filter:brightness(1.08)}
button.ghost{background:transparent;color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input{font:inherit;font-size:.92rem;padding:.45rem .6rem;border:1px solid var(--border);
  border-radius:8px;width:100%;background:var(--card);color:var(--text)}
label{display:block;color:var(--muted);font-size:.85rem;margin-bottom:.3rem}
.muted{color:var(--muted);font-size:.9rem}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.7rem;margin:.8rem 0}
.stat{background:var(--soft);border-radius:10px;padding:.7rem .8rem}
.stat span{display:block;color:var(--muted);font-size:.8rem}
.stat b{font-size:1.35rem;font-weight:650}
.badge{display:inline-flex;align-items:center;gap:.35rem;font-size:.82rem;padding:.15rem .55rem;
  border-radius:999px;background:var(--soft);color:var(--muted)}
.badge.on{background:rgba(63,185,80,.15);color:var(--ok)}
.spinner{display:inline-block;width:.9em;height:.9em;border:2px solid var(--soft);
  border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-.1em}
@keyframes spin{to{transform:rotate(360deg)}}
ul.clean{margin:.3rem 0;padding-left:1.1rem}
ul.clean li{margin:.15rem 0}
#scanResult,#reportResult,#proxyInfo,#doctorResult{margin-top:.6rem}
#scanResult:empty,#reportResult:empty,#proxyInfo:empty,#doctorResult:empty{display:none}
details{margin-top:.5rem}
summary{cursor:pointer;color:var(--muted);font-size:.88rem}
@media (max-width:520px){.wrap{padding:1.2rem .8rem 3rem}h1{font-size:1.15rem}}
</style></head><body><div class="wrap">
<header>
  <div class="brand"><div class="logo">a</div>
    <div><h1>agh-lite</h1><p class="sub">DSH 个人版 token 工具 · v{{version}}</p></div>
  </div>
  <div style="text-align:right">
    <p class="sub">数据目录 <code id="home">…</code></p>
    <p class="sub" id="lastScan"></p>
  </div>
</header>

<section class="card">
  <h2><span class="num">1</span>看看钱花在哪</h2>
  <p class="muted">扫描本机 DSH 会话，不会上传任何内容。</p>
  <div class="row"><button id="scan">扫描我的会话</button><span id="scanState" class="muted"></span></div>
  <div id="scanResult"></div>
</section>

<section class="card">
  <h2><span class="num">2</span>生成一份报告</h2>
  <p class="muted">生成 HTML 报告，包含按天用量、费用和会话排行。</p>
  <div class="row"><button id="report">生成 HTML 报告</button><span id="reportState" class="muted"></span></div>
  <div id="reportResult"></div>
</section>

<section class="card">
  <h2><span class="num">3</span>自动省 token（本地代理）</h2>
  <p class="muted">把 DSH 的模型 provider 指向这个地址，代理会自动裁掉旧的超长工具结果、缓存重复请求。</p>
  <label for="upstream">上游地址</label>
  <input id="upstream" value="https://api.deepseek.com" spellcheck="false">
  <div class="row" style="margin-top:.7rem">
    <button id="startProxy">启动代理</button>
    <button id="stopProxy" class="ghost">停止</button>
    <button id="copyProxy" class="ghost">复制地址</button>
    <span id="proxyState" class="muted"></span>
  </div>
  <div id="proxyInfo"></div>
  <details><summary>怎么把 DSH 接到代理上</summary>
    <ol class="clean muted">
      <li>启动代理后，地址是 <code>http://127.0.0.1:8787/v1</code>（端口可在启动命令里用 --proxy-port 改）。</li>
      <li>在 DSH 里加一个 OpenAI 兼容 provider，baseUrl 填这个地址，key 随便填。</li>
      <li>不想用了就点停止，或删掉这个 provider。</li>
    </ol>
  </details>
</section>

<section class="card">
  <h2><span class="num">4</span>环境自检</h2>
  <p class="muted">目录、依赖、端口有问题时先看这里。</p>
  <div class="row"><button id="doctor" class="ghost">运行自检</button></div>
  <div id="doctorResult"></div>
</section>

<script>
const $ = (id) => document.getElementById(id);
let proxyUrl = 'http://127.0.0.1:8787/v1';
let proxyRunning = false;

async function api(path, body) {
  const res = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  return res.json();
}
function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function stat(label, value) {
  return '<div class="stat"><span>' + esc(label) + '</span><b>' + esc(value) + '</b></div>';
}
function clock(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
}

async function refresh() {
  const s = await api('/api/status');
  $('home').textContent = s.home;
  proxyUrl = 'http://127.0.0.1:' + s.proxy.port + '/v1';
  proxyRunning = s.proxy.running;
  $('lastScan').textContent = s.last_audit_at ? '上次扫描 ' + clock(s.last_audit_at) : '';
  if (s.proxy.running) {
    $('proxyState').innerHTML = '<span class="badge on">运行中</span> ' + esc(s.proxy.url);
    const p = s.proxy.stats;
    $('proxyInfo').innerHTML = '<div class="stats">' +
      stat('请求', p.requests) + stat('缓存命中', p.cache_hits) +
      stat('裁剪次数', p.trimmed_requests) + '</div>';
  } else {
    $('proxyState').innerHTML = '<span class="badge">未启动</span>';
    $('proxyInfo').textContent = '';
  }
}

$('scan').onclick = async () => {
  $('scan').disabled = true;
  $('scanState').innerHTML = '<span class="spinner"></span> 扫描中…';
  let seconds = 0;
  const timer = setInterval(() => {
    seconds += 1;
    $('scanState').innerHTML = '<span class="spinner"></span> 扫描中，已用 ' + seconds + ' 秒…';
  }, 1000);
  try {
    const r = await api('/api/audit', {days: 30});
    $('scanState').textContent = '';
    if (r.error) { $('scanResult').textContent = r.error; return; }
    let html = '<div class="stats">' +
      stat('会话', r.sessions) + stat('tokens', r.tokens) +
      stat('估算费用', r.cost) + stat('估算可省', r.savings) + '</div>';
    if (r.savings_usd && r.savings_usd !== '未配置价格') {
      html += '<p class="muted">可省金额约 ' + esc(r.savings_usd) + '</p>';
    }
    if (r.findings.length) {
      html += '<p class="muted">最该先动：</p><ul class="clean">' +
        r.findings.map(f => '<li>' + esc(f.title) + '：约 ' + esc(f.estimated) + '</li>').join('') + '</ul>';
    }
    if (r.workspaces && r.workspaces.length) {
      html += '<p class="muted">最烧 token 的项目：</p><ul class="clean">' +
        r.workspaces.map(w => '<li>' + esc(w.name) + '：' + w.sessions +
          ' 个会话 · ' + esc(w.tokens) + '</li>').join('') + '</ul>';
    }
    $('scanResult').innerHTML = html;
    refresh();
  } catch (e) {
    $('scanState').textContent = '失败：' + e;
  } finally {
    clearInterval(timer);
    $('scan').disabled = false;
  }
};

$('report').onclick = async () => {
  $('report').disabled = true;
  $('reportState').innerHTML = '<span class="spinner"></span> 生成中…';
  const r = await api('/api/report', {});
  $('report').disabled = false;
  $('reportState').textContent = '';
  if (r.error) { $('reportState').textContent = r.error; return; }
  $('reportResult').innerHTML = '<a href="/report" target="_blank">打开报告</a>';
  window.open('/report', '_blank');
};

$('startProxy').onclick = async () => {
  $('proxyState').innerHTML = '<span class="spinner"></span> 启动中…';
  const upstream = $('upstream').value.trim();
  try { localStorage.setItem('agh-lite-upstream', upstream); } catch (e) {}
  const r = await api('/api/proxy/start', {upstream: upstream});
  if (r.error) { $('proxyState').textContent = r.error; return; }
  refresh();
};
$('stopProxy').onclick = async () => { await api('/api/proxy/stop', {}); refresh(); };
$('copyProxy').onclick = async () => {
  try {
    await navigator.clipboard.writeText(proxyUrl);
    $('copyProxy').textContent = '已复制';
  } catch (e) {
    window.prompt('复制这个地址', proxyUrl);
  }
  setTimeout(() => { $('copyProxy').textContent = '复制地址'; }, 1500);
};
$('doctor').onclick = async () => {
  $('doctor').disabled = true;
  $('doctorResult').innerHTML = '<span class="spinner"></span> 检查中…';
  const r = await api('/api/doctor');
  $('doctor').disabled = false;
  $('doctorResult').innerHTML = r.lines.map(line =>
    '<div class="' + (line.startsWith('[x]') ? 'bad' : line.startsWith('[!]') ? 'muted' : 'ok') + '">' +
    esc(line) + '</div>').join('');
};

refresh();
try {
  const saved = localStorage.getItem('agh-lite-upstream');
  if (saved) $('upstream').value = saved;
} catch (e) {}
setInterval(() => { if (proxyRunning) refresh(); }, 4000);
</script></div></body></html>"""
