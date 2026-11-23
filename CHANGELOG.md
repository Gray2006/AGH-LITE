# Changelog

## 0.9.0 (2026-11-24)

### 新增

- `audit --format html`：自包含 HTML 报告（内联样式、无脚本、无外部资源），含总览、建议卡片、对账、workspace、会话排行；`--redact` 同样生效。

## 0.8.0 (2026-11-24)

### 新增

- 代理节省账本：每次请求把裁剪字符、缓存命中、usage 写进 `~/.dsh/agh-lite-proxy.jsonl`。
- `agh-lite proxy --stats [--json]`：汇总请求数、缓存命中、裁剪量、省下的 tokens 与折合费用。

## 0.7.0 (2026-11-24)

### 新增

- 费用校准：有真实账本时，报告给出 `calibration_factor`（实际 / 估算），差额超过 30% 时把「可省」换算成真实金额；JSON 同步输出 `calibration_factor` 与 `calibrated_savings_usd`。

### 实测

- 本机 2041 笔账本：估算 $248、实际 $645，系数约 2.6，校准后可省金额同步放大。

## 0.6.0 (2026-11-24)

### 新增

- `volatile_system_prompt` 规则：system 指纹先归一化时间戳、UUID、epoch，再判断变化；只变「现在几点」的会话单独归类，不再和真的规则改写混在一起。
- `SessionRecord.system_volatile_changes`：原始文本变了但语义没变的次数。

### 实测

- 真实数据里 11 个会话命中 `unstable_prefix`（327M prompt tokens），其中 2 个是纯时间戳注入（37M），缓存命中 1% / 15%。

## 0.5.1 (2026-11-24)

### 修复

- 「按 workspace」在真实会话上全落到 `unknown`：`session` 事件不带 `cwd` 时，从 `sessions/<workspace>/<session>/` 目录名兜底，并解码 DSH 的 `~XXXX` 转义。

## 0.5.0 (2026-11-24)

### 新增

- 前缀稳定性检测：记录每个会话的 `header_changes` / `system_changes`（工具集、system prompt 指纹变化），审计新增 `unstable_prefix` 规则——回答「缓存为什么打不中」。
- 报告新增「按 workspace」：按会话 `cwd` 聚合 token 与费用，`--format json` 同步输出 `workspaces`。

## 0.4.0 (2026-11-24)

### 新增

- `agh-lite proxy`：本地 OpenAI 兼容代理，自动裁剪较早的长工具结果、缓存非流式重复请求；只监听 127.0.0.1，不改 DSH 安装。`--no-trim` / `--no-cache` 可分别关闭，`/agh-lite/health` 看命中与裁剪计数。
- `cache` 连接允许跨线程使用（代理内部加锁），`cache stats` 的 saved_usd 同样适用。

### 说明

- 代理是显式选择：把 DSH provider 的 baseUrl 指到本地端口，删掉 provider 即恢复；不是 MITM。

## 0.3.0 (2026-11-24)

### 新增

- `usage`：读 DSH 的 `.dshw-usage.json`，按天/模型聚合真实花费。
- `audit --usage PATH` / `--no-usage`：报告新增「与真实账单对账」，给出实际花费、估算花费、差额，以及按天/按模型的真实费用；默认自动发现账本。
- `cache stats` 增加折合费用与未定价模型，缓存省下的 tokens 能直接换成钱。
- CI（ruff + pytest，Python 3.12/3.13）与版本号一致性测试。

### 修复

- `audit --days N --limit M` 的顺序：现在先按事件时间筛选，再截断，`--limit` 不会被旧会话占满。
- `BudgetExceeded` 美元不足时不再谎报 tokens，异常带 `unit` 字段。

## 0.2.0 (2026-11-23)

从 AGH v0.10.0 回收四样东西：计费口径、遗忘曲线、时间口径、可持久账本。

### 新增

- `pricing`：模型价格表 + 费用估算。`audit` 报告现在给出估算总花费、按模型的 tokens/费用拆分，以及每条建议折合多少钱；未命中的模型不计价。
- `audit --pricing FILE`：用自己的价格表覆盖内置价格。
- `TokenBudget.save/load`：预算窗口落盘与恢复，重启后幂等键仍有效。
- `SessionRecord.started_at/ended_at`、`StepUsage.model`：按会话内事件时间统计，`--days` 不再依赖文件 mtime。

### 变更

- `pack` 的时间权重由线性改为半衰期衰减（借 AGH `forgetting.py` 的思路），最新片段 0.3，一个时间跨度前减半。
- 审计的「按模型」表：同一模型跨 provider 前缀归一，例如 `deepseek/deepseek-v4.1-flash`。
- 内置价格表补了 `deepseek-flash` 与免费的 `glm-4-flash`。
- 审计报告不再进版本库：真实报告含本机会话统计，留在本地。

### 实测

- 178 个会话、4.60B tokens，估算费用约 $245，可省约 $36.55。
- 未知模型不再猜价，报告里单独列出。

## 0.1.0 (2026-11-23)

首个版本。

### 新增

- `audit`：离线审计 DSH 会话，输出中文 Markdown 或 JSON，规则包括重复调用、超大工具结果、低前缀缓存、静态前缀过重、上下文膨胀、推理占比、供应商重试、重复读文件。
- `pack`：预算内上下文打包，精确 + 近似去重，受保护片段优先，丢弃带原因。
- `cache`：SQLite 内容寻址响应缓存，TTL、命中统计、按年龄清理。
- `budget`：预授权 / 结算 / 超预留违规 / TTL 回收，支持 token 与美元双限额。
- `journal`：哈希链日志，支持追加、列表、校验。
- `tokens`：文本和会话的 token 估算与画像。
- `sessions`：DSH 会话解析，支持明文 `.jsonl` 与多帧 `.jsonl.zstd`。

### 实测

- 178 个会话、30139 步、4.48B tokens，全量审计 13.6 秒。
- 真实 DSH 事件里 `system/message` 每步一条、`llm/retry` 大量存在，解析按实测口径处理。
