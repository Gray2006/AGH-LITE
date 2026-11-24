from __future__ import annotations

from agh_lite.cli import main
from agh_lite.doctor import format_checks, run_checks


def test_doctor_reports_home_and_sessions(tmp_path, write_session) -> None:
    write_session()
    checks = run_checks(tmp_path, proxy_port=0)
    names = {check.name for check in checks}
    assert "Python" in names
    assert "DSH 数据目录" in names
    assert "会话目录" in names
    text = format_checks(checks)
    assert "自检" in text
    assert "[ok] Python" in text


def test_doctor_cli_exit_code(tmp_path, write_session, capsys) -> None:
    write_session()
    assert main(["doctor", "--home", str(tmp_path), "--proxy-port", "0"]) == 0
    out = capsys.readouterr().out
    assert "自检" in out


def test_doctor_missing_home_fails(tmp_path) -> None:
    assert main(["doctor", "--home", str(tmp_path / "missing"), "--proxy-port", "0"]) == 1
