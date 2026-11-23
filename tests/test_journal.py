from __future__ import annotations

import json
from pathlib import Path

import pytest

from agh_lite.journal import GENESIS, HashChainJournal, JournalCorrupt


def test_append_builds_chain(tmp_path: Path) -> None:
    journal = HashChainJournal(tmp_path / "j.jsonl")
    first = journal.append("audit", {"sessions": 1})
    second = journal.append("cache.hit", {"key": "abc"})
    assert first.seq == 0
    assert first.prev == GENESIS
    assert second.prev == first.hash
    assert journal.verify().ok


def test_reopen_continues_chain(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    HashChainJournal(path).append("a", {})
    reopened = HashChainJournal(path)
    entry = reopened.append("b", {})
    assert entry.seq == 1
    assert reopened.verify().count == 2


def test_entries_limit(tmp_path: Path) -> None:
    journal = HashChainJournal(tmp_path / "j.jsonl")
    for index in range(5):
        journal.append("n", {"i": index})
    assert [entry.payload["i"] for entry in journal.entries(limit=2)] == [3, 4]
    with pytest.raises(ValueError):
        journal.entries(limit=0)


def test_tampered_payload_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    HashChainJournal(path).append("a", {"value": 1})
    lines = path.read_text(encoding="utf-8").splitlines()
    raw = json.loads(lines[0])
    raw["payload"]["value"] = 2
    lines[0] = json.dumps(raw, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = HashChainJournal(path).verify()
    assert not result.ok
    assert result.broken_at == 0


def test_removed_entry_breaks_sequence(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = HashChainJournal(path)
    journal.append("a", {})
    journal.append("b", {})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[1] + "\n", encoding="utf-8")
    result = HashChainJournal(path).verify()
    assert not result.ok
    assert "序号" in result.reason or "prev" in result.reason


def test_unparsable_line_is_corrupt_on_warm_up(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(JournalCorrupt):
        HashChainJournal(path)


def test_summary(tmp_path: Path) -> None:
    journal = HashChainJournal(tmp_path / "j.jsonl")
    journal.append("a", {})
    journal.append("a", {})
    journal.append("b", {})
    summary = journal.summary()
    assert summary["entries"] == 3
    assert summary["kinds"] == {"a": 2, "b": 1}
