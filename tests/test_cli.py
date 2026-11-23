from __future__ import annotations

import json

import pytest

from agh_lite.cli import main


def test_tokens_from_file(tmp_path, capsys) -> None:
    path = tmp_path / "t.txt"
    path.write_text("你好世界", encoding="utf-8")
    assert main(["tokens", "--input", str(path)]) == 0
    out = capsys.readouterr().out
    assert "估算 tokens：3" in out


def test_tokens_session(capsys, write_session) -> None:
    assert main(["tokens", "--session", str(write_session())]) == 0
    assert "tokens" in capsys.readouterr().out


def test_tokens_stdin(monkeypatch, capsys) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("abcd"))
    assert main(["tokens"]) == 0
    assert "估算 tokens：2" in capsys.readouterr().out


def test_pack_from_json(tmp_path, capsys) -> None:
    payload = {
        "segments": [
            {"id": "a", "text": "hello world", "priority": 0.9},
            {"id": "b", "text": "hello world", "priority": 0.5},
        ]
    }
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["pack", "--input", str(path), "--budget", "100"]) == 0
    out = capsys.readouterr().out
    assert "去重 1 段" in out


def test_pack_json_format(tmp_path, capsys) -> None:
    path = tmp_path / "segments.json"
    path.write_text(json.dumps([{"id": "a", "text": "hello"}]), encoding="utf-8")
    assert main(["pack", "--input", str(path), "--budget", "100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kept"] == 1
    assert data["budget"] == 100


def test_pack_bad_input(tmp_path, capsys) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    assert main(["pack", "--input", str(path), "--budget", "10"]) == 2
    assert "读取输入失败" in capsys.readouterr().err


def test_audit_markdown_to_file(tmp_path, write_session) -> None:
    write_session()
    out_path = tmp_path / "report.md"
    assert main(["audit", "--home", str(tmp_path), "--out", str(out_path)]) == 0
    text = out_path.read_text(encoding="utf-8")
    assert "DSH Token 审计报告" in text


def test_audit_json(capsys, tmp_path, write_session) -> None:
    write_session()
    assert main(["audit", "--home", str(tmp_path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["sessions"] == 1


def test_audit_redact_hides_local_paths(capsys, tmp_path, write_session) -> None:
    write_session()
    assert main(["audit", "--home", str(tmp_path), "--format", "json", "--redact"]) == 0
    text = capsys.readouterr().out
    assert "G:\\\\work" not in text
    assert "<local-path>" in text


def test_audit_missing_home(tmp_path, capsys) -> None:
    assert main(["audit", "--home", str(tmp_path)]) == 2
    assert "审计失败" in capsys.readouterr().err


def test_cache_stats_missing(tmp_path, capsys) -> None:
    assert main(["cache", "stats", "--cache", str(tmp_path / "none.sqlite")]) == 0
    assert "缓存不存在" in capsys.readouterr().out


def test_cache_stats_after_write(tmp_path, capsys) -> None:
    from agh_lite.cache import ResponseCache

    path = tmp_path / "c.sqlite"
    with ResponseCache(path) as cache:
        key = cache.make_key("m", [{"role": "user", "content": "x"}])
        cache.put(key, "ok", prompt_tokens=10)
    assert main(["cache", "stats", "--cache", str(path)]) == 0
    out = capsys.readouterr().out
    assert "条目：1" in out


def test_journal_append_and_verify(tmp_path, capsys) -> None:
    path = tmp_path / "j.jsonl"
    assert main(["journal", "append", "--journal", str(path), "--kind", "note", "--data", '{"a":1}']) == 0
    assert main(["journal", "verify", "--journal", str(path)]) == 0
    assert "链完整" in capsys.readouterr().out


def test_journal_list(tmp_path, capsys) -> None:
    path = tmp_path / "j.jsonl"
    main(["journal", "append", "--journal", str(path), "--kind", "note"])
    assert main(["journal", "list", "--journal", str(path)]) == 0
    assert "#0" in capsys.readouterr().out


def test_journal_bad_data(tmp_path, capsys) -> None:
    path = tmp_path / "j.jsonl"
    assert main(["journal", "append", "--journal", str(path), "--data", "nope"]) == 2
    assert "不是合法 JSON" in capsys.readouterr().err


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "agh-lite" in capsys.readouterr().out


def test_missing_command_exits() -> None:
    with pytest.raises(SystemExit):
        main([])
