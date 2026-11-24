from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agh_lite.ui import UIState, _UIServer


@pytest.fixture
def ui(tmp_path, write_session):
    write_session()
    state = UIState(home=tmp_path, report_path=tmp_path / "report.html")
    server = _UIServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def test_page_is_served(ui) -> None:
    base, _ = ui
    page = _get(base + "/")
    assert "agh-lite" in page
    assert "扫描我的会话" in page


def test_status_and_audit(ui) -> None:
    base, state = ui
    status = json.loads(_get(base + "/api/status"))
    assert status["proxy"]["running"] is False
    assert str(state.home) in status["home"]

    result = _post(base + "/api/audit", {"days": 0})
    assert result["sessions"] == 1
    assert "tokens" in result["tokens"] or result["tokens"]
    assert isinstance(result["findings"], list)
    assert isinstance(result["workspaces"], list)


def test_report_endpoint_writes_html(ui) -> None:
    base, state = ui
    result = _post(base + "/api/report", {"days": 30})
    assert result["ok"] is True
    assert state.report_path.is_file()
    page = _get(base + "/report")
    assert "<!doctype html>" in page
