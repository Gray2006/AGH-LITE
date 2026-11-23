# 设计说明

agh-lite 不是 AGH 的缩水版，而是换了一个问题：
AGH 问「多智能体怎么互相信任」，agh-lite 问「一个人怎么少烧 token」。

## 约束先定

1. **不改 DSH 内核**。个人用户装的是发行版，没有插件开发链；任何需要 DSH 配合的设计都不可用。
2. **零运行时依赖**。读 DSH 的 zstd 会话才需要 `zstandard`，其余只用标准库。
3. **只读**。审计不写 DSH 目录；缓存、预算账本、日志都放在 `~/.dsh/` 或用户指定位置。
4. **估算不冒充账单**。会话里有权威 `usage` 时用真实值；没有时明确标注估算。

## 模块

### `tokens.py`

按字符分桶估算：CJK 1.6 字/token、ASCII 3.6、其它 2.4。DeepSeek 系 tokenizer 下偏保守。
只用于「没有 usage 的文本」和「打包试算」，审计算总额时优先用真实 usage。

### `budget.py`

从 AGH `ledger.py` 抽出来的单窗口预算：

- `reserve(owner, max_tokens, reserved_usd, key, ttl_ms)` 占额度；
- `settle(lease, actual_tokens, actual_usd)` 按实际用量记账，超出预留记 `over_reserved` 但照实扣；
- `key` 让预留/结算幂等，重试不会重复扣；
- `reconcile()` 回收 TTL 过期租约，进程崩溃后不会永久占额度；
- `save()/load()` 把窗口落盘，重启后已结算的 key 仍然幂等。

没有做日/月窗口，因为个人场景下窗口边界由调用方最清楚；库里只维护一个窗口。

### `cache.py`

键 = `sha256(canonical_json({v, ns, model, messages, params}))`。
写入前剥掉 `id`、`created_at`、`timestamp`、`cache_control` 等不影响输出的字段，减少无谓未命中。
SQLite WAL + busy timeout，够单机多进程读写。

明确不做的事：不做语义缓存、不做向量检索。请求内容不同就是不同，宁可未命中，不可错命中。

### `pack.py`

打包 = 去重 + 预算内选择：

1. 精确去重：规范化空白与小写后逐字比较；
2. 近似去重：6-gram Jaccard ≥ 阈值（默认 0.85），只对 160 字以上的片段做，最多 600 个代表；
3. `system`、`pinned`、末尾 `protect_last` 条先占预算；
4. 其余按 `priority + kind 权重 + 时间新近度` 贪心装入，装不下就带原因丢弃。
   新近度用半衰期衰减（`0.3 * 0.5^age`，一个时间跨度减半），借的是 AGH `forgetting.py` 的衰减思路。

被丢的片段返回 `Dropped(id, reason, tokens)`，方便复核，不静默丢。

### `journal.py`

每条记录带前一条的哈希；`verify()` 检查 prev、序号、内容哈希。
它防「改一条看不出来」，不防「有写权限的人重写整条链」——后者需要外部锚点，个人版不值得。

### `sessions.py`

DSH 会话事件（实测）：

```
session, subagent/descriptor, request/header, request/context,
system/message, user/message, assistant/message,
tool/call, tool/result, step/start, step/end, turn/start, turn/end,
compaction/start, compaction/summary, compaction/end, llm/retry, ...
```

关键口径：

- `assistant/message.data.usage`：`totalTokens = inputTokens + cacheReadTokens + outputTokens`，`inputTokens` 不含缓存；
- `system/message` 每步写一条，取**最大**而不是累加；
- `request/header` 每步一条，工具 schema 取最大 JSON 长度；`config.model` 记到该步，用于按模型计价；
- `tool/result` 的 `message.content` 是 text block 列表，一次遍历同时得到字符数与估算 token；
- `compaction/start` 是 DSH 自动压缩，`tool/call.name == "compress"` 是手动压缩，两者都算「做过压缩」；
- `llm/retry` 只计数，不计费——失败请求可能没账单，但会拖长会话；
- `started_at/ended_at` 取事件 `time` 的最小/最大值；`--days` 先按文件 mtime 粗筛，再用会话内最后事件时间精筛。

zstd 读取：本机 zstandard 0.25 的 `stream_reader` 迭代会抛 `io.UnsupportedOperation`，所以用 `read()` 一次读完再按行切；`read_across_frames=True` 支持多帧拼接。19.7MB 的最大会话约 1 秒。

前缀指纹：`request/header` 的 config+tools 哈希、`system/message` 内容哈希。
同会话里指纹变了就计一次 `header_changes` / `system_changes`——这是判断「缓存为什么打不中」的证据，
比只看命中率有用。

### `audit.py`

规则与估算权重：

| 规则 | 触发 | 估算 |
|---|---|---|
| `duplicate_tool_calls` | 同会话内 `name + arguments` 完全一致 | 重复结果的 token |
| `oversized_tool_outputs` | 单条结果 > 8K tokens | 超出部分 × 60% |
| `low_prefix_cache` | prompt ≥ 50K 且命中 < 40% | 该会话 prompt × 10% |
| `unstable_prefix` | header/system 指纹反复变化且命中 < 70% | 相关 prompt × 15% |
| `static_prefix_bloat` | system + 工具 schema ≥ 12K | 静态前缀 × 步数 × 15% |
| `context_bloat` | 每步平均 prompt ≥ 120K，或 20 步以上从不压缩 | 相关会话 prompt × 10% |
| `heavy_reasoning` | output > 5K 且 reasoning 文本 > 正文 3 倍 | output × 20% |
| `provider_retries` | 单会话 `llm/retry` ≥ 3 | 0（无法确认计费） |
| `repeated_file_reads` | 同路径同区间 read ≥ 2 次 | 重复结果 token |

各项互相重叠，`potential_savings` 不相加，按总量 25% 封顶。
每项估算再按全局平均单价折成美元，报告里显示「约 $X」，避免只看 token 没感觉。

费用口径：`input × input价 + cacheRead × 缓存价 + output × output价`，单价按百万 tokens。
价格表在 `pricing.py`，未命中的模型不猜价，列进 `unknown_models`；`--pricing` 可覆盖。
报告按模型聚合 tokens 与费用，跨 provider 前缀归一（`deepseek/deepseek-v4.1-flash` 与
`deepseek-v4.1-flash` 视为同一模型）。

报告默认中文 Markdown；`--format json` 给脚本。

### `proxy.py`

为什么做：DSH 的插件扩展点只能改路由（`agent/request` 返回 `LlmCallConfig`），改不了完整 prompt；
内置压缩只在压力越线时触发。想在**每次请求**上少带一点，最省事的是让调用先经过本地代理：

```
DSH provider baseUrl -> http://127.0.0.1:8787/v1 -> 上游
```

只做两件事，可分别关闭：

1. 裁剪较早的 `role=tool` 长结果（头尾保留，中间折叠），最近 N 条不动；
2. 非流式请求按请求体哈希缓存，流式原样透传。

刻意不做：不改 system/user/assistant、不做语义缓存、不落盘 Authorization、不监听外网。
代价是模型看到的历史确实变了，所以默认只裁 tool 结果，并给 `--no-trim` 留退路。

### `usage.py`

读 `$DSH_HOME/.dshw-usage.json` 的 `events`：每条带 `ts / day / model / cost / tokens`。
只做聚合（按天、按模型、总量），不做二次换算；坏事件跳过，字段缺失不猜。
审计把估算费用和它对账，差额单独列出——两边计费口径不同，差额不等于账错。

## 为什么不是内核补丁

最省 token 的做法是「请求前砍上下文、请求后记缓存」。DSH 的插件扩展点能改路由，但改不了完整 prompt；
硬改只能动内核。所以 agh-lite 的默认路径仍是审计与库，代理是**显式选择**：

- 用户把 provider 的 baseUrl 指到本地，随时可以改回去；
- 代理只监听 127.0.0.1，不是 MITM，不碰 TLS；
- 不改 DSH 安装，不依赖内部 API 版本。

这个取舍写在这里，避免以后有人以为它是 DSH 的插件。

## 后续可做

- 前缀稳定性检测：比较相邻 `request/header` 的哈希，判断工具集是否频繁变化；
- 按 workspace 维度拆分报告（模型维度已做）。
