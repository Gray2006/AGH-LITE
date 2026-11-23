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

# custom price table
agh-lite audit --pricing my-prices.json

# redact absolute paths before sharing
agh-lite audit --redact --out runs/audit.md
```

What the report contains:

| Section | Contents |
|---|---|
| Overview | total tokens, prompt / output, cache hit rate, tool schema, estimated cost |
| Top findings | evidence, recommendation, estimated tokens / money saved per issue |
| By model | token share and estimated cost per model |
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
- the same model behind different provider prefixes is normalized: `deepseek/deepseek-v4.1-flash` and `deepseek-v4.1-flash` are one model.

Audit rules:

| Rule | Trigger | Estimate |
|---|---|---|
| `duplicate_tool_calls` | identical `name + arguments` within a session | duplicate result tokens |
| `oversized_tool_outputs` | one result > 8K tokens | excess × 60% |
| `low_prefix_cache` | prompt ≥ 50K and hit < 40% | session prompt × 10% |
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

Top three findings:

1. **Context bloat**: 48 sessions average over 120K prompt per step, peak 282K; ~381M tokens recoverable.
2. **Static prefix bloat**: ~59K of system + tool schema on every request; ~273M tokens.
3. **Low prefix cache**: 13 sessions below 40% hit rate; ~28M tokens.

The counter-intuitive conclusion: **tool results are 0.2% of the prompt; the real cost is history re-read every step and the 52K tool schema.**

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
- **Costs are estimates, not invoices.** The price table lives in `pricing.py`; unknown models are left unpriced and listed in `unknown_models`.
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
  audit.py      offline audit rules and report
  cli.py        command-line entry
```

Design notes: [docs/DESIGN.md](docs/DESIGN.md).

## Author

Built by [Gray2006](https://github.com/Gray2006) with AI coding assistants.

## License

MIT
