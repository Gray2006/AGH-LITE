# agh-lite

[English](README.en.md) · [简体中文](README.md) · MIT

从 [AGH](https://github.com/Gray2006/AGH) 蒸馏出来的**个人版 token 经济工具箱**。离线读 DSH 会话，回答「钱花在哪、哪一步可以砍」；再把预算、缓存、去重、哈希链做成可嵌入的库。

> 不改 DSH 内核，不注册插件，不代理请求。审计只读本地会话，不写、不上传。

```
DSH 会话 (.jsonl.zstd)
   │  只读
   ▼
audit ── 重复调用 / 超大结果 / 前缀缓存 / 上下文膨胀 / 静态前缀
   │
   ▼
估算报告 ── tokens · 费用 · 可省上限
   │
   ├── pack    预算内打包 + 去重（时间按半衰期降权）
   ├── cache   内容寻址的响应缓存
   ├── budget  预授权 / 结算 / 落盘
   └── journal 哈希链账本
```

---

## 它解决什么

DSH 只记 token，不记钱。一个长会话跑下来，账单通常长这样：prompt 越滚越大，每一步都在为前面的历史重新付费，但没人知道哪一段是白付的。

常见的直觉是「工具输出太占地方」。实测不是：在 4.6B tokens 的样本里，工具结果只占 prompt 的 0.2%，真正贵的是**每步都重读的历史**和**每次请求都带着的 52K 工具定义**。

agh-lite 把这件事拆开：先告诉你钱花在哪，再把「怎么少花」做成能嵌进自己脚本的零件。

## 装

需要 Python 3.12+。

```bash
# 从 PyPI（发布后）
pip install agh-lite

# 要读 DSH 的 .jsonl.zstd 会话（推荐）
pip install 'agh-lite[dsh]'

# 从源码开发
pip install -e '.[dev]'
```

装完先自检，有问题它会直接说：

```bash
agh-lite doctor
```

## 用

### 面板（不想记命令就用这个）

```bash
agh-lite ui
```

会打开 `http://127.0.0.1:8790/`，一个页面三件事：

- **扫描我的会话**：点一下，十几秒后给出会话数、总 tokens、估算费用、可省多少、最该改的几件事，以及最烧 token 的项目
- **生成 HTML 报告**：生成后可直接打开（含按天 tokens 柱状图与费用折线）
- **启动本地代理**：填上游地址，一键启停；页面上显示请求数、缓存命中、裁剪次数

只监听本机，不对外网开放。`--port` 换面板端口，`--proxy-port` 换代理端口，`--no-browser` 不自动开浏览器。

不想开终端：Windows 双击 `scripts/agh-lite-ui.cmd`，macOS/Linux 跑 `scripts/agh-lite-ui.sh`。

### 审计

```bash
# 默认读 $DSH_HOME 或 ~/.dsh
agh-lite audit

# 最近 7 天、最多 30 个会话，写 Markdown
agh-lite audit --days 7 --limit 30 --out runs/audit.md

# 机器可读
agh-lite audit --format json

# 自包含 HTML，方便发给别人或存档
agh-lite audit --format html --out runs/audit.html

# 用自己的价格表估算费用
agh-lite audit --pricing my-prices.json

# 真实账单对账（默认自动找 $DSH_HOME/.dshw-usage.json）
agh-lite audit --usage /path/to/.dshw-usage.json

# 不想对账
agh-lite audit --no-usage

# 分享前把本机绝对路径脱敏
agh-lite audit --redact --out runs/audit.md
```

报告里有什么：

| 部分 | 内容 |
|---|---|
| 总览 | 总 tokens、prompt / output、缓存命中、工具 schema、估算费用 |
| 最该先动的几件事 | 每条问题给证据、做法、估算省多少 tokens / 多少钱 |
| 按模型 | 各模型的 tokens 占比与估算费用 |
| 与真实账单对账 | 实际花费、估算花费、差额，以及按天/按模型的真实费用 |
| 按天用量 | HTML 报告里的 tokens 柱子图（最近 14 天） |
| 按 workspace | 哪个项目目录最烧 token，会话数与估算费用 |
| 会话排行 | 最贵的会话及其缓存命中、重复量 |

### 打包

```bash
agh-lite pack --input segments.json --budget 60000 --render
```

输入是段列表：

```json
{
  "segments": [
    {"id": "rules", "text": "...", "kind": "system", "priority": 0.9},
    {"id": "task", "text": "...", "role": "user", "priority": 0.8},
    {"id": "old", "text": "...", "priority": 0.3, "timestamp": 1730000000},
    {"id": "noise", "text": "...", "priority": 0.1}
  ]
}
```

规则：`system` / `pinned` / 末尾 `protect_last` 段先占预算；其余按 `priority + kind 权重 + 时间衰减` 贪心装入，装不下带原因丢弃。时间权重是半衰期衰减，最新 0.3，一个时间跨度前减半。

### 缓存、预算、日志

这三个是库，接在自己的调用脚本里：

```python
from agh_lite.budget import TokenBudget
from agh_lite.cache import ResponseCache

budget = TokenBudget(limit_tokens=2_000_000)
cache = ResponseCache("~/.dsh/agh-lite-cache.sqlite")

key = cache.make_key(model, messages, params)
hit = cache.get(key)
if hit is not None:
    return hit.response

lease = budget.reserve(owner="chat", max_tokens=estimated_prompt + max_output, key=key)
try:
    response, usage = call_llm(messages)
except Exception:
    budget.release(lease)
    raise
budget.settle(lease, actual_tokens=usage.total_tokens, key=key)
cache.put(key, response, prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)
```

`budget.save(path)` / `TokenBudget.load(path)` 可以把窗口落盘，重启后已结算的幂等键仍然有效。

哈希链日志：

```bash
agh-lite journal append --kind note --data '{"msg":"..."}'
agh-lite journal verify
```

## 它怎么工作

| 文件 | 作用 |
|---|---|
| `sessions.py` | 解析 DSH 会话（明文与多帧 zstd），抽 usage、工具调用、压缩、重试 |
| `audit.py` | 八条规则的离线审计，中文报告与 JSON |
| `pricing.py` | 模型价格表与费用估算，未知模型不猜价 |
| `usage.py` | 读 DSH 的 `.dshw-usage.json`，按天/模型聚合真实花费 |
| `pack.py` | 精确 + 近似去重，预算内选择 |
| `cache.py` | SQLite 内容寻址缓存，TTL 与命中统计 |
| `budget.py` | 单窗口预授权 / 结算 / 违规 / TTL 回收 / 落盘 |
| `journal.py` | 哈希链日志 |
| `tokens.py` | 无 usage 时的 token 估算（CJK / ASCII 分桶） |

口径：

- `prompt = inputTokens + cacheReadTokens`，`output` 为模型输出，`total` 取会话里的 `totalTokens`；
- 费用 = `input × 输入价 + cacheRead × 缓存价 + output × 输出价`，单价按百万 tokens；
- 缓存命中的 prompt 按缓存价计，这是长会话里最大的一笔折扣；
- `--days` 按会话内最后事件时间筛选，文件 mtime 只做粗筛；
- 同一模型跨 provider 前缀归一，`deepseek/deepseek-v4.1-flash` 与 `deepseek-v4.1-flash` 视为同一个；
- 有 `.dshw-usage.json` 时，估算费用会和真实账本对账，差额单独列出；差额超过 30% 会给一个校准系数，把「可省」换算成真实金额。

审计规则：

| 规则 | 触发 | 估算 |
|---|---|---|
| `duplicate_tool_calls` | 同会话内 `name + arguments` 完全一致 | 重复结果的 token |
| `oversized_tool_outputs` | 单条结果 > 8K tokens | 超出部分 × 60% |
| `low_prefix_cache` | prompt ≥ 50K 且命中 < 40% | 该会话 prompt × 10% |
| `unstable_tools` | 工具集反复变化，且命中 < 70% | 相关 prompt × 10% |
| `unstable_system` | system prompt 语义反复变化，且命中 < 70% | 相关 prompt × 10% |
| `volatile_system_prompt` | system 每步只变时间戳/环境快照，命中 < 50% | 相关 prompt × 15% |
| `static_prefix_bloat` | system + 工具 schema ≥ 12K | 静态前缀 × 步数 × 15% |
| `context_bloat` | 每步平均 prompt ≥ 120K，或 20 步以上从不压缩 | 相关会话 prompt × 10% |
| `heavy_reasoning` | output > 5K 且 reasoning 文本 > 正文 3 倍 | output × 20% |
| `provider_retries` | 单会话 `llm/retry` ≥ 3 | 0（无法确认计费） |
| `repeated_file_reads` | 同路径同区间 read ≥ 2 次 | 重复结果 token |

各项互相重叠，`potential_savings` 不相加，按总量 25% 封顶。

## 真实数据

在约 180 个 DSH 会话上跑过一次全量审计（13.6 秒）：

| 指标 | 数值 |
|---|---|
| 总 tokens | 4.60B |
| prompt / output | 4.58B / 17.0M |
| 前缀缓存命中 | 90% |
| 平均每步 prompt | 149.5K |
| 工具 schema（单次请求） | 52.7K（122 个工具） |
| 系统提示（单次请求） | 6.5K |
| 手动 compress / 自动压缩 | 351 / 755 |
| 供应商重试 | 5309 次 |
| 估算费用 | 约 $245（可省约 $36.55） |
| 真实账单对账 | 2041 笔；估算比实际低约 60%，默认价未含中转加价 |

> 对账一跑就暴露出问题：内置价格表是官方参考价，实际通道有加价，`--pricing` 得换成自己的单价，否则「可省」会被低估。

审计给出的前三件事：

1. **上下文膨胀**：48 个会话平均每步 prompt 超过 120K，最高 282K，估算可省 381M tokens。
2. **静态前缀过重**：每次请求都带着约 59K 的 system + 工具 schema，估算 273M。
3. **部分会话前缀缓存低**：13 个会话命中率低于 40%，估算 28M。

结论和直觉相反：**工具结果只占 prompt 的 0.2%，真正贵的是每步都重读的历史和 52K 的工具定义。**

## 能主动省 token 吗

能，但要你显式接一下：`agh-lite proxy` 起一个本地 OpenAI 兼容代理，把 DSH 的 provider 指过来。

```bash
# 上游填你平时用的地址；默认只监听 127.0.0.1
agh-lite proxy --upstream https://api.deepseek.com

# 先只缓存、不改请求，验证连通
agh-lite proxy --upstream https://api.deepseek.com --no-trim

# 调裁剪力度：单条旧 tool 结果最多留 2000 字，最近 6 条不动
agh-lite proxy --upstream https://api.deepseek.com --max-tool-chars 2000 --keep-recent 6

# 流式请求也记账（给上游加 include_usage）
agh-lite proxy --upstream https://api.deepseek.com --stream-usage
```

代理做两件事，都可单独关：

| 动作 | 默认 | 说明 |
|---|---|---|
| 裁剪 | 开 | 较早的 `role=tool` 长结果截成头尾两段，最近 8 条不动；不改 system、user、assistant |
| 缓存 | 开 | 非流式请求按请求体哈希命中，第二次不打上游；流式请求原样转发 |

然后在 DSH 里加一个 OpenAI 兼容 provider，`baseUrl` 填 `http://127.0.0.1:8787/v1`，key 随便填（代理不校验），模型名照上游写。不想要了就删掉这个 provider，DSH 本身没被改。

边界：代理只监听本机、不记录请求正文、不落盘 Authorization；裁剪会改变模型看到的历史，重要会话可以先 `--no-trim` 只开缓存。健康检查：`curl http://127.0.0.1:8787/agh-lite/health`。

每次请求的节省会写进 `~/.dsh/agh-lite-proxy.jsonl`，随时汇总：

```bash
agh-lite proxy --stats
agh-lite proxy --stats --json
```

输出裁剪字符数、缓存省下的 tokens、按价格表折合的费用。

不想走代理时，库模式仍然可用：`cache` / `pack` / `budget` 接进自己的调用脚本，命中缓存不重复付费、超预算直接拒绝。

## 与 AGH 的关系

| AGH | agh-lite | 为什么留 |
|---|---|---|
| `ledger.py` | `budget.py` | 预授权 + 结算 + TTL 回收，个人也要防手滑 |
| `bus.py` 去重 | `pack.py` | 只留内容去重，去掉跨进程投递 |
| `billing.py` | `pricing.py` | 把「结算后扣款」换成「这些 token 值多少钱」 |
| `forgetting.py` | `pack.py` | 借用半衰期衰减，给旧片段降权 |
| `timestamp.py` | `sessions.py` | 用会话内事件时间，而不是文件 mtime |
| `crypto.py` / `frozen.py` | `journal.py` | 只留「可验证的本地账」，去掉签名网关 |
| `eventlog.py` | `journal.py` | 哈希链保留，L1 重放去掉 |
| `contracts.py` / `arena.py` / `gate.py` | 无 | 多智能体治理，个人版用不上 |

## 已知边界

- **默认不代理请求**。DSH 发行版没有公开的请求钩子，硬接只能改内核或 MITM；审计离线做，其余能力做成库。想要自动省，显式启动 `agh-lite proxy` 并把 provider 指过来。
- **费用默认是估算**。价格表在 `pricing.py`，未命中的模型不计价，会列进 `unknown_models`；有 `.dshw-usage.json` 时可以拉真实账本对账，但两边口径不同，差额不等于账错。
- **代理会改请求**。裁剪只动较早的 tool 结果，但模型看到的历史确实变了；先 `--no-trim` 验证连通，重要会话再决定开不开。
- **缓存只认同一个请求**。输入逐字节规范化后一致才命中，不做语义匹配；有副作用的工具链不该缓存。
- **哈希链只防「改一条看不出来」**，防不了有写权限的人重写整条链。
- **预算只维护一个窗口**，日/月边界由调用方重置；不承诺分布式 exactly-once。
- **审计报告默认不公开**。本机跑出来的报告含真实会话统计，留在本地；仓库只放聚合数字。

## 开发

```bash
python -m pytest
python -m ruff check .
```

```
src/agh_lite/
  tokens.py     token 估算（CJK / ASCII / other 分桶）
  budget.py     预算租约、结算、落盘
  cache.py      SQLite 响应缓存
  pack.py       上下文打包与去重
  journal.py    哈希链日志
  sessions.py   DSH 会话解析（含 zstd 多帧）
  pricing.py    模型价格表与费用估算
  usage.py      DSH 真实用量账本对账
  proxy.py      本地 OpenAI 兼容代理（裁剪 + 缓存）
  ui.py         本地面板
  doctor.py     环境自检
  audit.py      离线审计规则与报告
  cli.py        命令行入口
```

更细的设计取舍见 [docs/DESIGN.md](docs/DESIGN.md)。

### 发布（维护者）

```bash
python -m build          # 产出 dist/ 下的 sdist 与 wheel
```

PyPI 上传走 GitHub Actions 的 `publish` workflow（手动触发，Trusted Publishing，不需要 token）：
先在 PyPI 给项目配置 Trusted Publisher（仓库 `Gray2006/AGH-LITE`、workflow `publish.yml`、environment `pypi`），
再在 Actions 里点 Run workflow；勾选 TestPyPI 可先发测试源。

## 作者

本项目由 [Gray2006](https://github.com/Gray2006) 与 AI 编码助手协作完成。

## 许可

MIT
