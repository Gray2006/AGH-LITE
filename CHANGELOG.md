# Changelog

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
