# -*- coding: utf-8 -*-
"""gufa_console Web 控制台测试（纯本地，不联网）。"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gufa_console as gc  # noqa: E402
import gufa_quant_pro as g  # noqa: E402


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def server_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    payload = g.default_config_dict()
    payload["runtime"]["state_dir"] = str(tmp_path / "runtime")
    write_json(config_path, payload)
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setenv("GUFA_CREDENTIALS_FILE", str(credentials_path))

    state_dir = tmp_path / "runtime"
    state_dir.mkdir(exist_ok=True)
    write_json(state_dir / "health.json", {"ts": "2026-08-06T00:00:00+00:00", "valid": True})
    write_json(state_dir / "equity.jsonl", [])  # 占位，稍后覆盖
    (state_dir / "equity.jsonl").write_text(
        json.dumps({"ts": "2026-08-06T00:00:00+00:00", "equity": 1000.0}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-08-06T01:00:00+00:00", "equity": 1050.0}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (state_dir / "orders.audit.jsonl").write_text(
        json.dumps({"ts": "2026-08-06T00:30:00+00:00", "event": "order_fill",
                    "plan": {"symbol": "BTC/USDT", "side": "buy", "amount": 0.01, "price": 60000.0}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (state_dir / "gufa_quant.jsonl").write_text(
        json.dumps({"ts": "2026-08-06T00:00:00+00:00", "level": "INFO", "message": "hello"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    server = gc.ConsoleServer(("127.0.0.1", 0), gc.ConsoleHandler, config_path, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield {"server": server, "config_path": config_path, "state_dir": state_dir,
           "credentials_path": credentials_path, "port": port}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def api(port: int, method: str, path: str, body: object = None, token: str = "test-token"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}


def test_status_requires_token(server_env) -> None:
    status, body = api(server_env["port"], "GET", "/api/status", token="")
    assert status == 401
    assert "令牌" in body.get("error", "")


def test_status_payload(server_env) -> None:
    status, body = api(server_env["port"], "GET", "/api/status")
    assert status == 200
    assert body["config_exists"] is True
    assert body["config_valid"] is True
    assert body["paused"] is False
    assert body["managed"]["running"] is False
    assert body["exchange"]["sandbox"] is True
    assert body["last_equity"] == 0.0


def test_control_pause_resume(server_env) -> None:
    pause_file = server_env["state_dir"] / "pause"
    _, body = api(server_env["port"], "POST", "/api/control", {"action": "pause"})
    assert body["ok"] is True and pause_file.exists()
    _, body = api(server_env["port"], "POST", "/api/control", {"action": "resume"})
    assert body["ok"] is True and not pause_file.exists()


def test_config_save_and_validation(server_env) -> None:
    _, body = api(server_env["port"], "POST", "/api/config", {
        "runtime": {"symbols": ["ETH/USDT"], "poll_interval_seconds": 90},
        "exchange": {"proxy_url": "http://127.0.0.1:7892"},
    })
    assert body["ok"] is True
    cfg = g.AppConfig.load(server_env["config_path"])
    assert cfg.runtime.symbols == ["ETH/USDT"]
    assert cfg.runtime.poll_interval_seconds == 90
    assert cfg.exchange.proxy_url == "http://127.0.0.1:7892"

    # 非法代理 → 400
    status, body = api(server_env["port"], "POST", "/api/config",
                       {"exchange": {"proxy_url": "socks5://x"}})
    assert status == 400
    assert "http" in body.get("error", "")


def test_credentials_save(server_env) -> None:
    _, body = api(server_env["port"], "POST", "/api/credentials", {
        "exchange_api_key": "key-1",
        "exchange_secret": "secret-1",
        "exchange_passphrase": "pass-1",
        "ai_api_key": "ai-1",
    })
    assert body["ok"] is True
    store = g.CredentialStore(server_env["credentials_path"])
    assert store.stored("GUFA_API_KEY") == "key-1"
    assert store.stored("GUFA_API_SECRET") == "secret-1"
    assert store.stored("GUFA_API_PASSWORD") == "pass-1"
    assert store.stored("OPENAI_API_KEY") == "ai-1"


def test_read_only_endpoints(server_env) -> None:
    _, equity = api(server_env["port"], "GET", "/api/equity")
    assert len(equity) == 2 and equity[-1]["equity"] == 1050.0

    _, stats = api(server_env["port"], "GET", "/api/stats")
    assert stats["cycles"] == 2
    assert stats["fills"] == 1
    assert stats["return_pct"] == 5.0

    _, audit = api(server_env["port"], "GET", "/api/audit")
    assert audit[0]["event"] == "order_fill"

    _, log = api(server_env["port"], "GET", "/api/log")
    assert log[0]["message"] == "hello"

    _, cfg = api(server_env["port"], "GET", "/api/config")
    assert cfg["exists"] is True
    assert cfg["exchange"]["proxy_url"] == ""


def test_control_start_requires_valid_config(server_env) -> None:
    # 配置被破坏后 start 应报错而非崩溃
    write_json(server_env["config_path"], {"broken": True})
    _, body = api(server_env["port"], "POST", "/api/control", {"action": "start"})
    assert body["ok"] is False
    assert "配置" in body.get("error", "")


def test_dashboard_html_served(server_env) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{server_env['port']}/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "GuFaQuant 控制台" in html
    assert "启动交易" in html


def test_default_credentials_path_override(server_env) -> None:
    assert "GUFA_CREDENTIALS_FILE" in __import__("os").environ
    store = gc.ConsoleServer(("127.0.0.1", 0), gc.ConsoleHandler,
                             server_env["config_path"], "x").credentials_store()
    assert store.path == server_env["credentials_path"]
