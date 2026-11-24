"""离线审计：把 DSH 会话里的 token 花销拆开看。

只读、只统计、不调用模型。目标不是给一张漂亮报表，而是回答三个问题：
1. 钱花在 prompt 还是 output？
2. 哪些内容是重复的、超长的、可以提前砍掉的？
3. 下一次会话具体改哪一件事，能省多少（估算）。

估算口径写在报告末尾；不要把它当账单。
"""

from __future__ import annotations

import html
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pricing import DEFAULT_PRICES, ModelPrice, format_usd, load_prices, lookup_price
from .sessions import (
    SessionReadError,
    SessionRecord,
    find_session_files,
    load_session,
    resolve_dsh_home,
)
from .tokens import format_tokens
from .usage import UsageSummary, find_usage_file, load_usage, summarize

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


def _short(session_id: str, width: int = 8) -> str:
    """DSH 目录名形如 session-<uuid>，显示时去掉前缀，避免满屏都是 session-。"""
    return session_id.removeprefix("session-")[:width]

# 单条工具结果超过这个量，基本可以判断「整篇倒进来了」
OVERSIZED_RESULT_TOKENS = 8_000
# 工具 schema + system prompt 的静态前缀超过这个量，值得看工具集
STATIC_PREFIX_TOKENS = 12_000
# 会话 prompt 超过这个量才开始谈前缀缓存
CACHE_PROMPT_FLOOR = 50_000


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    recommendation: str
    estimated_tokens: int = 0
    estimated_usd: float | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "recommendation": self.recommendation,
            "estimated_tokens": self.estimated_tokens,
            "estimated_usd": self.estimated_usd,
            "evidence": self.evidence,
        }


@dataclass
class Totals:
    sessions: int = 0
    turns: int = 0
    steps: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    tool_result_tokens: int = 0
    tool_result_chars: int = 0
    tool_schema_tokens: int = 0
    system_tokens: int = 0
    error_results: int = 0
    compression_calls: int = 0
    compactions: int = 0
    llm_retries: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    priced_tokens: int = 0
    unknown_models: list[str] = field(default_factory=list)
    model_usage: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def has_cost(self) -> bool:
        return self.priced_tokens > 0

    @property
    def blended_usd_per_m(self) -> float:
        """已定价部分的平均每百万 tokens 单价，用于把 token 估算换成钱。"""
        return self.cost_usd / self.priced_tokens * 1_000_000 if self.priced_tokens else 0.0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens

    @property
    def cache_hit_ratio(self) -> float:
        return self.cache_read_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def avg_prompt_tokens(self) -> int:
        return self.prompt_tokens // self.steps if self.steps else 0

    @property
    def tool_share(self) -> float:
        """工具结果相对 prompt 的占比；不是占总量——总量含缓存重读。"""
        return self.tool_result_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def output_share(self) -> float:
        return self.output_tokens / self.total_tokens if self.total_tokens else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "turns": self.turns,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_hit_ratio": round(self.cache_hit_ratio, 4),
            "output_tokens": self.output_tokens,
            "tool_result_tokens": self.tool_result_tokens,
            "tool_result_chars": self.tool_result_chars,
            "tool_schema_tokens": self.tool_schema_tokens,
            "system_tokens": self.system_tokens,
            "tool_share": round(self.tool_share, 4),
            "output_share": round(self.output_share, 4),
            "error_results": self.error_results,
            "compression_calls": self.compression_calls,
            "compactions": self.compactions,
            "llm_retries": self.llm_retries,
            "duration_ms": self.duration_ms,
            "cost_usd": round(self.cost_usd, 4),
            "priced_tokens": self.priced_tokens,
            "unknown_models": self.unknown_models,
            "model_usage": self.model_usage,
        }


@dataclass
class SessionSummary:
    session_id: str
    path: str
    cwd: str
    model: str
    turns: int
    steps: int
    total_tokens: int
    prompt_tokens: int
    cache_hit_ratio: float
    tool_result_tokens: int
    output_tokens: int
    duplicate_tokens: int
    oversized_tokens: int
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "cwd": self.cwd,
            "model": self.model,
            "turns": self.turns,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "cache_hit_ratio": round(self.cache_hit_ratio, 4),
            "tool_result_tokens": self.tool_result_tokens,
            "output_tokens": self.output_tokens,
            "duplicate_tokens": self.duplicate_tokens,
            "oversized_tokens": self.oversized_tokens,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 4),
        }


@dataclass
class AuditReport:
    home: Path
    generated_at: int
    totals: Totals
    findings: list[Finding]
    sessions: list[SessionSummary]
    skipped: list[str] = field(default_factory=list)
    days: int | None = None
    usage: UsageSummary | None = None

    @property
    def estimate_delta_usd(self) -> float | None:
        if self.usage is None or not self.totals.has_cost:
            return None
        return self.totals.cost_usd - self.usage.total_cost

    @property
    def calibration_factor(self) -> float | None:
        """真实账单 / 估算费用。价格表偏低时用它把「可省」换算成真钱。"""
        if self.usage is None or not self.totals.has_cost or self.totals.cost_usd <= 0:
            return None
        return self.usage.total_cost / self.totals.cost_usd

    @property
    def calibrated_savings_usd(self) -> float | None:
        factor = self.calibration_factor
        savings = self.potential_savings_usd
        if factor is None or savings is None:
            return None
        return savings * factor

    @property
    def potential_savings(self) -> int:
        """各项估算上限的保守合并——不相加，避免把重叠收益算成钱。"""
        gross = sum(finding.estimated_tokens for finding in self.findings)
        cap = int(self.totals.total_tokens * 0.25)
        return min(gross, cap)

    @property
    def potential_savings_usd(self) -> float | None:
        if not self.totals.has_cost:
            return None
        return self.potential_savings / 1_000_000 * self.totals.blended_usd_per_m

    def top_sessions(self, limit: int = 10) -> list[SessionSummary]:
        return sorted(self.sessions, key=lambda item: item.total_tokens, reverse=True)[:limit]

    def workspace_rows(self, limit: int = 10) -> list[dict[str, Any]]:
        """按 cwd 聚合，回答「哪个项目最烧 token」。"""
        grouped: dict[str, dict[str, Any]] = {}
        for summary in self.sessions:
            key = summary.cwd or "unknown"
            row = grouped.setdefault(
                key,
                {"workspace": key, "sessions": 0, "tokens": 0, "cost_usd": 0.0, "priced": False},
            )
            row["sessions"] += 1
            row["tokens"] += summary.total_tokens
            if summary.cost_usd is not None:
                row["cost_usd"] += summary.cost_usd
                row["priced"] = True
        rows = sorted(grouped.values(), key=lambda item: item["tokens"], reverse=True)
        for row in rows:
            if not row.pop("priced"):
                row["cost_usd"] = None
            else:
                row["cost_usd"] = round(row["cost_usd"], 4)
        return rows[:limit]

    def to_dict(self, *, top: int = 10) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "home": str(self.home),
            "days": self.days,
            "totals": self.totals.to_dict(),
            "potential_savings": self.potential_savings,
            "potential_savings_usd": (
                None if self.potential_savings_usd is None else round(self.potential_savings_usd, 4)
            ),
            "usage": None if self.usage is None else self.usage.to_dict(),
            "estimate_delta_usd": (
                None if self.estimate_delta_usd is None else round(self.estimate_delta_usd, 4)
            ),
            "calibration_factor": (
                None if self.calibration_factor is None else round(self.calibration_factor, 4)
            ),
            "calibrated_savings_usd": (
                None if self.calibrated_savings_usd is None else round(self.calibrated_savings_usd, 4)
            ),
            "findings": [finding.to_dict() for finding in self.findings],
            "top_sessions": [summary.to_dict() for summary in self.top_sessions(top)],
            "workspaces": self.workspace_rows(top),
            "skipped": self.skipped,
        }

    # ------------------------------------------------------------ 渲染
    def render_markdown(self, *, top: int = 10) -> str:
        totals = self.totals
        generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.generated_at / 1000))
        lines: list[str] = [
            "# DSH Token 审计报告",
            "",
            f"- 生成时间：{generated}",
            f"- 数据目录：`{self.home}`",
            f"- 窗口：{'最近 ' + str(self.days) + ' 天' if self.days else '全部可用会话'}",
            f"- 会话：{totals.sessions} 个，{totals.turns} 轮，{totals.steps} 步",
            f"- 总量：**{format_tokens(totals.total_tokens)} tokens**"
            f"（prompt {format_tokens(totals.prompt_tokens)} / output {format_tokens(totals.output_tokens)}）",
            f"- 前缀缓存命中：{totals.cache_hit_ratio:.0%}",
            f"- 平均每步 prompt：{format_tokens(totals.avg_prompt_tokens)}",
            f"- 工具结果累计：{format_tokens(totals.tool_result_tokens)}",
            f"- 估算费用：{format_usd(totals.cost_usd) if totals.has_cost else '未配置价格'}",
            "",
            "## 总览",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 总 tokens | {totals.total_tokens:,} |",
            f"| prompt tokens | {totals.prompt_tokens:,} |",
            f"| 其中缓存命中 | {totals.cache_read_tokens:,}（{totals.cache_hit_ratio:.0%}） |",
            f"| output tokens | {totals.output_tokens:,} |",
            f"| 平均每步 prompt | {totals.avg_prompt_tokens:,} |",
            f"| 工具结果 tokens | {totals.tool_result_tokens:,}（相当于 prompt 的 {totals.tool_share:.1%}） |",
            f"| 工具 schema（单次请求） | {totals.tool_schema_tokens:,} |",
            f"| system prompt（单次请求） | {totals.system_tokens:,} |",
            f"| 压缩：手动 {totals.compression_calls} / 自动 {totals.compactions} |",
            f"| 供应商重试 | {totals.llm_retries} 次 |",
            f"| 工具报错 | {totals.error_results} 次 |",
            f"| 估算费用 | {format_usd(totals.cost_usd) if totals.has_cost else '未配置价格'} |",
            "",
        ]

        if totals.has_cost and self.potential_savings_usd is not None:
            lines.append(
                f"**估算可省：约 {format_tokens(self.potential_savings)} tokens"
                f"（约 {format_usd(self.potential_savings_usd)}，"
                f"占总量 {self.potential_savings / totals.total_tokens:.0%}）**"
                if totals.total_tokens
                else "**估算可省：无数据**"
            )
        else:
            lines.append(
                f"**估算可省：约 {format_tokens(self.potential_savings)} tokens"
                f"（占总量 {self.potential_savings / totals.total_tokens:.0%}）**"
                if totals.total_tokens
                else "**估算可省：无数据**"
            )
        lines.append("")

        if self.findings:
            lines += ["## 最该先动的几件事", ""]
            for index, finding in enumerate(self.findings[:top], start=1):
                price = (
                    f"，约 {format_usd(finding.estimated_usd)}"
                    if finding.estimated_usd is not None
                    else ""
                )
                lines += [
                    f"### {index}. [{SEVERITY_LABEL.get(finding.severity, finding.severity)}] "
                    f"{finding.title}（约 {format_tokens(finding.estimated_tokens)} tokens{price}）",
                    "",
                    finding.detail,
                    "",
                ]
                if finding.evidence:
                    lines.append("证据：")
                    lines += [f"- {item}" for item in finding.evidence]
                    lines.append("")
                lines += [f"怎么做：{finding.recommendation}", ""]
        else:
            lines += ["## 结论", "", "没有发现明显的结构性浪费。保持当前用法。", ""]

        if totals.model_usage:
            lines += [
                "## 按模型",
                "",
                "| 模型 | tokens | 占比 | 估算费用 |",
                "|---|---|---|---|",
            ]
            for model, row in sorted(
                totals.model_usage.items(), key=lambda item: item[1]["total"], reverse=True
            ):
                share = row["total"] / totals.total_tokens if totals.total_tokens else 0.0
                cost = format_usd(row["cost"]) if row["cost"] is not None else "未配置"
                lines.append(
                    f"| {model or 'unknown'} | {format_tokens(int(row['total']))} | {share:.0%} | {cost} |"
                )
            lines.append("")

        if self.usage is not None and self.usage.events:
            delta = self.estimate_delta_usd
            lines += [
                "## 与真实账单对账",
                "",
                f"- 实际花费：{format_usd(self.usage.total_cost)}（{self.usage.events} 笔）",
                f"- 估算花费：{format_usd(totals.cost_usd) if totals.has_cost else '未配置价格'}",
                f"- 差额：{format_usd(delta) if delta is not None else '无法比较'}",
                "",
            ]
            if delta is not None and abs(delta) > self.usage.total_cost * 0.3:
                lines += [
                    "> 估算与实际差超过 30%：先检查价格表是否覆盖了实际通道。"
                    "中转站常在官方价上加价，用 `--pricing` 换成自己的单价再比。",
                    "",
                ]
            factor = self.calibration_factor
            calibrated = self.calibrated_savings_usd
            if factor is not None and calibrated is not None and abs(factor - 1) > 0.1:
                lines += [
                    f"> 按真实账单校准：估算 × {factor:.2f}，校准后可省约 {format_usd(calibrated)}。",
                    "",
                ]
            lines += [
                "| 日期 | 实际费用 |",
                "|---|---|",
            ]
            for day, cost in sorted(self.usage.by_day.items(), reverse=True)[:14]:
                lines.append(f"| {day} | {format_usd(cost)} |")
            lines.append("")
            if self.usage.by_model:
                lines += [
                    "| 模型 | 实际费用 | 实际 tokens |",
                    "|---|---|---|",
                ]
                for model, row in sorted(
                    self.usage.by_model.items(), key=lambda item: item[1]["cost"], reverse=True
                ):
                    lines.append(
                        f"| {model} | {format_usd(row['cost'])} | {format_tokens(int(row['tokens']))} |"
                    )
                lines.append("")

        workspaces = self.workspace_rows(top)
        if workspaces:
            lines += [
                "## 按 workspace",
                "",
                "| workspace | 会话 | 总 tokens | 估算费用 |",
                "|---|---|---|---|",
            ]
            for row in workspaces:
                cost = format_usd(row["cost_usd"]) if row["cost_usd"] is not None else "未配置"
                lines.append(
                    f"| `{row['workspace']}` | {row['sessions']} | "
                    f"{format_tokens(row['tokens'])} | {cost} |"
                )
            lines.append("")

        sessions = self.top_sessions(top)
        if sessions:
            lines += [
                "## 会话排行",
                "",
                "| 会话 | 模型 | 轮/步 | 总 tokens | 缓存命中 | 工具结果 | 重复 |",
                "|---|---|---|---|---|---|---|",
            ]
            for summary in sessions:
                lines.append(
                    f"| `{_short(summary.session_id, 12)}` | {summary.model or '-'} | "
                    f"{summary.turns}/{summary.steps} | {format_tokens(summary.total_tokens)} | "
                    f"{summary.cache_hit_ratio:.0%} | {format_tokens(summary.tool_result_tokens)} | "
                    f"{format_tokens(summary.duplicate_tokens)} |"
                )
            lines.append("")

        lines += [
            "## 口径与边界",
            "",
            "- 统计来自 DSH 会话记录里的 `usage`：prompt = inputTokens + cacheReadTokens，output 为模型输出。",
            "- 没有 usage 的旧会话不计入 token 总量，但工具调用仍会参与重复/超长分析。",
            "- 「估算可省」是上限估计，各项互相重叠，不按单项相加，总量按 25% 封顶。",
            "- 费用按内置价格表估算，未命中的模型不计价；用 `--pricing` 传自己的价格表可以覆盖。",
            "- 有 `.dshw-usage.json` 时报告会附真实账单对账；差额来自估算口径与未定价模型，不代表账错了。",
            "- 本工具只读，不上传任何会话内容；`skipped` 里是读取失败或超限跳过的文件。",
        ]
        if self.skipped:
            lines += ["", "跳过："]
            lines += [f"- {item}" for item in self.skipped[:10]]
        return "\n".join(lines)

    # ------------------------------------------------------------ HTML
    def render_html(self, *, top: int = 10) -> str:
        """自包含 HTML：无外部资源，方便直接发给别人或存档。"""
        esc = html.escape
        totals = self.totals
        generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.generated_at / 1000))
        rows: list[str] = []

        def table(headers: list[str], body: list[list[str]]) -> str:
            head = "".join(f"<th>{esc(item)}</th>" for item in headers)
            cells = "".join(
                "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in body
            )
            return f"<table><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table>"

        rows.append(
            table(
                ["指标", "数值"],
                [
                    ["会话 / 轮 / 步", f"{totals.sessions} / {totals.turns} / {totals.steps}"],
                    ["总 tokens", f"{totals.total_tokens:,}"],
                    ["prompt tokens", f"{totals.prompt_tokens:,}"],
                    ["缓存命中", f"{totals.cache_hit_ratio:.0%}"],
                    ["output tokens", f"{totals.output_tokens:,}"],
                    ["工具结果 tokens", f"{totals.tool_result_tokens:,}（{totals.tool_share:.0%}）"],
                    ["估算费用", format_usd(totals.cost_usd) if totals.has_cost else "未配置价格"],
                    ["估算可省", format_tokens(self.potential_savings)],
                ],
            )
        )

        if self.usage is not None and self.usage.tokens_by_day:
            days = sorted(self.usage.tokens_by_day.items())[-14:]
            max_value = max(value for _, value in days) or 1
            width, height, pad = 720, 200, 32
            bar_width = (width - 2 * pad) / len(days)
            bars: list[str] = []
            for index, (day, value) in enumerate(days):
                bar_height = (height - 2 * pad) * value / max_value
                x = pad + index * bar_width
                y = height - pad - bar_height
                bars.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width - 4:.1f}" '
                    f'height="{max(bar_height, 1):.1f}" fill="#1f6feb">'
                    f"<title>{esc(day)}: {value:,} tokens</title></rect>"
                )
                if index % 3 == 0 or index == len(days) - 1:
                    bars.append(
                        f'<text x="{x + bar_width / 2:.1f}" y="{height - pad + 14}" '
                        f'font-size="9" text-anchor="middle" fill="#57606a">{esc(day[5:])}</text>'
                    )
            rows.append(
                "<h2>按天用量</h2>"
                f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
                f'aria-label="按天 tokens 用量">{"".join(bars)}</svg>'
            )

        if self.usage is not None and self.usage.by_day:
            days = sorted(self.usage.by_day.items())[-14:]
            max_cost = max(cost for _, cost in days) or 1.0
            width, height, pad = 720, 120, 24
            step = (width - 2 * pad) / max(1, len(days) - 1)
            points: list[tuple[float, float, str, float]] = []
            for index, (day, cost) in enumerate(days):
                x = pad + index * step
                y = height - pad - (height - 2 * pad) * cost / max_cost
                points.append((x, y, day, cost))
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in points)
            dots = "".join(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="#cf222e">'
                f"<title>{esc(day)}: {format_usd(cost)}</title></circle>"
                for x, y, day, cost in points
            )
            rows.append(
                "<h3>按天费用</h3>"
                f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
                f'aria-label="按天费用">'
                f'<polyline points="{polyline}" fill="none" stroke="#cf222e" stroke-width="2"/>'
                f"{dots}</svg>"
            )

        if self.findings:
            cards = []
            for finding in self.findings[:top]:
                evidence = "".join(f"<li>{esc(item)}</li>" for item in finding.evidence)
                cards.append(
                    f'<div class="finding {esc(finding.severity)}">'
                    f'<div class="badge">{esc(SEVERITY_LABEL.get(finding.severity, finding.severity))}</div>'
                    f"<h3>{esc(finding.title)}</h3>"
                    f"<p>{esc(finding.detail)}</p>"
                    f"<p class='rec'>怎么做：{esc(finding.recommendation)}</p>"
                    f"<ul>{evidence}</ul></div>"
                )
            rows.append("<h2>最该先动的几件事</h2>" + "".join(cards))

        if self.usage is not None and self.usage.events:
            delta = self.estimate_delta_usd
            rows.append(
                "<h2>与真实账单对账</h2>"
                + table(
                    ["项目", "金额"],
                    [
                        ["实际花费", format_usd(self.usage.total_cost)],
                        ["估算花费", format_usd(totals.cost_usd) if totals.has_cost else "未配置价格"],
                        ["差额", format_usd(delta) if delta is not None else "无法比较"],
                    ],
                )
            )

        workspaces = self.workspace_rows(top)
        if workspaces:
            rows.append(
                "<h2>按 workspace</h2>"
                + table(
                    ["workspace", "会话", "总 tokens", "估算费用"],
                    [
                        [
                            row["workspace"],
                            str(row["sessions"]),
                            format_tokens(row["tokens"]),
                            format_usd(row["cost_usd"]) if row["cost_usd"] is not None else "未配置",
                        ]
                        for row in workspaces
                    ],
                )
            )

        sessions = self.top_sessions(top)
        if sessions:
            rows.append(
                "<h2>会话排行</h2>"
                + table(
                    ["会话", "模型", "轮/步", "总 tokens", "缓存命中", "重复"],
                    [
                        [
                            _short(summary.session_id, 12),
                            summary.model or "-",
                            f"{summary.turns}/{summary.steps}",
                            format_tokens(summary.total_tokens),
                            f"{summary.cache_hit_ratio:.0%}",
                            format_tokens(summary.duplicate_tokens),
                        ]
                        for summary in sessions
                    ],
                )
            )

        style = """
:root{--bg:#f6f8fa;--card:#fff;--text:#1f2328;--muted:#57606a;--border:#d0d7de;
--soft:#eaeef2;--accent:#1f6feb;--high:#cf222e;--medium:#bf8700;--low:#1a7f37}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--card:#161b22;--text:#e6edf3;
--muted:#8b949e;--border:#30363d;--soft:#21262d;--accent:#4493f8;
--high:#f85149;--medium:#d29922;--low:#3fb950}}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:960px;
margin:2rem auto;padding:0 1.2rem;color:var(--text);background:var(--bg);line-height:1.6;
font-variant-numeric:tabular-nums}
h1{font-size:1.6rem;letter-spacing:.2px}
h2{margin-top:2rem;border-bottom:1px solid var(--border);padding-bottom:.35rem;font-size:1.15rem}
h3{margin-top:1.2rem;font-size:.98rem;color:var(--muted)}
code{background:var(--soft);padding:.1rem .35rem;border-radius:5px}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:1.2rem 0}
.hero div{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.8rem .9rem}
.hero span{display:block;color:var(--muted);font-size:.8rem}
.hero b{font-size:1.35rem;font-weight:650}
table{border-collapse:collapse;width:100%;margin:.8rem 0;background:var(--card)}
th,td{border:1px solid var(--border);padding:.42rem .6rem;text-align:left;font-size:.9rem}
th{background:var(--soft);font-weight:600}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--soft) 55%,transparent)}
.finding{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--muted);
border-radius:8px;padding:.85rem 1rem;margin:.8rem 0}
.finding.high{border-left-color:var(--high)}.finding.medium{border-left-color:var(--medium)}
.finding.low{border-left-color:var(--low)}
.badge{display:inline-block;font-size:.75rem;padding:.1rem .5rem;border-radius:999px;
background:var(--soft);color:var(--muted);margin-bottom:.3rem}
.rec{color:var(--muted)}ul{margin:.4rem 0}
footer{margin-top:2.5rem;color:var(--muted);font-size:.85rem;border-top:1px solid var(--border);padding-top:1rem}
@media print{body{max-width:none;margin:0;background:#fff;color:#000}
.hero div,.finding,table{box-shadow:none}th{background:#eee}
a{color:var(--accent)}}
"""
        hero = (
            '<div class="hero">'
            f'<div><span>总 tokens</span><b>{format_tokens(totals.total_tokens)}</b></div>'
            f'<div><span>会话</span><b>{totals.sessions}</b></div>'
            f'<div><span>估算费用</span><b>'
            f'{format_usd(totals.cost_usd) if totals.has_cost else "未配置"}</b></div>'
            f'<div><span>估算可省</span><b>{format_tokens(self.potential_savings)}</b></div>'
            "</div>"
        )
        meta = (
            f"<p>生成时间 {esc(generated)} · 数据目录 <code>{esc(str(self.home))}</code> · "
            f"{'最近 ' + str(self.days) + ' 天' if self.days else '全部可用会话'}</p>"
        )
        return (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>DSH Token 审计报告</title><style>{style}</style></head><body>"
            f"<h1>DSH Token 审计报告</h1>{meta}{hero}{''.join(rows)}"
            "<footer>估算口径见 README；本报告只读生成，不含会话正文。</footer>"
            "</body></html>"
        )


def _session_cost(
    record: SessionRecord, prices: dict[str, ModelPrice]
) -> tuple[float | None, int]:
    cost = 0.0
    priced = 0
    for model, (input_tokens, cache_read, output, total) in record.model_usage().items():
        price = lookup_price(model, prices)
        if price is None:
            continue
        cost += price.cost(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            output_tokens=output,
        )
        priced += total
    return (cost if priced else None), priced


def _summarize(record: SessionRecord, prices: dict[str, ModelPrice]) -> SessionSummary:
    duplicate_tokens = sum(item[2] for item in record.duplicate_calls())
    oversized = sum(
        max(0, result.tokens - OVERSIZED_RESULT_TOKENS) for result in record.tool_results.values()
    )
    cost, _ = _session_cost(record, prices)
    return SessionSummary(
        session_id=record.session_id,
        path=str(record.path),
        cwd=record.cwd,
        model=record.model,
        turns=record.turns,
        steps=len(record.steps),
        total_tokens=record.total_tokens,
        prompt_tokens=record.prompt_tokens,
        cache_hit_ratio=record.cache_hit_ratio,
        tool_result_tokens=record.tool_result_tokens,
        output_tokens=record.output_tokens,
        duplicate_tokens=duplicate_tokens,
        oversized_tokens=oversized,
        cost_usd=cost,
    )


def _aggregate_totals(records: list[SessionRecord], prices: dict[str, ModelPrice]) -> Totals:
    totals = Totals(sessions=len(records))
    seen_tools: Counter[str] = Counter()
    unknown: set[str] = set()
    models: dict[str, dict[str, float]] = {}
    for record in records:
        totals.turns += record.turns
        totals.steps += len(record.steps)
        totals.total_tokens += record.total_tokens
        totals.input_tokens += sum(step.input_tokens for step in record.steps)
        totals.cache_read_tokens += record.cache_read_tokens
        totals.output_tokens += record.output_tokens
        totals.tool_result_tokens += record.tool_result_tokens
        totals.tool_result_chars += record.tool_result_chars
        totals.tool_schema_tokens = max(totals.tool_schema_tokens, record.tool_schema_tokens)
        totals.system_tokens = max(totals.system_tokens, record.system_tokens)
        totals.error_results += record.error_results
        totals.compression_calls += record.compression_calls
        totals.compactions += record.compactions
        totals.llm_retries += record.llm_retries
        totals.duration_ms += record.duration_ms
        seen_tools.update(record.tool_names)
        for model, (input_tokens, cache_read, output, total) in record.model_usage().items():
            row = models.setdefault(
                model,
                {"input": 0.0, "cache_read": 0.0, "output": 0.0, "total": 0.0, "cost": 0.0},
            )
            row["input"] += input_tokens
            row["cache_read"] += cache_read
            row["output"] += output
            row["total"] += total
            price = lookup_price(model, prices)
            if price is None:
                unknown.add(model)
                row["cost"] = None  # type: ignore[assignment]
            elif row["cost"] is not None:
                row["cost"] += price.cost(
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read,
                    output_tokens=output,
                )
                totals.cost_usd += price.cost(
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read,
                    output_tokens=output,
                )
                totals.priced_tokens += total
    totals.tool_schema_tokens = max(totals.tool_schema_tokens, 0)
    totals.unknown_models = sorted(unknown)
    totals.model_usage = models
    return totals


def audit_records(
    records: list[SessionRecord],
    *,
    home: Path,
    generated_at: int | None = None,
    days: int | None = None,
    skipped: list[str] | None = None,
    prices: dict[str, ModelPrice] | None = None,
    usage: UsageSummary | None = None,
) -> AuditReport:
    prices = prices or dict(DEFAULT_PRICES)
    totals = _aggregate_totals(records, prices)
    summaries = [_summarize(record, prices) for record in records]
    findings: list[Finding] = []

    # 1. 重复调用：同一会话内 name+arguments 完全一样
    duplicate_total = sum(summary.duplicate_tokens for summary in summaries)
    if duplicate_total > 0:
        top_dupes = []
        for record in records:
            for args_key, count, tokens in record.duplicate_calls()[:3]:
                if tokens > 0:
                    top_dupes.append((tokens, record.session_id, args_key, count))
        top_dupes.sort(reverse=True)
        findings.append(
            Finding(
                code="duplicate_tool_calls",
                severity="high" if duplicate_total > totals.tool_result_tokens * 0.2 else "medium",
                title="同一工具调用被重复执行",
                detail=(
                    f"有 {format_tokens(duplicate_total)} tokens 来自重复的工具结果。"
                    "最常见的成因是同一路径读两遍、同一个查询重试、子任务各自再查一遍。"
                ),
                recommendation="先查记忆/索引，再读文件；重复结果写进缓存或记忆，下一次直接引用。",
                estimated_tokens=duplicate_total,
                evidence=[
                    f"`{key[:90]}` ×{count}（{format_tokens(tokens)} tokens，会话 {_short(session)}）"
                    for tokens, session, key, count in top_dupes[:5]
                ],
            )
        )

    # 2. 超大工具结果
    oversized_total = sum(summary.oversized_tokens for summary in summaries)
    if oversized_total > 0:
        heaviest = sorted(
            (
                (result.tokens, record.session_id, call.name)
                for record in records
                for call in record.tool_calls
                if (result := record.tool_results.get(call.call_id))
                and result.tokens > OVERSIZED_RESULT_TOKENS
            ),
            reverse=True,
        )[:5]
        findings.append(
            Finding(
                code="oversized_tool_outputs",
                severity="high" if oversized_total > totals.total_tokens * 0.1 else "medium",
                title="工具一次倒进来太多内容",
                detail=(
                    f"超过 {format_tokens(OVERSIZED_RESULT_TOKENS)} 的单条工具结果，"
                    f"其超出部分合计 {format_tokens(oversized_total)} tokens。上下文一旦变长，"
                    "之后每一步都要重新为它付 prompt 钱。"
                ),
                recommendation=(
                    "读文件先 grep 定位再读区间；网页用 query 聚焦；shell 输出加 head/limit；"
                    "把完整结果落盘，只把结论带进上下文。"
                ),
                estimated_tokens=int(oversized_total * 0.6),
                evidence=[
                    f"`{name}` 单条 {format_tokens(tokens)}（会话 {_short(session)}）"
                    for tokens, session, name in heaviest
                ],
            )
        )

    # 3. 前缀缓存命中低，且前缀本身在变
    low_cache = [
        record
        for record in records
        if record.prompt_tokens >= CACHE_PROMPT_FLOOR and record.cache_hit_ratio < 0.4
    ]
    low_cache_tokens = sum(record.prompt_tokens for record in low_cache)
    if low_cache:
        worst = sorted(low_cache, key=lambda record: record.prompt_tokens, reverse=True)[:3]
        findings.append(
            Finding(
                code="low_prefix_cache",
                severity="medium",
                title="prompt 前缀缓存命中偏低",
                detail=(
                    f"{len(low_cache)} 个会话的 prompt 超过 {format_tokens(CACHE_PROMPT_FLOOR)}，"
                    f"但缓存命中率低于 40%，合计 {format_tokens(low_cache_tokens)} prompt tokens。"
                    "多半是 system prompt 或工具定义每次都变，或者频繁换模型/会话。"
                ),
                recommendation=(
                    "把稳定内容放最前（system、工具定义、项目约定），动态内容放最后；"
                    "同一任务尽量在同一会话里做；别在 system 里塞时间戳或随机 id。"
                ),
                estimated_tokens=int(low_cache_tokens * 0.1),
                evidence=[
                    f"会话 {_short(record.session_id)}：{format_tokens(record.prompt_tokens)} prompt，"
                    f"命中 {record.cache_hit_ratio:.0%}"
                    for record in worst
                ],
            )
        )

    # 3b. 工具集反复变
    unstable_tools = [
        record
        for record in records
        if len(record.steps) >= 10
        and record.header_changes >= max(3, len(record.steps) // 10)
        and record.cache_hit_ratio < 0.7
    ]
    if unstable_tools:
        tools_tokens = sum(record.prompt_tokens for record in unstable_tools)
        worst = sorted(unstable_tools, key=lambda record: record.prompt_tokens, reverse=True)[:3]
        added: Counter[str] = Counter()
        removed: Counter[str] = Counter()
        for record in unstable_tools:
            added.update(record.tool_added)
            removed.update(record.tool_removed)
        changed_tools = sorted(
            set(added) | set(removed),
            key=lambda item: added[item] + removed[item],
            reverse=True,
        )[:5]
        tool_evidence = [
            f"工具 {name} +{added[name]}/-{removed[name]}" for name in changed_tools
        ]
        findings.append(
            Finding(
                code="unstable_tools",
                severity="high" if any(record.cache_hit_ratio < 0.4 for record in unstable_tools) else "medium",
                title="工具集在变，缓存前缀跟着失效",
                detail=(
                    f"{len(unstable_tools)} 个会话里工具定义反复变化"
                    f"（最多 {max(record.header_changes for record in unstable_tools)} 次），"
                    f"合计 {format_tokens(tools_tokens)} prompt tokens。"
                    "工具 schema 位于请求最前面，一变，后面的历史缓存全部作废。"
                ),
                recommendation=(
                    "同一任务里别切换工具集；用固定的小工具集，按需在会话开始前定好；"
                    "别按步骤临时增减工具。"
                ),
                estimated_tokens=int(tools_tokens * 0.1),
                evidence=[
                    f"会话 {_short(record.session_id)}：{len(record.steps)} 步，"
                    f"header 变 {record.header_changes} 次，命中 {record.cache_hit_ratio:.0%}"
                    for record in worst
                ]
                + tool_evidence,
            )
        )

    # 3c. system prompt 被反复改写（语义真的变了）
    unstable_system = [
        record
        for record in records
        if len(record.steps) >= 10
        and record.system_changes >= 3
        and record.cache_hit_ratio < 0.7
    ]
    if unstable_system:
        system_tokens = sum(record.prompt_tokens for record in unstable_system)
        worst = sorted(unstable_system, key=lambda record: record.prompt_tokens, reverse=True)[:3]
        findings.append(
            Finding(
                code="unstable_system",
                severity="medium",
                title="system prompt 反复改写",
                detail=(
                    f"{len(unstable_system)} 个会话里 system prompt 语义发生变化"
                    f"（最多 {max(record.system_changes for record in unstable_system)} 次），"
                    f"合计 {format_tokens(system_tokens)} prompt tokens。"
                ),
                recommendation=(
                    "规则类内容会话内保持不动；要加约束就追加到最后一条 user 消息；"
                    "别把每步生成的清单写回 system。"
                ),
                estimated_tokens=int(system_tokens * 0.1),
                evidence=[
                    f"会话 {_short(record.session_id)}：{len(record.steps)} 步，"
                    f"system 变 {record.system_changes} 次，命中 {record.cache_hit_ratio:.0%}"
                    for record in worst
                ],
            )
        )

    # 3d. system prompt 每一步都变，但只是时间戳之类的动态注入
    volatile_system = [
        record
        for record in records
        if len(record.steps) >= 10
        and record.system_volatile_changes >= max(5, len(record.steps) // 2)
        and record.cache_hit_ratio < 0.5
    ]
    if volatile_system:
        volatile_tokens = sum(record.prompt_tokens for record in volatile_system)
        max_volatile = max(record.system_volatile_changes for record in volatile_system)
        worst = sorted(volatile_system, key=lambda record: record.prompt_tokens, reverse=True)[:3]
        findings.append(
            Finding(
                code="volatile_system_prompt",
                severity="high" if any(record.cache_hit_ratio < 0.2 for record in volatile_system) else "medium",
                title="system prompt 每步都在变，缓存从第一步就打不中",
                detail=(
                    f"{len(volatile_system)} 个会话的 system prompt 几乎每步都不同，"
                    f"但去掉时间戳后语义没变（最多 {max_volatile} 次），"
                    f"合计 {format_tokens(volatile_tokens)} prompt tokens。"
                    "这通常是运行时把「当前时间/环境快照」注入了 system，而不是规则真的改了。"
                ),
                recommendation=(
                    "把动态内容从 system 挪到最后一条 user 消息；system 只放稳定规则。"
                    "如果无法改 DSH 的注入方式，至少别在 system 里放随时间变化的清单。"
                ),
                estimated_tokens=int(volatile_tokens * 0.15),
                evidence=[
                    f"会话 {_short(record.session_id)}：{len(record.steps)} 步，"
                    f"system 原始变化 {record.system_volatile_changes} 次（语义未变），"
                    f"命中 {record.cache_hit_ratio:.0%}"
                    for record in worst
                ],
            )
        )

    # 4. 静态前缀过重（工具集 / system prompt）
    static_tokens = max(totals.system_tokens + totals.tool_schema_tokens, 0)
    if static_tokens >= STATIC_PREFIX_TOKENS and totals.steps:
        per_step = static_tokens * totals.steps
        findings.append(
            Finding(
                code="static_prefix_bloat",
                severity="medium" if static_tokens >= STATIC_PREFIX_TOKENS * 2 else "low",
                title="每次请求都带着很大的工具集与系统提示",
                detail=(
                    f"单次请求的静态前缀约 {format_tokens(static_tokens)} tokens"
                    f"（system {format_tokens(totals.system_tokens)} + 工具 schema "
                    f"{format_tokens(totals.tool_schema_tokens)}），"
                    f"按 {totals.steps} 步计约 {format_tokens(per_step)} tokens 被反复携带。"
                ),
                recommendation=(
                    "个人版只留当前任务需要的工具；AGENTS.md 只放「到哪都成立」的约定；"
                    "把长规则拆到按需加载的文件，别每次全量注入。"
                ),
                estimated_tokens=int(static_tokens * totals.steps * 0.15),
                evidence=[
                    f"工具 {max((len(record.tool_names) for record in records), default=0)} 个，"
                    f"schema {format_tokens(totals.tool_schema_tokens)} tokens"
                ],
            )
        )

    # 5. 上下文膨胀：每步 prompt 很大，或长会话从不压缩
    avg_prompts = [
        (record.prompt_tokens // len(record.steps), record)
        for record in records
        if len(record.steps) >= 5
    ]
    bloated = [(avg, record) for avg, record in avg_prompts if avg >= 120_000]
    unpruned = [
        record
        for record in records
        if len(record.steps) >= 20
        and record.compression_calls == 0
        and record.compactions == 0
        and record.max_prompt_tokens >= 100_000
    ]
    if bloated or unpruned:
        targets = [record for _, record in bloated] or unpruned
        worst_avg = max((avg for avg, _ in bloated), default=0)
        target_tokens = sum(record.prompt_tokens for record in targets)
        if bloated:
            detail = (
                f"{len(bloated)} 个会话平均每步 prompt 超过 120K"
                f"（最高 {format_tokens(worst_avg)}），"
                f"合计 {format_tokens(target_tokens)} prompt tokens。"
                "缓存能打折，但每步新增的那部分仍按原价计费。"
            )
        else:
            detail = (
                f"{len(unpruned)} 个会话超过 20 步且 prompt 到过 "
                f"{format_tokens(max(record.max_prompt_tokens for record in unpruned))}，"
                "但既没有手动 compress 也没有自动压缩。"
            )
        findings.append(
            Finding(
                code="context_bloat",
                severity="high" if worst_avg >= 200_000 or len(unpruned) >= 3 else "medium",
                title="上下文一路膨胀，每步都在为历史付钱",
                detail=detail,
                recommendation=(
                    "到阶段边界就压缩：工具输出提炼成结论、死路只留一行、已完成阶段落盘；"
                    "长任务拆会话，别在一个会话里从调研做到交付。"
                ),
                estimated_tokens=int(target_tokens * 0.1),
                evidence=[
                    f"会话 {_short(record.session_id)}：{len(record.steps)} 步，"
                    f"prompt {format_tokens(record.prompt_tokens)}"
                    + (f"，每步均 {format_tokens(avg)}" if avg else "")
                    for avg, record in sorted(bloated, key=lambda item: item[0], reverse=True)[:3]
                ]
                or [
                    f"会话 {_short(record.session_id)}：{len(record.steps)} 步，"
                    f"最大 prompt {format_tokens(record.max_prompt_tokens)}"
                    for record in unpruned[:3]
                ],
            )
        )

    # 6. 推理占比过高
    heavy_reasoning = [
        record
        for record in records
        if record.output_tokens > 5_000
        and sum(step.reasoning_chars for step in record.steps)
        > 3 * max(1, sum(step.text_chars for step in record.steps))
    ]
    if heavy_reasoning:
        output_tokens = sum(record.output_tokens for record in heavy_reasoning)
        findings.append(
            Finding(
                code="heavy_reasoning",
                severity="low",
                title="推理输出远多于最终答复",
                detail=(
                    f"{len(heavy_reasoning)} 个会话里 reasoning 文本是正文的 3 倍以上，"
                    f"output 合计 {format_tokens(output_tokens)} tokens。"
                ),
                recommendation="简单任务降 reasoning effort；把『先想清楚再动手』留在复杂任务上。",
                estimated_tokens=int(output_tokens * 0.2),
                evidence=[
                    f"会话 {_short(record.session_id)}：output {format_tokens(record.output_tokens)} tokens"
                    for record in heavy_reasoning[:3]
                ],
            )
        )

    # 7. 供应商重试：不一定重复计费，但会拖长会话、增加上下文
    retrying = [record for record in records if record.llm_retries >= 3]
    if retrying:
        retries = sum(record.llm_retries for record in retrying)
        findings.append(
            Finding(
                code="provider_retries",
                severity="low",
                title="供应商重试次数偏多",
                detail=(
                    f"{len(retrying)} 个会话共 {retries} 次 llm/retry。"
                    "重试本身可能不计费，但它会拖长会话、把失败前后的上下文都留在记录里。"
                ),
                recommendation="换个时段或换条通道；长任务先拆小请求，别让一次失败拖垮整轮。",
                estimated_tokens=0,
                evidence=[
                    f"会话 {_short(record.session_id)}：{record.llm_retries} 次重试"
                    for record in sorted(retrying, key=lambda r: r.llm_retries, reverse=True)[:3]
                ],
            )
        )

    # 8. 重复读同一文件（单列，避免与重复调用混淆）
    repeated_reads = []
    for record in records:
        for path, count, tokens in record.repeated_file_reads()[:2]:
            if tokens > 0:
                repeated_reads.append((tokens, record.session_id, path, count))
    repeated_reads.sort(reverse=True)
    if repeated_reads and sum(item[0] for item in repeated_reads) > totals.total_tokens * 0.02:
        read_tokens = sum(item[0] for item in repeated_reads)
        findings.append(
            Finding(
                code="repeated_file_reads",
                severity="low",
                title="同一文件被反复整读",
                detail=f"同一路径（相同区间）被读两次以上，重复结果约 {format_tokens(read_tokens)} tokens。",
                recommendation="第一次读就记下关键行号与结论；后续用 grep/read offset，不重复整篇读。",
                estimated_tokens=read_tokens,
                evidence=[
                    f"`{path}` ×{count}（{format_tokens(tokens)}）" for tokens, _, path, count in repeated_reads[:5]
                ],
            )
        )

    findings.sort(
        key=lambda finding: (SEVERITY_ORDER.get(finding.severity, 9), -finding.estimated_tokens)
    )
    if totals.has_cost:
        for finding in findings:
            finding.estimated_usd = finding.estimated_tokens / 1_000_000 * totals.blended_usd_per_m
    return AuditReport(
        home=home,
        generated_at=generated_at or int(time.time() * 1000),
        totals=totals,
        findings=findings,
        sessions=sorted(summaries, key=lambda item: item.total_tokens, reverse=True),
        skipped=skipped or [],
        days=days,
        usage=usage,
    )


def audit_home(
    home: str | Path | None = None,
    *,
    days: int | None = None,
    limit: int | None = None,
    max_bytes: int | None = 24 * 1024 * 1024,
    workspace: str | None = None,
    pricing: str | Path | None = None,
    usage: str | Path | bool | None = None,
) -> AuditReport:
    resolved = resolve_dsh_home(home)
    prices = load_prices(pricing) if pricing else dict(DEFAULT_PRICES)
    cutoff_ms: int | None = None
    since: float | None = None
    if days:
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        # 文件 mtime 只做粗筛，多留 7 天余量；精确口径用会话内最后事件时间
        since = time.time() - (days + 7) * 86400
    # 有 --days 时先多读再精筛，limit 必须作用在筛选后的结果上
    files = find_session_files(
        resolved,
        since=since,
        max_bytes=max_bytes,
        limit=None if cutoff_ms is not None else limit,
    )
    records: list[SessionRecord] = []
    skipped: list[str] = []
    for path in files:
        if workspace and workspace.lower() not in str(path).lower():
            continue
        try:
            record = load_session(path)
        except (SessionReadError, OSError) as exc:
            skipped.append(f"{path}: {exc}")
            continue
        if cutoff_ms is not None:
            ended = record.ended_at or int(path.stat().st_mtime * 1000)
            if ended < cutoff_ms:
                continue
        records.append(record)
    if cutoff_ms is not None:
        records.sort(key=lambda item: item.ended_at, reverse=True)
        if limit is not None:
            records = records[:limit]

    usage_summary: UsageSummary | None = None
    if usage is not False:
        usage_path = Path(usage).expanduser() if isinstance(usage, str | Path) else find_usage_file(resolved)
        if usage_path is not None and Path(usage_path).is_file():
            try:
                usage_summary = summarize(load_usage(usage_path), since_ms=cutoff_ms)
            except Exception as exc:  # 账本坏了不该拖垮审计
                skipped.append(f"{usage_path}: {exc}")

    return audit_records(
        records,
        home=resolved,
        days=days,
        skipped=skipped,
        prices=prices,
        usage=usage_summary,
    )
