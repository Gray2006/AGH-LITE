"""agh-lite 命令行：审计、打包、缓存、日志。

设计原则：默认只读、默认打印人话、需要机器读时才加 `--format json`。
所有子命令都能被脚本调用，失败时返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .audit import audit_home
from .budget import BudgetExceeded
from .cache import ResponseCache
from .journal import HashChainJournal, JournalCorrupt
from .pack import PackBudgetExceeded, Segment, pack
from .pricing import format_usd
from .proxy import ProxyConfig, serve
from .sessions import SessionReadError, load_session
from .tokens import estimate_text_tokens, format_tokens

DEFAULT_CACHE = "~/.dsh/agh-lite-cache.sqlite"
DEFAULT_JOURNAL = "~/.dsh/agh-lite-journal.jsonl"

_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s\"`）)]*")


def _redact(text: str) -> str:
    """把报告里的本机绝对路径换掉，便于公开分享。"""
    text = text.replace("G:\\DSH\\.dsh", "$DSH_HOME")
    return _WINDOWS_PATH.sub("<local-path>", text)


# ---------------------------------------------------------------- audit
def _cmd_audit(args: argparse.Namespace) -> int:
    try:
        report = audit_home(
            args.home,
            days=args.days,
            limit=args.limit,
            max_bytes=args.max_bytes,
            workspace=args.workspace,
            pricing=args.pricing,
            usage=False if args.no_usage else args.usage,
        )
    except (FileNotFoundError, SessionReadError, ValueError, json.JSONDecodeError) as exc:
        print(f"审计失败：{exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = json.dumps(report.to_dict(top=args.top), ensure_ascii=False, indent=2)
    elif args.format == "html":
        payload = report.render_html(top=args.top)
    else:
        payload = report.render_markdown(top=args.top)
    if args.redact:
        payload = _redact(payload)

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        print(f"报告已写入 {out}")
        print(
            f"会话 {report.totals.sessions} 个，总量 {format_tokens(report.totals.total_tokens)}，"
            f"估算可省 {format_tokens(report.potential_savings)}"
        )
    else:
        print(payload)
    return 0


# ----------------------------------------------------------------- pack
def _load_segments(path: str) -> list[Segment]:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    items = raw.get("segments", raw) if isinstance(raw, dict) else raw
    segments = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            segments.append(Segment(id=f"s{index}", text=item))
        else:
            segments.append(
                Segment(
                    id=str(item.get("id", f"s{index}")),
                    text=str(item.get("text", "")),
                    role=str(item.get("role", "context")),
                    kind=str(item.get("kind", "note")),
                    priority=float(item.get("priority", 0.5)),
                    pinned=bool(item.get("pinned", False)),
                )
            )
    return segments


def _cmd_pack(args: argparse.Namespace) -> int:
    try:
        segments = _load_segments(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取输入失败：{exc}", file=sys.stderr)
        return 2
    try:
        result = pack(segments, budget=args.budget, protect_last=args.protect_last)
    except PackBudgetExceeded as exc:
        result = exc.result
        print(
            f"预算 {result.budget} 不够，受保护内容已占 {result.total_tokens}；"
            f"丢弃 {len(result.dropped)} 段",
            file=sys.stderr,
        )
        if not args.force:
            return 3

    if args.format == "json":
        print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    else:
        print(
            f"保留 {result.kept}/{len(segments)} 段，{result.total_tokens}/{result.budget} tokens，"
            f"去重 {result.duplicates} 段，丢弃 {len(result.dropped)} 段"
        )
        for dropped in result.dropped:
            print(f"  - {dropped.id}: {dropped.reason}（{dropped.tokens}）")
        if args.render:
            sys.stdout.write(result.render() + "\n")
    return 0


# ---------------------------------------------------------------- cache
def _cache_path(args: argparse.Namespace) -> Path:
    return Path(args.cache or DEFAULT_CACHE).expanduser()


def _cmd_cache(args: argparse.Namespace) -> int:
    path = _cache_path(args)
    if args.cache_action == "stats":
        if not path.exists():
            print(f"缓存不存在：{path}")
            return 0
        cache = ResponseCache(path)
        try:
            stats = cache.stats()
        finally:
            cache.close()
        print(f"路径：{path}")
        print(f"条目：{stats.entries}")
        print(f"命中：{stats.hits}")
        print(
            f"累计节省：prompt {format_tokens(stats.prompt_tokens_saved)} / "
            f"completion {format_tokens(stats.completion_tokens_saved)} tokens"
        )
        if stats.saved_usd is not None:
            print(f"折合费用：约 {format_usd(stats.saved_usd)}")
        if stats.unpriced_models:
            print(f"未定价模型：{', '.join(stats.unpriced_models)}")
        print(f"体积：{stats.db_bytes / 1024:.1f} KiB")
        return 0
    if args.cache_action == "purge":
        cache = ResponseCache(path)
        try:
            removed = cache.purge(older_than_seconds=args.days * 86400)
        finally:
            cache.close()
        print(f"清掉 {removed} 条过期缓存")
        return 0
    print("用法：agh-lite cache {stats,purge}", file=sys.stderr)
    return 2


# -------------------------------------------------------------- journal
def _cmd_journal(args: argparse.Namespace) -> int:
    path = Path(args.journal or DEFAULT_JOURNAL).expanduser()
    journal = HashChainJournal(path)
    if args.journal_action == "append":
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as exc:
            print(f"--data 不是合法 JSON：{exc}", file=sys.stderr)
            return 2
        entry = journal.append(args.kind, payload)
        print(f"#{entry.seq} {entry.hash[:12]}")
        return 0
    if args.journal_action == "list":
        for entry in journal.entries(limit=args.limit):
            print(f"#{entry.seq} {entry.kind} {entry.hash[:12]}")
        return 0
    if args.journal_action == "verify":
        result = journal.verify()
        print(result.describe())
        return 0 if result.ok else 1
    print("用法：agh-lite journal {append,list,verify}", file=sys.stderr)
    return 2


# --------------------------------------------------------------- tokens
def _cmd_tokens(args: argparse.Namespace) -> int:
    if args.input:
        text = Path(args.input).expanduser().read_text(encoding="utf-8")
    elif args.session:
        record = load_session(Path(args.session).expanduser())
        print(record.describe())
        return 0
    else:
        text = sys.stdin.read()
    estimate = estimate_text_tokens(text)
    print(f"字符：{len(text):,}")
    print(f"估算 tokens：{estimate:,}（{format_tokens(estimate)}）")
    print(f"按 4.0 字/token 参考：{len(text) / 4:,.0f}")
    return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    if args.stats:
        from .proxy import load_ledger, summarize_ledger

        ledger = args.ledger or str(Path.home() / ".dsh" / "agh-lite-proxy.jsonl")
        stats = summarize_ledger(load_ledger(ledger))
        if args.json:
            print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(f"账本：{ledger}")
        print(f"请求：{stats.requests}，缓存命中：{stats.cache_hits}，裁剪过：{stats.trimmed_requests}")
        print(f"裁剪字符：{stats.chars_removed:,}（约 {format_tokens(stats.tokens_saved_trim)} tokens）")
        print(f"缓存省下：{format_tokens(stats.tokens_saved_cache)} tokens")
        if stats.cost_saved_usd is not None:
            print(f"折合费用：约 {format_usd(stats.cost_saved_usd)}")
        if stats.unpriced_models:
            print(f"未定价模型：{', '.join(stats.unpriced_models)}")
        return 0
    if not args.upstream:
        print("需要 --upstream，或用 --stats 查看账本", file=sys.stderr)
        return 2
    try:
        config = ProxyConfig(
            upstream=args.upstream,
            host=args.host,
            port=args.port,
            trim=not args.no_trim,
            max_tool_chars=args.max_tool_chars,
            keep_recent=args.keep_recent,
            cache=not args.no_cache,
            cache_path=args.cache_path,
            ledger_path=args.ledger,
            stream_usage=args.stream_usage,
        )
    except ValueError as exc:
        print(f"参数不对：{exc}", file=sys.stderr)
        return 2
    serve(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agh-lite",
        description="DSH 个人版 token 经济工具箱：审计、打包、缓存、日志。",
    )
    parser.add_argument("--version", action="version", version=f"agh-lite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="离线审计 DSH 会话，找 token 浪费")
    audit.add_argument("--home", help="DSH 数据目录（默认 $DSH_HOME 或 ~/.dsh）")
    audit.add_argument("--workspace", help="只审计路径包含该字符串的会话")
    audit.add_argument("--days", type=int, help="只看最近 N 天")
    audit.add_argument("--limit", type=int, help="最多读取 N 个会话文件")
    audit.add_argument("--max-bytes", type=int, default=24 * 1024 * 1024, help="单文件读取上限")
    audit.add_argument("--top", type=int, default=10, help="报告里列几个问题/会话")
    audit.add_argument("--format", choices=["md", "json", "html"], default="md")
    audit.add_argument("--redact", action="store_true", help="把报告里的本机绝对路径替换掉，便于分享")
    audit.add_argument("--pricing", help="自定义价格表 JSON（覆盖内置价格）")
    audit.add_argument("--usage", help="DSH 用量账本路径（默认自动找 $DSH_HOME/.dshw-usage.json）")
    audit.add_argument("--no-usage", action="store_true", help="跳过真实账单对账")
    audit.add_argument("--out", help="写入文件而不是打印")
    audit.set_defaults(func=_cmd_audit)

    pack_cmd = sub.add_parser("pack", help="按预算打包上下文并去重")
    pack_cmd.add_argument("--input", required=True, help="JSON：段列表或 {\"segments\": [...]}")
    pack_cmd.add_argument("--budget", type=int, required=True, help="预算 tokens")
    pack_cmd.add_argument("--protect-last", type=int, default=1, help="末尾保护几段")
    pack_cmd.add_argument("--render", action="store_true", help="打印拼好的上下文")
    pack_cmd.add_argument("--force", action="store_true", help="超预算也输出结果")
    pack_cmd.add_argument("--format", choices=["text", "json"], default="text")
    pack_cmd.set_defaults(func=_cmd_pack)

    cache = sub.add_parser("cache", help="响应缓存统计与清理")
    cache.add_argument("cache_action", nargs="?", choices=["stats", "purge"], default="stats")
    cache.add_argument("--cache", help="缓存文件路径")
    cache.add_argument("--days", type=int, default=30, help="purge 用：保留最近 N 天")
    cache.set_defaults(func=_cmd_cache)

    journal = sub.add_parser("journal", help="哈希链审计日志")
    journal.add_argument("journal_action", nargs="?", choices=["append", "list", "verify"], default="list")
    journal.add_argument("--journal", help="日志文件路径")
    journal.add_argument("--kind", default="note", help="append 用：事件类型")
    journal.add_argument("--data", default="{}", help="append 用：JSON 负载")
    journal.add_argument("--limit", type=int, default=20)
    journal.set_defaults(func=_cmd_journal)

    tokens = sub.add_parser("tokens", help="估算文本或会话的 token")
    tokens.add_argument("--input", help="文本文件；缺省读 stdin")
    tokens.add_argument("--session", help="DSH 会话文件，打印会话画像")
    tokens.set_defaults(func=_cmd_tokens)

    proxy = sub.add_parser("proxy", help="本地 OpenAI 兼容代理：自动裁剪旧工具结果 + 缓存")
    proxy.add_argument("--upstream", help="上游地址，如 https://api.deepseek.com")
    proxy.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    proxy.add_argument("--port", type=int, default=8787)
    proxy.add_argument("--max-tool-chars", type=int, default=4_000, help="单条旧 tool 结果保留的字符数")
    proxy.add_argument("--keep-recent", type=int, default=8, help="最近 N 条消息不动")
    proxy.add_argument("--cache-path", help="缓存文件（默认 ~/.dsh/agh-lite-proxy-cache.sqlite）")
    proxy.add_argument("--ledger", help="节省账本（默认 ~/.dsh/agh-lite-proxy.jsonl）")
    proxy.add_argument("--no-trim", action="store_true", help="只缓存，不裁剪")
    proxy.add_argument("--no-cache", action="store_true", help="只裁剪，不缓存")
    proxy.add_argument("--stream-usage", action="store_true", help="给流式请求加上 include_usage，记账更准")
    proxy.add_argument("--stats", action="store_true", help="不启动服务，只汇总账本里的节省量")
    proxy.add_argument("--json", action="store_true", help="配合 --stats 输出 JSON")
    proxy.set_defaults(func=_cmd_proxy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (BudgetExceeded, JournalCorrupt) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
