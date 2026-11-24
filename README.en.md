# agh-lite

[English](README.en.md) · [简体中文](README.md) · MIT

A personal **token-economy toolkit** distilled from [AGH](https://github.com/Gray2006/AGH). It reads DSH session logs offline to answer "where did the money go, and what can be cut", then packages budgeting, caching, deduplication, and a hash-chain ledger as embeddable libraries.

> No DSH kernel patches, no plugin registration, no request proxying. The audit is read-only, local, and never uploads anything.

```
DSH session (.jsonl.zstd)
   │  read-only
   ▼
audit ── duplicate calls / oversized results / prefix cache / context bloat / static prefix
   │
   ▼
report ── tokens · cost · savings ceiling
   │
   ├── pack    budgeted packing + dedupe (half-life recency)
   ├── cache   content-addressed response cache
   ├── budget  reserve / settle / persist
   └── journal hash-chain ledger
```

---

## The problem it solves

DSH records tokens, not money. A long session ends up with a prompt that keeps growing, every step paying again for history nobody checked, and no way to tell which part was wasted.

The common intuition is "tool output is the problem". Measurement says otherwise: across 4.6B tokens, tool results are 0.2% of the prompt; the expensive parts are **history re-read on every step** and **the 52K tool schema carried on every request**.

agh-lite splits that into two pieces: show where the money goes, then make "spend less" embeddable in your own scripts.

## Install

Python 3.12+.

```bash
# plain-text sessions only, no dependencies
pip install -e .

# read DSH .jsonl.zstd sessions (recommended)
pip install -e '.[dsh]'

# development
pip install -e '.[dev]'
```

## Usage

### Audit

```bash
# defaults to $DSH_HOME or ~/.dsh
agh-lite audit

# last 7 days, at most 30 sessions, Markdown output
agh-lite audit --days 7 --limit 30 --out runs/audit.md

# machine-readable
agh-lite audit --format json

# self-contained HTML for sharing or archiving
agh-lite audit --format html --out runs/audit.html

# custom price table
agh-lite audit --pricing my-prices.json

# reconcile with the real ledger (auto-discovers $DSH_HOME/.dshw-usage.json)
agh-lite audit --usage /path/to/.dshw-usage.json

# skip reconciliation
agh-lite audit --no-usage

# redact absolute paths before sharing
agh-lite audit --redact --out runs/audit.md
```

What the report contains:

| Section | Contents |
|---|---|
| Overview | total tokens, prompt / output, cache hit rate, tool schema, estimated cost |
| Top findings | evidence, recommendation, estimated tokens / money saved per issue |
| By model | token share and estimated cost per model |
| Reconciliation | actual spend, estimated spend, delta, and real cost by day/model |
| Daily usage | tokens bar chart in the HTML report (last 14 days) |
| By workspace | which project directory burns the most, sessions and estimated cost |
| Session ranking | most expensive sessions with cache hit and duplicates |

### Pack

```bash
agh-lite pack --input segments.json --budget 60000 --render
```

Input is a list of segments:

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

`system`, `pinned`, and the last `protect_last` segments take the budget first; the rest are greedily selected by `priority + kind weight + time decay`, and anything that does not fit is dropped with a reason. Recency decays with a half-life: newest 0.3, halved one time span back.

### Cache, budget, journal

These are libraries to call from your own script:

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

`budget.save(path)` / `TokenBudget.load(path)` persist a window; settled idempotency keys still hold after a restart.

Hash-chain journal:

```bash
agh-lite journal append --kind note --data '{"msg":"..."}'
agh-lite journal verify
```

## How it works

| File | Role |
|---|---|
| `sessions.py` | Parse DSH sessions (plain and multi-frame zstd), extract usage, tool calls, compaction, retries |
| `audit.py` | Eight-rule offline audit, Chinese Markdown and JSON |
| `pricing.py` | Model price table and cost estimation; unknown models are not guessed |
| `usage.py` | Read DSH's `.dshw-usage.json`, aggregate real spend by day/model |
| `pack.py` | Exact + near dedupe, budgeted selection |
| `cache.py` | SQLite content-addressed cache with TTL and hit stats |
| `budget.py` | Single-window reserve / settle / violations / TTL reclaim / persistence |
| `journal.py` | Hash-chain log |
| `tokens.py` | Token estimation without usage (CJK / ASCII buckets) |

Accounting conventions:

- `prompt = inputTokens + cacheReadTokens`, `output` is model output, `total` uses the session's `totalTokens`;
- cost = `input × input price + cacheRead × cache price + output × output price`, per million tokens;
- cached prompt tokens are billed at the cache price — the biggest discount in long sessions;
- `--days` filters by the session's last event time; file mtime is only a coarse prefilter;
- the same model behind different provider prefixes is normalized: `deepseek/deepseek-v4.1-flash` and `deepseek-v4.1-flash` are one model;
- when `.dshw-usage.json` is present, the estimate is reconciled against the real ledger and the delta is listed; if the delta exceeds 30%, a calibration factor converts the savings ceiling into real money.

Audit rules:

| Rule | Trigger | Estimate |
|---|---|---|
| `duplicate_tool_calls` | identical `name + arguments` within a session | duplicate result tokens |
| `oversized_tool_outputs` | one result > 8K tokens | excess × 60% |
| `low_prefix_cache` | prompt ≥ 50K and hit < 40% | session prompt × 10% |
| `unstable_tools` | tool set keeps changing, hit < 70% | related prompt × 10% |
| `unstable_system` | system prompt semantics keep changing, hit < 70% | related prompt × 10% |
| `volatile_system_prompt` | system only changes timestamps/env snapshots, hit < 50% | related prompt × 15% |
| `static_prefix_bloat` | system + tool schema ≥ 12K | static prefix × steps × 15% |
| `context_bloat` | average prompt ≥ 120K, or 20+ steps without compaction | related prompt × 10% |
| `heavy_reasoning` | output > 5K and reasoning > 3× text | output × 20% |
| `provider_retries` | ≥ 3 `llm/retry` in one session | 0 (billing unconfirmed) |
| `repeated_file_reads` | same path and range read ≥ 2 times | duplicate result tokens |

Estimates overlap; `potential_savings` is not a sum and is capped at 25% of the total.

## Real numbers

A full audit over ~180 DSH sessions (13.6 s):

| Metric | Value |
|---|---|
| Total tokens | 4.60B |
| prompt / output | 4.58B / 17.0M |
| Prefix cache hit | 90% |
| Average prompt per step | 149.5K |
| Tool schema per request | 52.7K (122 tools) |
| System prompt per request | 6.5K |
| Manual / automatic compaction | 351 / 755 |
| Provider retries | 5309 |
| Estimated cost | ~$245 (savings ceiling ~$36.55) |
| Ledger reconciliation | 2041 entries; the estimate is ~60% below actual because relays add a markup |

> Reconciliation immediately exposed the gap: the built-in table uses official list prices, while real routes charge more. Replace it with `--pricing`, otherwise the savings ceiling is understated.

Top three findings:

1. **Context bloat**: 48 sessions average over 120K prompt per step, peak 282K; ~381M tokens recoverable.
2. **Static prefix bloat**: ~59K of system + tool schema on every request; ~273M tokens.
3. **Low prefix cache**: 13 sessions below 40% hit rate; ~28M tokens.

The counter-intuitive conclusion: **tool results are 0.2% of the prompt; the real cost is history re-read every step and the 52K tool schema.**

## Can it actively save tokens?

Yes, once you point DSH at it: `agh-lite proxy` starts a local OpenAI-compatible proxy and DSH talks to that instead of the upstream.

```bash
# upstream is whatever you normally use; listens on 127.0.0.1 only
agh-lite proxy --upstream https://api.deepseek.com

# cache only, no request rewriting, to verify connectivity
agh-lite proxy --upstream https://api.deepseek.com --no-trim

# tune trimming: keep 2000 chars per old tool result, leave the 6 newest alone
agh-lite proxy --upstream https://api.deepseek.com --max-tool-chars 2000 --keep-recent 6

# account usage for streaming requests too (adds include_usage upstream)
agh-lite proxy --upstream https://api.deepseek.com --stream-usage
```

The proxy does two things, each can be turned off:

| Action | Default | Notes |
|---|---|---|
| Trim | on | older `role=tool` results are cut to head + tail; the newest 8 messages are untouched; system/user/assistant are never rewritten |
| Cache | on | non-streaming requests hit a body-hash cache and never reach upstream twice; streaming requests pass through unchanged |

Then add an OpenAI-compatible provider in DSH with `baseUrl` = `http://127.0.0.1:8787/v1` and any key (the proxy does not check it); use the upstream model names. Remove the provider to opt out; DSH itself is untouched.

Limits: local-only binding, no request bodies logged, no Authorization persisted; trimming changes the history the model sees, so for important sessions start with `--no-trim`. Health check: `curl http://127.0.0.1:8787/agh-lite/health`.

Every request's savings are appended to `~/.dsh/agh-lite-proxy.jsonl`; summarize anytime:

```bash
agh-lite proxy --stats
agh-lite proxy --stats --json
```

It reports trimmed characters, tokens saved by the cache, and the money equivalent from the price table.

Without the proxy, the libraries still work: wire `cache` / `pack` / `budget` into your own call path.

## Relation to AGH

| AGH | agh-lite | Why keep it |
|---|---|---|
| `ledger.py` | `budget.py` | reserve + settle + TTL reclaim; individuals still slip |
| `bus.py` dedupe | `pack.py` | content dedupe only, drop cross-process delivery |
| `billing.py` | `pricing.py` | "what did these tokens cost" instead of "charge after settlement" |
| `forgetting.py` | `pack.py` | half-life decay to down-weight stale segments |
| `timestamp.py` | `sessions.py` | in-session event time instead of file mtime |
| `crypto.py` / `frozen.py` | `journal.py` | local verifiable ledger, no signing gateway |
| `eventlog.py` | `journal.py` | hash chain kept, L1 replay dropped |
| `contracts.py` / `arena.py` / `gate.py` | none | multi-agent governance is out of scope |

## Known limits

- **No request proxying.** DSH has no public pre-request hook; intercepting means patching the kernel or MITM. The audit is offline, the rest are libraries.
- **Costs are estimates by default.** The price table lives in `pricing.py`; unknown models are left unpriced and listed in `unknown_models`. With `.dshw-usage.json` the audit reconciles against the real ledger, but the two count differently; a delta does not mean the bill is wrong.
- **The proxy rewrites requests.** Trimming only touches older tool results, but the history the model sees does change; verify with `--no-trim` first.
- **The cache matches one exact request.** Inputs must be byte-identical after normalization; no semantic matching. Do not cache side-effecting tool chains.
- **The hash chain catches tampering with one entry**, not a full rewrite by someone with write access.
- **The budget keeps one window.** Daily/monthly resets are the caller's job; no distributed exactly-once.
- **Audit reports are not published.** Real reports contain live session statistics and stay local; the repo only carries aggregate numbers.

## Development

```bash
python -m pytest
python -m ruff check .
```

```
src/agh_lite/
  tokens.py     token estimation (CJK / ASCII / other buckets)
  budget.py     budget leases, settlement, persistence
  cache.py      SQLite response cache
  pack.py       context packing and dedupe
  journal.py    hash-chain log
  sessions.py   DSH session parsing (multi-frame zstd)
  pricing.py    model price table and cost estimation
  usage.py      reconcile against DSH's real usage ledger
  proxy.py      local OpenAI-compatible proxy (trim + cache)
  audit.py      offline audit rules and report
  cli.py        command-line entry
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md).

## Author

Built by [Gray2006](https://github.com/Gray2006) with AI coding assistants.

## License

MIT
