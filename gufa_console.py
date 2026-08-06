# -*- coding: utf-8 -*-
"""GuFaQuant-Pro Web 控制台（零第三方依赖）。

面向电脑小白与手机端：
- 浏览器访问即用（手机同一局域网可打开）
- 一键配置向导（交易所凭据 / AI 设置 / 代理与交易对）
- 一键启动 / 停止 / 暂停 / 恢复交易
- 只读看板：状态、权益曲线、持仓、成交、日志

安全：
- 默认仅监听 127.0.0.1；手机访问需 --host 0.0.0.0
- 所有 /api/* 需要访问令牌（Bearer Token / ?token=）
- 凭据只写入项目既有 CredentialStore（或环境变量优先），绝不回显
- 本模块不导入 ccxt / openai，启动即用
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import gufa_quant_pro as g

APP_TITLE = "GuFaQuant 控制台"
DEFAULT_PORT = 8600

# Windows 下创建新进程组并隐藏窗口
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 允许通过 Web 修改的非敏感配置字段（白名单，防越权写任意字段）
_ALLOWED_CONFIG: Dict[str, List[str]] = {
    "exchange": ["id", "sandbox", "market_type", "timeout_ms", "max_retries",
                 "retry_base_seconds", "recv_window_ms", "client_order_id_param", "proxy_url"],
    "runtime": ["symbols", "quote_currency", "timeframe", "ohlcv_limit",
                "poll_interval_seconds", "closed_candle_only", "max_candle_lag_seconds",
                "log_level", "log_max_bytes", "log_backup_count", "webhook_url"],
    "ai": ["enabled", "model", "base_url", "timeout_seconds", "minimum_allow_confidence",
           "decision_mode", "fail_closed", "max_output_tokens"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return g.load_json(path)
    except Exception:
        return default


def _read_jsonl(path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    rows = g._read_jsonl(path)  # noqa: SLF001 复用项目容错读取
    if limit > 0:
        return rows[-limit:]
    return rows


def _tail_lines(path: Path, lines: int = 200) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if line:
                    out.append(line)
    except OSError:
        return []
    return out[-lines:]


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mask_url(url: str) -> str:
    """webhook 等 URL 只保留 scheme://host，避免泄露路径中的令牌。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return "***"


class ManagedProcess:
    """控制台托管的交易进程（run 子进程）。"""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.pid: int = 0
        self.started_at: str = ""
        self.cmd: List[str] = []

    @property
    def running(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        return _is_alive(self.pid)


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, config_path: Path, token: str) -> None:
        super().__init__(addr, handler)
        self.config_path = config_path.expanduser().resolve()
        self.token = token
        self.managed = ManagedProcess()
        self.lock = threading.RLock()
        self._restore_managed()

    # ---- 状态目录 ----
    def resolve_state_dir(self) -> Optional[Path]:
        try:
            cfg = g.AppConfig.load(self.config_path)
        except Exception:
            return None
        return Path(cfg.runtime.state_dir).expanduser().resolve()

    def resolve_config(self) -> Optional[g.AppConfig]:
        try:
            return g.AppConfig.load(self.config_path)
        except Exception:
            return None

    def credentials_store(self) -> g.CredentialStore:
        return g.CredentialStore(g.default_credentials_path(self.config_path))

    # ---- 托管进程 ----
    def _restore_managed(self) -> None:
        """重启控制台后，若上次启动的交易进程还在，则继续托管。"""
        state_dir = self.resolve_state_dir()
        if not state_dir:
            return
        pid_file = state_dir / "console.pid.json"
        data = _read_json(pid_file, {})
        pid = int(data.get("pid", 0) or 0)
        if pid > 0 and _is_alive(pid):
            self.managed.pid = pid
            self.managed.started_at = str(data.get("started_at", ""))
            self.managed.cmd = list(data.get("cmd", []))

    def start_trading(self) -> Dict[str, Any]:
        with self.lock:
            if self.managed.running:
                return {"ok": False, "error": "交易进程已在运行"}
            cfg = self.resolve_config()
            if cfg is None:
                return {"ok": False, "error": "配置无效，请先完成设置向导"}
            state_dir = Path(cfg.runtime.state_dir).expanduser().resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve().parent / "gufa_quant_pro.py"
            cmd = [sys.executable, str(script), "--config", str(self.config_path), "run"]
            log_handle = open(state_dir / "console-run.log", "ab", buffering=0)  # noqa: SIM115
            kwargs: Dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(
                    cmd, stdout=log_handle, stderr=subprocess.STDOUT,
                    env=os.environ.copy(), **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                log_handle.close()
                return {"ok": False, "error": f"启动失败: {exc}"}
            self.managed.proc = proc
            self.managed.pid = proc.pid
            self.managed.started_at = utc_now()
            self.managed.cmd = cmd
            g.atomic_write_json(
                state_dir / "console.pid.json",
                {"pid": proc.pid, "started_at": self.managed.started_at, "cmd": cmd},
                mode=0o644,
            )
            return {"ok": True, "pid": proc.pid, "started_at": self.managed.started_at}

    def stop_trading(self) -> Dict[str, Any]:
        with self.lock:
            if not self.managed.running:
                self._clear_pid_file()
                return {"ok": False, "error": "交易进程未在运行"}
            target = self.managed.proc.pid if self.managed.proc is not None else self.managed.pid
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(target), "/T", "/F"],
                        capture_output=True, timeout=20,
                    )
                else:
                    os.killpg(os.getpgid(target), 15)
                    self.managed.proc.wait(timeout=20) if self.managed.proc else None
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"停止失败: {exc}"}
            self.managed.proc = None
            self.managed.pid = 0
            self.managed.started_at = ""
            self._clear_pid_file()
            return {"ok": True}

    def _clear_pid_file(self) -> None:
        state_dir = self.resolve_state_dir()
        if not state_dir:
            return
        try:
            (state_dir / "console.pid.json").unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 只读聚合 ----
    def status_payload(self) -> Dict[str, Any]:
        cfg = self.resolve_config()
        state_dir = self.resolve_state_dir()
        payload: Dict[str, Any] = {
            "config_exists": self.config_path.exists(),
            "config_valid": cfg is not None,
            "managed": {
                "running": self.managed.running,
                "pid": self.managed.pid,
                "started_at": self.managed.started_at,
                "cmd": self.managed.cmd,
            },
        }
        if cfg is not None:
            payload["exchange"] = {
                "id": cfg.exchange.id,
                "sandbox": cfg.exchange.sandbox,
                "market_type": cfg.exchange.market_type,
                "proxy_url": cfg.exchange.proxy_url,
                "proxy_set": bool(cfg.exchange.proxy_url),
            }
            payload["ai"] = {
                "enabled": cfg.ai.enabled,
                "model": cfg.ai.model,
                "base_url": cfg.ai.base_url or "",
                "timeout_seconds": cfg.ai.timeout_seconds,
            }
            payload["runtime"] = {
                "symbols": list(cfg.runtime.symbols),
                "timeframe": cfg.runtime.timeframe,
                "poll_interval_seconds": cfg.runtime.poll_interval_seconds,
                "webhook_url": _mask_url(cfg.runtime.webhook_url),
            }
            store = self.credentials_store()
            payload["credentials"] = {
                "exchange_key": bool(store.stored(cfg.exchange.api_key_env)),
                "exchange_secret": bool(store.stored(cfg.exchange.secret_env)),
                "exchange_password": bool(store.stored(cfg.exchange.password_env)),
                "ai_key": bool(store.stored(cfg.ai.api_key_env)),
            }
        if state_dir is not None:
            health = _read_json(state_dir / "health.json", {})
            state = _read_json(state_dir / "state.json", {})
            pause_file = state_dir / "pause"
            payload["paused"] = pause_file.exists()
            payload["halted_reason"] = str(health.get("halted_reason") or state.get("halted_reason") or "")
            payload["last_cycle_at"] = state.get("last_cycle_at", "")
            payload["last_equity"] = state.get("last_equity", 0.0)
            payload["peak_equity"] = state.get("peak_equity", 0.0)
            payload["day_start_equity"] = state.get("day_start_equity", 0.0)
            payload["trades_today"] = state.get("trades_today", 0)
            payload["positions"] = state.get("positions", {})
            payload["pending_orders"] = state.get("pending_orders", {})
            payload["health"] = health if isinstance(health, dict) else {}
        return payload

    def stats_payload(self) -> Dict[str, Any]:
        state_dir = self.resolve_state_dir()
        if not state_dir:
            return {"cycles": 0}
        return _compute_stats(state_dir, "health.json")

    def validate_now(self) -> Dict[str, Any]:
        cfg = self.resolve_config()
        if cfg is None:
            return {"ok": False, "error": "配置无效，请先完成设置向导"}
        script = Path(__file__).resolve().parent / "gufa_quant_pro.py"
        cmd = [sys.executable, str(script), "--config", str(self.config_path), "validate"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=240,
                env=os.environ.copy(), encoding="utf-8", errors="replace",
            )
            output = (result.stdout or "") + (result.stderr or "")
            return {"ok": result.returncode == 0, "exit": result.returncode,
                    "output": output[-8000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "校验超时（240 秒）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"校验失败: {exc}"}


def _compute_stats(state_dir: Path, health_file: str) -> Dict[str, Any]:
    """与 cmd_stats 同源的只读汇总。"""
    equity_rows = _read_jsonl(state_dir / "equity.jsonl")
    audit_rows = _read_jsonl(state_dir / "orders.audit.jsonl")
    summary: Dict[str, Any] = {"cycles": len(equity_rows)}
    if equity_rows:
        first, last = equity_rows[0], equity_rows[-1]
        summary["period"] = {"first": first.get("ts"), "last": last.get("ts")}
        eq_first, eq_last = first.get("equity"), last.get("equity")
        summary["equity"] = {"first": eq_first, "last": eq_last}
        if isinstance(eq_first, (int, float)) and isinstance(eq_last, (int, float)) and eq_first > 0:
            summary["return_pct"] = round((eq_last / eq_first - 1) * 100, 4)
        peak = float("-inf")
        max_dd = 0.0
        for row in equity_rows:
            eq = row.get("equity")
            if not isinstance(eq, (int, float)) or eq <= 0:
                continue
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak)
        summary["max_drawdown_pct"] = round(max_dd * 100, 4)
    fills = [row for row in audit_rows if row.get("event") == "order_fill"]
    summary["fills"] = len(fills)
    per_symbol: Dict[str, int] = {}
    for fill in fills:
        symbol = (fill.get("plan") or {}).get("symbol") or (fill.get("fill") or {}).get("symbol") or "?"
        per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
    summary["fills_by_symbol"] = per_symbol
    summary["order_errors"] = sum(1 for row in audit_rows if row.get("event") == "order_error")
    summary["order_uncertain"] = sum(1 for row in audit_rows if row.get("event") == "order_uncertain")
    summary["health_exists"] = (state_dir / health_file).exists()
    return summary


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer  # type: ignore[assignment]

    # ---- 基础 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    def _send_json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, code: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            given = header[7:].strip()
        else:
            query = parse_qs(urlparse(self.path).query)
            given = (query.get("token") or [""])[0]
        return bool(given) and secrets.compare_digest(given, token)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_text(DASHBOARD_HTML, content_type="text/html; charset=utf-8")
            return
        if not self._authorized():
            self._send_json({"error": "未授权：请填写访问令牌"}, 401)
            return
        if path == "/api/status":
            self._send_json(self.server.status_payload())
        elif path == "/api/stats":
            self._send_json(self.server.stats_payload())
        elif path == "/api/equity":
            state_dir = self.server.resolve_state_dir()
            rows = _read_jsonl(state_dir / "equity.jsonl", 5000) if state_dir else []
            self._send_json(rows)
        elif path == "/api/audit":
            state_dir = self.server.resolve_state_dir()
            rows = _read_jsonl(state_dir / "orders.audit.jsonl", 500) if state_dir else []
            self._send_json(rows)
        elif path == "/api/log":
            state_dir = self.server.resolve_state_dir()
            query = parse_qs(urlparse(self.path).query)
            try:
                lines = int((query.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200
            raw = _tail_lines(state_dir / "gufa_quant.jsonl", lines) if state_dir else []
            parsed: List[Any] = []
            for line in raw:
                try:
                    parsed.append(json.loads(line))
                except Exception:
                    parsed.append({"message": line})
            self._send_json(parsed)
        elif path == "/api/runlog":
            state_dir = self.server.resolve_state_dir()
            raw = _tail_lines(state_dir / "console-run.log", 200) if state_dir else []
            self._send_json(raw)
        elif path == "/api/config":
            self._send_json(self._config_payload())
        else:
            self._send_json({"error": "未知接口"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._send_json({"error": "未授权：请填写访问令牌"}, 401)
            return
        body = self._read_body()
        if path == "/api/config":
            self._handle_config_save(body)
        elif path == "/api/credentials":
            self._handle_credentials_save(body)
        elif path == "/api/control":
            self._handle_control(body)
        elif path == "/api/validate":
            result = self.server.validate_now()
            self._send_json(result)
        else:
            self._send_json({"error": "未知接口"}, 404)

    # ---- 配置 ----
    def _config_payload(self) -> Dict[str, Any]:
        cfg = self.server.resolve_config()
        if cfg is None:
            return {"exists": False, "config_path": str(self.server.config_path)}
        return {
            "exists": True,
            "config_path": str(self.server.config_path),
            "exchange": {
                "id": cfg.exchange.id, "sandbox": cfg.exchange.sandbox,
                "market_type": cfg.exchange.market_type,
                "timeout_ms": cfg.exchange.timeout_ms, "max_retries": cfg.exchange.max_retries,
                "retry_base_seconds": cfg.exchange.retry_base_seconds,
                "recv_window_ms": cfg.exchange.recv_window_ms,
                "client_order_id_param": cfg.exchange.client_order_id_param,
                "proxy_url": cfg.exchange.proxy_url,
            },
            "runtime": {
                "symbols": list(cfg.runtime.symbols), "quote_currency": cfg.runtime.quote_currency,
                "timeframe": cfg.runtime.timeframe, "ohlcv_limit": cfg.runtime.ohlcv_limit,
                "poll_interval_seconds": cfg.runtime.poll_interval_seconds,
                "closed_candle_only": cfg.runtime.closed_candle_only,
                "max_candle_lag_seconds": cfg.runtime.max_candle_lag_seconds,
                "log_level": cfg.runtime.log_level, "webhook_url": cfg.runtime.webhook_url,
            },
            "ai": {
                "enabled": cfg.ai.enabled, "model": cfg.ai.model, "base_url": cfg.ai.base_url,
                "timeout_seconds": cfg.ai.timeout_seconds,
                "minimum_allow_confidence": cfg.ai.minimum_allow_confidence,
                "decision_mode": cfg.ai.decision_mode, "fail_closed": cfg.ai.fail_closed,
                "max_output_tokens": cfg.ai.max_output_tokens,
            },
        }

    def _handle_config_save(self, body: Dict[str, Any]) -> None:
        if not self.server.config_path.exists():
            g.atomic_write_json(self.server.config_path, g.default_config_dict(), mode=0o644)
        try:
            payload = g.load_json(self.server.config_path)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"现有配置无法读取: {exc}"}, 400)
            return
        for section, fields in _ALLOWED_CONFIG.items():
            if section not in body or not isinstance(body[section], dict):
                continue
            for key in fields:
                if key in body[section]:
                    payload.setdefault(section, {})[key] = body[section][key]
        try:
            g.atomic_write_json(self.server.config_path, payload, mode=0o644)
            g.AppConfig.load(self.server.config_path)  # 校验
        except g.ConfigError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"保存失败: {exc}"}, 400)
            return
        self._send_json({"ok": True})

    def _handle_credentials_save(self, body: Dict[str, Any]) -> None:
        cfg = self.server.resolve_config()
        if cfg is None:
            self._send_json({"ok": False, "error": "配置无效，请先保存配置"}, 400)
            return
        store = self.server.credentials_store()
        mapping = [
            ("exchange_api_key", cfg.exchange.api_key_env),
            ("exchange_secret", cfg.exchange.secret_env),
            ("exchange_passphrase", cfg.exchange.password_env),
            ("ai_api_key", cfg.ai.api_key_env),
        ]
        saved: Dict[str, bool] = {}
        for field, env_name in mapping:
            if field in body:
                store.set(env_name, str(body[field]).strip())
                saved[field] = True
        try:
            store.save()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"凭据保存失败: {exc}"}, 400)
            return
        self._send_json({"ok": True, "saved": saved,
                         "note": "凭据已保存到本地凭据文件；环境变量仍优先"})

    def _handle_control(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action", "")).strip()
        if action == "pause":
            state_dir = self.server.resolve_state_dir()
            if state_dir is None:
                self._send_json({"ok": False, "error": "配置无效，请先完成设置向导"}, 400)
                return
            pause_file = state_dir / "pause"
            pause_file.parent.mkdir(parents=True, exist_ok=True)
            pause_file.touch()
            self._send_json({"ok": True, "paused": True})
        elif action == "resume":
            state_dir = self.server.resolve_state_dir()
            if state_dir is not None:
                try:
                    (state_dir / "pause").unlink(missing_ok=True)
                except OSError:
                    pass
            self._send_json({"ok": True, "paused": False})
        elif action == "start":
            self._send_json(self.server.start_trading())
        elif action == "stop":
            self._send_json(self.server.stop_trading())
        else:
            self._send_json({"ok": False, "error": f"未知动作: {action}"}, 400)


def _lan_addresses() -> List[str]:
    hosts: List[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        hosts.append(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    if not hosts:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in hosts:
                    hosts.append(ip)
        except OSError:
            pass
    return hosts


def run_console(config_path: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                token: str = "") -> int:
    config_path = config_path.expanduser().resolve()
    token = (token or os.getenv("GUFA_CONSOLE_TOKEN", "")).strip()
    if not token:
        token = secrets.token_urlsafe(6)
    server = ConsoleServer((host, port), ConsoleHandler, config_path, token)
    actual_port = server.server_address[1]
    print(f"\n=== {APP_TITLE} ===")
    print(f"配置: {config_path}")
    print(f"本机访问: http://127.0.0.1:{actual_port}")
    for ip in _lan_addresses():
        print(f"手机/局域网访问: http://{ip}:{actual_port}")
    print(f"访问令牌: {token}")
    print("令牌已随机生成（可用 --token 或环境变量 GUFA_CONSOLE_TOKEN 固定）。")
    print("按 Ctrl+C 停止控制台（不会停止已启动的交易进程）。\n")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n控制台已停止。")
    finally:
        server.server_close()
    return 0


# =============================================================================
# 内嵌控制台页面（单文件，移动端友好，零外部依赖）
# =============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GuFaQuant 控制台</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--line:#26314a;--txt:#e8edf7;--dim:#93a1bd;--ok:#2ecc71;--warn:#f39c12;--bad:#e74c3c;--acc:#5b8cff;--mono:ui-monospace,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:12px;max-width:1000px;margin:0 auto}
h1{font-size:20px;margin:6px 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:4px;word-break:break-all}
.card .v.sm{font-size:14px;font-weight:400}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.badge{padding:4px 12px;border-radius:999px;font-size:13px;border:1px solid var(--line)}
.badge.on{background:rgba(46,204,113,.15);color:var(--ok);border-color:var(--ok)}
.badge.off{background:rgba(148,163,184,.1);color:var(--dim)}
.badge.warn{background:rgba(243,156,18,.15);color:var(--warn);border-color:var(--warn)}
.badge.bad{background:rgba(231,76,60,.15);color:var(--bad);border-color:var(--bad)}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
button{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:10px 18px;font-size:15px;cursor:pointer;transition:.15s}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{background:rgba(231,76,60,.2);border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:14px;flex-wrap:wrap}
.tab{padding:8px 14px;cursor:pointer;color:var(--dim);border-radius:8px 8px 0 0;font-size:14px}
.tab.active{color:var(--txt);background:var(--card);border:1px solid var(--line);border-bottom-color:var(--card)}
.panel{display:none}
.panel.active{display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);word-break:break-all}
th{color:var(--dim);font-weight:500}
pre{background:#0a0e18;border:1px solid var(--line);border-radius:10px;padding:12px;font-family:var(--mono);font-size:12px;overflow:auto;max-height:420px;white-space:pre-wrap;word-break:break-all}
.form-row{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.form-row label{font-size:13px;color:var(--dim)}
input,select{background:#0a0e18;border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:10px;font-size:15px;width:100%}
input[type=checkbox]{width:auto}
.check{flex-direction:row;align-items:center;gap:8px}
.check label{color:var(--txt)}
.mono{font-family:var(--mono)}
.hint{color:var(--dim);font-size:12px;margin-top:2px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#0a0e18;border:1px solid var(--acc);color:var(--txt);padding:10px 18px;border-radius:10px;display:none;z-index:99;max-width:90vw;font-size:14px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:100}
.modal.active{display:flex}
.modal-box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;width:min(90vw,380px)}
.svg-wrap{overflow:auto}
</style>
</head>
<body>
<div id="loginModal" class="modal active">
  <div class="modal-box">
    <h2 style="font-size:17px;margin-bottom:12px">访问令牌</h2>
    <p class="hint" style="margin-bottom:12px">令牌显示在控制台启动时的终端输出里（已随机生成，也可用 --token 固定）。</p>
    <input id="tokenInput" type="password" placeholder="粘贴访问令牌" autocomplete="off">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="primary" style="flex:1" onclick="saveToken()">进入</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<h1>🦐 GuFaQuant 控制台</h1>
<div class="sub" id="sub">连接中…</div>

<div class="badges" id="badges"></div>

<div class="actions">
  <button id="btnStart" class="primary" onclick="act('start')">▶ 启动交易</button>
  <button id="btnStop" class="danger" onclick="act('stop')">■ 停止交易</button>
  <button id="btnPause" onclick="act('pause')">⏸ 暂停新开仓</button>
  <button id="btnResume" onclick="act('resume')">▶ 恢复</button>
  <button onclick="validate()">🔍 立即校验</button>
</div>

<div class="cards" id="cards"></div>

<div class="tabs">
  <div class="tab active" data-panel="overview" onclick="switchTab('overview')">总览</div>
  <div class="tab" data-panel="equity" onclick="switchTab('equity')">权益曲线</div>
  <div class="tab" data-panel="positions" onclick="switchTab('positions')">持仓</div>
  <div class="tab" data-panel="trades" onclick="switchTab('trades')">成交</div>
  <div class="tab" data-panel="log" onclick="switchTab('log')">日志</div>
  <div class="tab" data-panel="setup" onclick="switchTab('setup')">设置向导</div>
</div>

<div class="panel active" id="panel-overview">
  <div id="validateOut" style="display:none;margin-bottom:12px"><pre id="validatePre"></pre></div>
</div>
<div class="panel" id="panel-equity"><div class="svg-wrap" id="equityChart"></div></div>
<div class="panel" id="panel-positions"><table id="posTable"><thead><tr><th>交易对</th><th>数量</th><th>均价</th><th>状态</th></tr></thead><tbody></tbody></table></div>
<div class="panel" id="panel-trades"><table id="tradeTable"><thead><tr><th>时间</th><th>事件</th><th>交易对</th><th>说明</th></tr></thead><tbody></tbody></table></div>
<div class="panel" id="panel-log"><pre id="logPre">加载中…</pre></div>
<div class="panel" id="panel-setup">
  <div class="tabs" style="border-bottom:none">
    <div class="tab active" data-step="1" onclick="switchStep(1)">1 交易所</div>
    <div class="tab" data-step="2" onclick="switchStep(2)">2 AI 断卦师</div>
    <div class="tab" data-step="3" onclick="switchStep(3)">3 运行设置</div>
  </div>

  <div class="step" id="step-1">
    <div class="form-row check"><input type="checkbox" id="f_sandbox" checked><label for="f_sandbox">模拟盘（Sandbox/Testnet）— 强烈建议首次使用保持开启</label></div>
    <div class="form-row"><label>交易所 CCXT ID</label><input id="f_ex_id" value="okx"></div>
    <div class="form-row"><label>API Key</label><input id="f_ex_key" autocomplete="off" placeholder="留空则保留已保存值"></div>
    <div class="form-row"><label>API Secret</label><input id="f_ex_secret" type="password" autocomplete="off" placeholder="留空则保留已保存值"></div>
    <div class="form-row"><label>Passphrase（OKX 需要）</label><input id="f_ex_pass" type="password" autocomplete="off" placeholder="留空则保留已保存值"></div>
    <div class="form-row"><label>代理 proxy_url（国内访问 OKX 需要，如 http://127.0.0.1:7890）</label><input id="f_proxy" placeholder="http://127.0.0.1:7890，不需要可留空"></div>
    <button class="primary" onclick="saveStep1()">保存交易所设置</button>
  </div>

  <div class="step" id="step-2" style="display:none">
    <div class="form-row check"><input type="checkbox" id="f_ai_enabled" checked><label for="f_ai_enabled">启用 AI 十项古法解读</label></div>
    <div class="form-row"><label>中转站 Base URL（OpenAI 兼容）</label><input id="f_ai_url" placeholder="https://tokenrhythm.studio/v1"></div>
    <div class="form-row"><label>模型 ID</label><input id="f_ai_model" placeholder="qwen3.7-max"></div>
    <div class="form-row"><label>AI API Key</label><input id="f_ai_key" type="password" autocomplete="off" placeholder="留空则保留已保存值"></div>
    <div class="form-row"><label>超时（秒）</label><input id="f_ai_timeout" type="number" value="120"></div>
    <button class="primary" onclick="saveStep2()">保存 AI 设置</button>
  </div>

  <div class="step" id="step-3" style="display:none">
    <div class="form-row"><label>交易对（逗号分隔）</label><input id="f_symbols" value="BTC/USDT,ETH/USDT,SOL/USDT"></div>
    <div class="form-row"><label>K 线周期</label><select id="f_tf"><option>1h</option><option>4h</option><option>1d</option></select></div>
    <div class="form-row"><label>轮询间隔（秒）</label><input id="f_poll" type="number" value="60"></div>
    <div class="form-row"><label>Webhook 通知 URL（可选）</label><input id="f_webhook" placeholder="留空禁用"></div>
    <button class="primary" onclick="saveStep3()">保存运行设置</button>
  </div>
  <p class="hint" style="margin-top:10px">保存后点击上方「🔍 立即校验」确认能连接交易所，再点「▶ 启动交易」。</p>
</div>

<script>
let token = localStorage.getItem('token') || '';
function api(path, opts={}){
  const headers = {'Content-Type':'application/json'};
  if(token) headers['Authorization'] = 'Bearer '+token;
  return fetch(path, {...opts, headers}).then(async r=>{
    if(r.status===401){ showLogin(); throw new Error('未授权'); }
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || ('HTTP '+r.status));
    return j;
  });
}
function toast(msg, ms=2600){ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; clearTimeout(t._h); t._h=setTimeout(()=>t.style.display='none', ms); }
function showLogin(){ document.getElementById('loginModal').classList.add('active'); }
function saveToken(){ token=document.getElementById('tokenInput').value.trim(); if(!token) return; localStorage.setItem('token', token); document.getElementById('loginModal').classList.remove('active'); refresh(); }
function switchTab(name){ document.querySelectorAll('.tab[data-panel]').forEach(t=>t.classList.toggle('active', t.dataset.panel===name)); document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active', p.id==='panel-'+name)); if(name==='equity') loadEquity(); if(name==='log') loadLog(); if(name==='trades') loadTrades(); if(name==='positions') loadState(); if(name==='setup') loadConfig(); }
function switchStep(n){ document.querySelectorAll('.tab[data-step]').forEach(t=>t.classList.toggle('active', +t.dataset.step===n)); for(let i=1;i<=3;i++) document.getElementById('step-'+i).style.display = i===n?'':'none'; }
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtNum(v, d=2){ if(v==null||isNaN(v)) return '-'; return Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d}); }
function fmtTime(ts){ if(!ts) return '-'; const d=new Date(ts); return isNaN(d)?ts:d.toLocaleString('zh-CN',{hour12:false}); }

function refresh(){
  api('/api/status').then(s=>{
    document.getElementById('sub').textContent = '配置: ' + (s.config_exists?'已生成':'未生成') + (s.config_valid?' · 有效':' · 待完善');
    const b=[];
    b.push(`<span class="badge ${s.managed.running?'on':'off'}">交易进程 ${s.managed.running?'运行中':'未运行'}</span>`);
    if(s.exchange) b.push(`<span class="badge ${s.exchange.sandbox?'warn':'bad'}">${s.exchange.sandbox?'模拟盘':'正式盘'}</span>`);
    b.push(`<span class="badge">${s.exchange?s.exchange.id.toUpperCase():'未配置'}</span>`);
    if(s.paused) b.push('<span class="badge warn">已暂停新开仓</span>');
    if(s.halted_reason) b.push(`<span class="badge bad">已熔断: ${esc(s.halted_reason)}</span>`);
    document.getElementById('badges').innerHTML = b.join('');
    const c=[];
    c.push(card('账户权益', fmtNum(s.last_equity)));
    c.push(card('峰值权益', fmtNum(s.peak_equity)));
    c.push(card('今日成交', s.trades_today||0));
    c.push(card('最近周期', fmtTime(s.last_cycle_at)));
    if(s.exchange) c.push(card('代理', s.exchange.proxy_set?esc(s.exchange.proxy_url):'未设置'));
    if(s.ai) c.push(card('AI 模型', s.ai.enabled?esc(s.ai.model):'未启用'));
    if(s.credentials) c.push(card('凭据', (s.credentials.exchange_key&&s.credentials.exchange_secret?'✓ 交易所':'✗ 交易所')+' '+(s.credentials.ai_key?'✓ AI':'✗ AI')));
    document.getElementById('cards').innerHTML = c.join('');
    document.getElementById('btnStart').disabled = s.managed.running;
    document.getElementById('btnStop').disabled = !s.managed.running;
    document.getElementById('btnPause').disabled = s.paused;
    document.getElementById('btnResume').disabled = !s.paused;
  }).catch(e=>{});
}
function card(k,v){ return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`; }

function act(action){
  const confirmMap={start:'确定启动交易进程？将开始自动交易。',stop:'确定停止交易进程？',pause:'确定暂停新开仓？存量仓位仍受管理。',resume:'确定恢复自动交易？'};
  if(!confirm(confirmMap[action])) return;
  api('/api/control',{method:'POST',body:JSON.stringify({action})}).then(r=>{ toast(r.ok?(r.error||'成功'):r.error); refresh(); }).catch(e=>toast(e.message));
}
function validate(){
  document.getElementById('validateOut').style.display='block';
  document.getElementById('validatePre').textContent='校验中…（最多 240 秒）';
  api('/api/validate',{method:'POST',body:'{}'}).then(r=>{
    document.getElementById('validatePre').textContent = r.output || r.error || (r.ok?'校验通过':'校验失败');
  }).catch(e=>document.getElementById('validatePre').textContent='失败: '+e.message);
}

function loadEquity(){
  api('/api/equity').then(rows=>{
    const wrap=document.getElementById('equityChart');
    if(!rows.length){ wrap.innerHTML='<p class="hint">暂无权益数据（运行过至少一个周期后出现）</p>'; return; }
    const w=Math.max(680, rows.length*6), h=260, pad=34;
    const vals=rows.map(r=>Number(r.equity)||0);
    const min=Math.min(...vals), max=Math.max(...vals), span=(max-min)||1;
    const pts=vals.map((v,i)=>`${pad+i*(w-pad*2)/Math.max(1,vals.length-1)},${h-pad-(v-min)/span*(h-pad*2)}`).join(' ');
    const last=vals[vals.length-1];
    wrap.innerHTML=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="min-width:100%">
      <line x1="${pad}" y1="${h-pad}" x2="${w-pad}" y2="${h-pad}" stroke="#26314a"/>
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${h-pad}" stroke="#26314a"/>
      <text x="6" y="${pad+4}" fill="#93a1bd" font-size="11">${fmtNum(max)}</text>
      <text x="6" y="${h-pad}" fill="#93a1bd" font-size="11">${fmtNum(min)}</text>
      <text x="${pad}" y="${h-8}" fill="#93a1bd" font-size="11">${fmtTime(rows[0].ts)}</text>
      <text x="${w-pad*3}" y="${h-8}" fill="#93a1bd" font-size="11">${fmtTime(rows[rows.length-1].ts)}</text>
      <polyline points="${pts}" fill="none" stroke="#5b8cff" stroke-width="2"/>
      <circle cx="${pad+(vals.length-1)*(w-pad*2)/Math.max(1,vals.length-1)}" cy="${h-pad-(last-min)/span*(h-pad*2)}" r="4" fill="#2ecc71"/>
    </svg><p class="hint">最新权益: ${fmtNum(last)}（共 ${rows.length} 个周期）</p>`;
  }).catch(()=>{});
}
function loadState(){
  api('/api/status').then(s=>{
    const tbody=document.querySelector('#posTable tbody'); tbody.innerHTML='';
    const pos=s.positions||{};
    const keys=Object.keys(pos);
    if(!keys.length){ tbody.innerHTML='<tr><td colspan="4">暂无持仓</td></tr>'; return; }
    for(const k of keys){ const p=pos[k]; tbody.innerHTML+=`<tr><td>${esc(k)}</td><td>${fmtNum(p.amount,6)}</td><td>${fmtNum(p.avg_price||p.price)}</td><td>${esc(p.status||'')}</td></tr>`; }
  }).catch(()=>{});
}
function loadTrades(){
  api('/api/audit').then(rows=>{
    const tbody=document.querySelector('#tradeTable tbody'); tbody.innerHTML='';
    if(!rows.length){ tbody.innerHTML='<tr><td colspan="4">暂无成交记录</td></tr>'; return; }
    rows.slice().reverse().slice(0,100).forEach(r=>{
      const plan=r.plan||{}; const fill=r.fill||{};
      const sym=plan.symbol||fill.symbol||'-';
      const desc=plan.side?`${esc(plan.side)} ${fmtNum(plan.amount,4)} @ ${fmtNum(plan.price)}`:(fill.price?`@ ${fmtNum(fill.price)}`:'');
      tbody.innerHTML+=`<tr><td>${fmtTime(r.ts)}</td><td>${esc(r.event)}</td><td>${esc(sym)}</td><td>${desc}</td></tr>`;
    });
  }).catch(()=>{});
}
function loadLog(){
  api('/api/log?lines=300').then(rows=>{
    document.getElementById('logPre').textContent = rows.map(r=>`[${r.ts||''}] ${r.level||''} ${r.message||''}`).slice(-300).join('\n')||'暂无日志';
  }).catch(e=>document.getElementById('logPre').textContent='失败: '+e.message);
}

function loadConfig(){
  api('/api/config').then(c=>{
    if(!c.exists) return;
    document.getElementById('f_sandbox').checked = c.exchange.sandbox;
    document.getElementById('f_ex_id').value = c.exchange.id;
    document.getElementById('f_proxy').value = c.exchange.proxy_url;
    document.getElementById('f_ai_enabled').checked = c.ai.enabled;
    document.getElementById('f_ai_url').value = c.ai.base_url||'';
    document.getElementById('f_ai_model').value = c.ai.model||'';
    document.getElementById('f_ai_timeout').value = c.ai.timeout_seconds;
    document.getElementById('f_symbols').value = c.runtime.symbols.join(',');
    document.getElementById('f_tf').value = c.runtime.timeframe;
    document.getElementById('f_poll').value = c.runtime.poll_interval_seconds;
    document.getElementById('f_webhook').value = c.runtime.webhook_url||'';
  }).catch(()=>{});
}
function saveStep1(){
  const cred={}; if(document.getElementById('f_ex_key').value.trim()) cred.exchange_api_key=document.getElementById('f_ex_key').value.trim();
  if(document.getElementById('f_ex_secret').value.trim()) cred.exchange_secret=document.getElementById('f_ex_secret').value.trim();
  if(document.getElementById('f_ex_pass').value.trim()) cred.exchange_passphrase=document.getElementById('f_ex_pass').value.trim();
  const cfg={exchange:{id:document.getElementById('f_ex_id').value.trim()||'okx', sandbox:document.getElementById('f_sandbox').checked, proxy_url:document.getElementById('f_proxy').value.trim()}};
  Promise.all([
    api('/api/config',{method:'POST',body:JSON.stringify(cfg)}),
    Object.keys(cred).length?api('/api/credentials',{method:'POST',body:JSON.stringify(cred)}):Promise.resolve({ok:true})
  ]).then(()=>{ toast('交易所设置已保存'); refresh(); }).catch(e=>toast(e.message));
}
function saveStep2(){
  const cred={}; if(document.getElementById('f_ai_key').value.trim()) cred.ai_api_key=document.getElementById('f_ai_key').value.trim();
  const cfg={ai:{enabled:document.getElementById('f_ai_enabled').checked, base_url:document.getElementById('f_ai_url').value.trim(), model:document.getElementById('f_ai_model').value.trim(), timeout_seconds:Number(document.getElementById('f_ai_timeout').value)||120}};
  Promise.all([
    api('/api/config',{method:'POST',body:JSON.stringify(cfg)}),
    Object.keys(cred).length?api('/api/credentials',{method:'POST',body:JSON.stringify(cred)}):Promise.resolve({ok:true})
  ]).then(()=>{ toast('AI 设置已保存'); refresh(); }).catch(e=>toast(e.message));
}
function saveStep3(){
  const symbols=document.getElementById('f_symbols').value.split(',').map(s=>s.trim()).filter(Boolean);
  const cfg={runtime:{symbols, timeframe:document.getElementById('f_tf').value, poll_interval_seconds:Number(document.getElementById('f_poll').value)||60, webhook_url:document.getElementById('f_webhook').value.trim()}};
  api('/api/config',{method:'POST',body:JSON.stringify(cfg)}).then(()=>{ toast('运行设置已保存'); refresh(); }).catch(e=>toast(e.message));
}

if(localStorage.getItem('token')){ document.getElementById('loginModal').classList.remove('active'); }
refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(run_console(Path("config.json")))
