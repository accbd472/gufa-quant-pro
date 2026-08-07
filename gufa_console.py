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
import shutil
import socket
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error
from copy import deepcopy
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
                 "retry_base_seconds", "recv_window_ms", "client_order_id_param",
                 "proxy_url", "proxy_list"],
    "runtime": ["symbols", "quote_currency", "timeframe", "ohlcv_limit",
                "poll_interval_seconds", "closed_candle_only", "max_candle_lag_seconds",
                "log_level", "log_max_bytes", "log_backup_count", "webhook_url"],
    "ai": ["enabled", "model", "base_url", "api_key_name", "timeout_seconds", "minimum_allow_confidence",
           "decision_mode", "fail_closed", "max_output_tokens", "split_readings", "reasoning_effort"],
}

# OKX 公开现货合约列表（用于自助选择交易标的；经代理拉取，失败则回退内置列表）
_OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"

# 内置主流币候选池（OKX 现货，计价币 USDT；网络不可用时仍可自助选择）
_BUILTIN_SPOT_SYMBOLS: List[str] = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "BNB/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "TON/USDT", "TRX/USDT", "DOT/USDT",
    "LTC/USDT", "BCH/USDT", "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT",
    "SUI/USDT", "PEPE/USDT", "SHIB/USDT", "UNI/USDT", "ATOM/USDT", "FIL/USDT",
    "XLM/USDT", "ICP/USDT", "HBAR/USDT", "INJ/USDT", "SEI/USDT", "TIA/USDT",
    "WIF/USDT", "ORDI/USDT", "RNDR/USDT", "ETC/USDT", "POL/USDT",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_okx_candidates(proxy_url: str, quote: str) -> Optional[List[str]]:
    """经代理拉取 OKX 现货合约列表（只读公开接口）。失败返回 None，调用方回退内置池。"""
    try:
        if proxy_url:
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        request = urllib.request.Request(
            _OKX_INSTRUMENTS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GuFaQuant/8.1"},
        )
        with opener.open(request, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        rows = payload.get("data") or []
        symbols: List[str] = []
        for row in rows:
            if row.get("instType") != "SPOT" or row.get("state") != "live":
                continue
            inst = str(row.get("instId") or "")
            if "-" not in inst:
                continue
            base, contract_quote = inst.rsplit("-", 1)
            if contract_quote.upper() == quote.upper():
                symbols.append(f"{base.upper()}/{quote.upper()}")
        seen: set = set()
        deduped = [s for s in symbols if not (s in seen or seen.add(s))]
        return deduped[:80] or None
    except Exception:
        return None


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
        # 自动守护：交易进程意外退出时自动拉起（订单状态不确定除外），
        # 1 小时窗口内连续自动重启超过 3 次则熔断，等待人工处理。
        self.auto_restart = {
            "enabled": True,
            "window_started": 0.0,
            "count": 0,
            "last_restart_at": "",
            "disabled_reason": "",
        }
        self._restore_managed()
        self._supervisor = threading.Thread(
            target=self._supervise_loop, daemon=True, name="console-supervisor"
        )
        self._supervisor.start()

    # ---- 自动守护 ----
    def _supervise_loop(self) -> None:
        while True:
            try:
                self._supervise_once()
            except Exception:  # noqa: BLE001 守护失败不能拖垮控制台
                pass
            time.sleep(10)

    def _supervise_once(self) -> None:
        if not self.auto_restart["enabled"]:
            return
        state_dir = self.resolve_state_dir()
        if state_dir is None:
            return
        pid_file = state_dir / "console.pid.json"
        data = _read_json(pid_file, {})
        pid = int(data.get("pid", 0) or 0)
        if pid <= 0:
            return  # 用户从未启动交易，或已主动停止（stop 会清 pid 文件）
        if _is_alive(pid):
            self.auto_restart["count"] = 0  # 进程稳定运行，重置窗口计数
            return
        # 进程已死：检查是否订单状态不确定（必须人工，禁止自动拉起）
        health = _read_json(state_dir / "health.json", {})
        if str(health.get("status")) == "order_uncertain":
            self.auto_restart["disabled_reason"] = "订单状态不确定，需人工核对后再启动"
            self.auto_restart["enabled"] = False
            return
        now = time.time()
        if self.auto_restart["count"] == 0:
            self.auto_restart["window_started"] = now
        elif now - self.auto_restart["window_started"] > 3600:
            self.auto_restart["window_started"] = now
            self.auto_restart["count"] = 0
        if self.auto_restart["count"] >= 3:
            self.auto_restart["disabled_reason"] = "1 小时内连续自动重启超限，已熔断，请人工检查"
            self.auto_restart["enabled"] = False
            return
        result = self.start_trading()
        if result.get("ok"):
            self.auto_restart["count"] += 1
            self.auto_restart["last_restart_at"] = utc_now()

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

    def credentials_list(self) -> Dict[str, Any]:
        """返回凭据列表（仅 Key 名称，不暴露值）。"""
        store = self.credentials_store()
        return {
            "ai_key_names": store.list_ai_key_names(),
            "exchange_key_set": bool(store.stored("GUFA_API_KEY")),
            "exchange_secret_set": bool(store.stored("GUFA_API_SECRET")),
        }

    # ---- 自助选择交易标的 ----
    def symbols_candidates(self) -> Dict[str, Any]:
        """返回可自助选择的交易对：优先从 OKX 实时拉取，失败回退内置池。"""
        cfg = self.resolve_config()
        quote = (cfg.runtime.quote_currency if cfg else "USDT").strip().upper() or "USDT"
        proxy_url = (cfg.exchange.proxy_url.strip() if cfg else "") or ""
        fetched = _fetch_okx_candidates(proxy_url, quote)
        if fetched:
            return {"source": "okx", "quote": quote, "symbols": fetched,
                    "builtin": _BUILTIN_SPOT_SYMBOLS}
        return {"source": "builtin", "quote": quote, "symbols": list(_BUILTIN_SPOT_SYMBOLS),
                "builtin": list(_BUILTIN_SPOT_SYMBOLS)}

    def save_symbols(self, body: Dict[str, Any]) -> tuple:
        """保存交易标的；检测旧状态绑定并在确认后备份重置。返回 (status, payload)。"""
        raw = body.get("symbols")
        if not isinstance(raw, list) or not raw:
            return 400, {"ok": False, "error": "请至少选择一个交易对"}
        symbols = [str(s).strip().upper() for s in raw if str(s).strip()]
        if not symbols:
            return 400, {"ok": False, "error": "请至少选择一个交易对"}
        if len(symbols) != len(set(symbols)):
            return 400, {"ok": False, "error": "交易对存在重复项"}
        for symbol in symbols:
            if "/" not in symbol:
                return 400, {"ok": False, "error": f"交易对格式应为 币种/计价币，如 BTC/USDT：{symbol}"}
            base, quote = symbol.split("/", 1)
            if not base or not quote or base == quote:
                return 400, {"ok": False, "error": f"交易对无效：{symbol}"}
        if self.managed.running:
            return 409, {"ok": False, "error": "交易进程正在运行，请先点击「■ 停止交易」再更换标的"}

        if not self.config_path.exists():
            g.atomic_write_json(self.config_path, g.default_config_dict(), mode=0o644)
        try:
            payload = g.load_json(self.config_path)
        except Exception as exc:  # noqa: BLE001
            return 400, {"ok": False, "error": f"现有配置无法读取: {exc}"}
        old_payload = deepcopy(payload)
        payload.setdefault("runtime", {})["symbols"] = symbols
        try:
            g.atomic_write_json(self.config_path, payload, mode=0o644)
            new_cfg = g.AppConfig.load(self.config_path)
        except g.ConfigError as exc:
            g.atomic_write_json(self.config_path, old_payload, mode=0o644)
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            g.atomic_write_json(self.config_path, old_payload, mode=0o644)
            return 400, {"ok": False, "error": f"保存失败: {exc}"}

        # 状态绑定检查：symbols 计入 profile 指纹，更换标的后旧 state.json 会拒绝启动
        state_dir = self.resolve_state_dir()
        needs_reset = False
        if state_dir is not None:
            state = _read_json(state_dir / "state.json", None)
            if isinstance(state, dict):
                old_profile = str(state.get("profile_id") or "")
                if old_profile:
                    store = self.credentials_store()
                    account_key = (
                        os.getenv(new_cfg.exchange.api_key_env, "").strip()
                        or store.stored(new_cfg.exchange.api_key_env)
                    )
                    new_profile = g.build_profile_id(new_cfg, account_key)
                    needs_reset = old_profile != new_profile
        if needs_reset and not body.get("reset_state"):
            g.atomic_write_json(self.config_path, old_payload, mode=0o644)
            return 200, {
                "ok": False,
                "needs_reset": True,
                "error": "更换标的后，旧交易状态与新的交易对不匹配，需要重置状态（旧状态会先备份）。请确认后重试。",
            }
        backup = ""
        if needs_reset and state_dir is not None:
            backup = self._backup_state_files(state_dir)
        note = "已保存；请点击「▶ 启动交易」开始自动交易"
        if needs_reset:
            note = f"已保存并重置交易状态；旧状态已备份到 {backup}，请点击「▶ 启动交易」"
        return 200, {"ok": True, "reset": needs_reset, "backup": backup, "note": note}

    def _backup_state_files(self, state_dir: Path) -> str:
        """把旧状态文件复制到 state_backup_<时间戳>/ 并移除原件（换标的后重新开始）。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = state_dir / f"state_backup_{stamp}"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        for name in ("state.json", "health.json", "equity.jsonl", "orders.audit.jsonl", "pause"):
            source = state_dir / name
            if not source.exists():
                continue
            try:
                shutil.copy2(source, backup_dir / name)
                source.unlink()
            except OSError:
                pass
        return str(backup_dir)

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
                    cmd,
                    # 始终以项目目录为工作目录，避免控制台从别处启动时
                    # 交易进程把相对路径的 runtime 状态写到错误位置
                    cwd=str(Path(__file__).resolve().parent),
                    stdout=log_handle, stderr=subprocess.STDOUT,
                    env=os.environ.copy(), **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                log_handle.close()
                return {"ok": False, "error": f"启动失败: {exc}"}
            self.managed.proc = proc
            self.managed.pid = proc.pid
            self.managed.started_at = utc_now()
            self.managed.cmd = cmd
            # 用户手动启动成功 → 重新武装自动守护
            self.auto_restart["enabled"] = True
            self.auto_restart["count"] = 0
            self.auto_restart["disabled_reason"] = ""
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
            "auto_restart": {
                "enabled": bool(self.auto_restart["enabled"]),
                "count": int(self.auto_restart["count"]),
                "last_restart_at": str(self.auto_restart["last_restart_at"]),
                "disabled_reason": str(self.auto_restart["disabled_reason"]),
            },
        }
        if cfg is not None:
            payload["exchange"] = {
                "id": cfg.exchange.id,
                "sandbox": cfg.exchange.sandbox,
                "market_type": cfg.exchange.market_type,
                "proxy_url": cfg.exchange.proxy_url,
                "proxy_list": list(cfg.exchange.proxy_list),
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
        elif path == "/api/symbols/candidates":
            self._send_json(self.server.symbols_candidates())
        elif path == "/api/credentials":
            self._send_json(self.server.credentials_list())
        elif path == "/api/ai/models":
            self._handle_ai_models()
        elif path == "/api/ai/test":
            self._handle_ai_test()
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
        elif path == "/api/symbols":
            status, payload = self.server.save_symbols(body)
            self._send_json(payload, status)
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
                "api_key_name": cfg.ai.api_key_name,
                "timeout_seconds": cfg.ai.timeout_seconds,
                "minimum_allow_confidence": cfg.ai.minimum_allow_confidence,
                "decision_mode": cfg.ai.decision_mode, "fail_closed": cfg.ai.fail_closed,
                "max_output_tokens": cfg.ai.max_output_tokens,
                "split_readings": cfg.ai.split_readings,
                "reasoning_effort": cfg.ai.reasoning_effort,
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
        # 命名 AI Key（多 Key 档案）
        if "ai_key_name" in body and "ai_key_value" in body:
            name = str(body["ai_key_name"]).strip()
            value = str(body["ai_key_value"]).strip()
            if not name:
                self._send_json({"ok": False, "error": "AI Key 名称不能为空"}, 400)
                return
            if not value:
                self._send_json({"ok": False, "error": "AI Key 值不能为空"}, 400)
                return
            try:
                store.set_ai_key(name, value)
                store.save()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"保存失败: {exc}"}, 400)
                return
            self._send_json({"ok": True, "saved": True, "ai_key_names": store.list_ai_key_names(),
                             "note": f"AI Key「{name}」已保存"})
            return
        if "ai_key_delete" in body:
            name = str(body["ai_key_delete"]).strip()
            try:
                store.delete_ai_key(name)
                store.save()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"删除失败: {exc}"}, 400)
                return
            self._send_json({"ok": True, "deleted": name, "ai_key_names": store.list_ai_key_names(),
                             "note": f"AI Key「{name}」已删除"})
            return
        # 旧式凭据
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

    def _handle_ai_models(self) -> None:
        """GET /api/ai/models：用当前配置的 base_url + key 拉取模型列表。"""
        cfg = self.server.resolve_config()
        if cfg is None or not cfg.ai.enabled:
            self._send_json({"ok": False, "error": "AI 未启用或配置无效"}, 400)
            return
        base_url = cfg.ai.base_url.strip()
        if not base_url:
            self._send_json({"ok": False, "error": "请先保存 Base URL"}, 400)
            return
        # 解析 Key：优先命名 Key，其次旧式凭据
        store = self.server.credentials_store()
        name = cfg.ai.api_key_name.strip()
        try:
            if name:
                key = store.resolve_ai_key(name, required=True)
            else:
                key = store.resolve(cfg.ai.api_key_env, required=True, purpose="AI API")
        except g.ConfigError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        url = base_url.rstrip("/") + "/models"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            # 走代理（如果配置了）
            proxy_url = (cfg.exchange.proxy_url or "").strip()
            if proxy_url:
                handler = urllib.request.ProxyHandler({
                    "http": proxy_url, "https": proxy_url,
                })
                opener = urllib.request.build_opener(handler)
                resp = opener.open(req, timeout=15)
            else:
                resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
            models.sort()
            self._send_json({"ok": True, "models": models, "total": len(models)})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "API 返回非 JSON 响应，请检查 Base URL 是否正确", "models": []}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"拉取模型列表失败: {exc}", "models": []}, 400)

    def _handle_ai_test(self) -> None:
        """GET /api/ai/test：用当前配置发一条 chat/completions 探测连通性。"""
        cfg = self.server.resolve_config()
        if cfg is None or not cfg.ai.enabled:
            self._send_json({"ok": False, "error": "AI 未启用或配置无效"}, 400)
            return
        base_url = cfg.ai.base_url.strip()
        if not base_url:
            self._send_json({"ok": False, "error": "请先保存 Base URL"}, 400)
            return
        store = self.server.credentials_store()
        name = cfg.ai.api_key_name.strip()
        try:
            key = (store.resolve_ai_key(name, required=True) if name
                   else store.resolve(cfg.ai.api_key_env, required=True, purpose="AI API"))
        except g.ConfigError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        url = base_url.rstrip("/") + "/chat/completions"
        payload = json.dumps({
            "model": cfg.ai.model or "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        try:
            proxy_url = (cfg.exchange.proxy_url or "").strip()
            if proxy_url:
                handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(handler)
                resp = opener.open(req, timeout=15)
            else:
                resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8"))
            model_used = data.get("model", "?")
            content = ""
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
            self._send_json({"ok": True, "model": model_used,
                             "reply": content[:200],
                             "note": f"✅ 连接成功，模型 {model_used} 响应正常"})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "API 返回非 JSON 响应，请检查 Base URL"}, 400)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            self._send_json({"ok": False, "error": f"HTTP {exc.code}: {body}"}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"连接失败: {exc}"}, 400)

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
.chip{display:inline-block;padding:7px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--txt);font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
#candGrid{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px;max-height:220px;overflow:auto;padding:2px}
#symSearch{font-size:14px}
.sel-list{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 8px}
.sel-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.4);font-size:13px}
.sel-chip b{cursor:pointer;color:var(--dim);font-weight:700}
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
<div class="sub" id="sub">连接中… · <a href="http://127.0.0.1:8601" target="_blank" style="color:var(--acc);text-decoration:none;border-bottom:1px dashed var(--acc)">🛸 打开科幻实时大屏 (8601)</a></div>

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
  <p class="hint" style="margin-bottom:10px">当前支持 <strong>OKX 模拟盘/正式盘现货（虚拟货币）</strong>。股票行情源暂未接入，后续可扩展。第 3 步可<strong>自助选择要交易的币种</strong>，保存后点「▶ 启动交易」即可自己运行。</p>
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
    <div class="form-row"><label>代理 proxy_url（首选，国内访问 OKX 需要，如 http://127.0.0.1:7890）</label><input id="f_proxy" placeholder="http://127.0.0.1:7890，不需要可留空"></div>
    <div class="form-row"><label>代理池 proxy_list（自动切换：逗号分隔多个代理，当前失效自动换下一个）</label><input id="f_proxy_list" placeholder="http://127.0.0.1:7890,http://127.0.0.1:7891,http://127.0.0.1:7892"></div>
    <button class="primary" onclick="saveStep1()">保存交易所设置</button>
  </div>

  <div class="step" id="step-2" style="display:none">
    <div class="form-row check"><input type="checkbox" id="f_ai_enabled" checked><label for="f_ai_enabled">启用 AI 十项古法解读</label></div>
    <div class="form-row"><label>中转站 Base URL（OpenAI 兼容）</label><input id="f_ai_url" placeholder="https://tokenrhythm.studio/v1"></div>
    <div class="form-row"><label>AI Key 档案（多 Key 自主切换）</label>
      <select id="f_ai_key_name" onchange="onKeyChange()"><option value="">— 旧式凭据（环境变量） —</option></select>
    </div>
    <div class="form-row" style="display:flex;gap:8px;align-items:flex-end">
      <div style="flex:1"><label style="font-size:11px">新 Key 名称</label><input id="f_ai_key_name_new" placeholder="如：tokenrhythm-主号"></div>
      <div style="flex:2"><label style="font-size:11px">新 Key 值</label><input id="f_ai_key_value_new" type="password" autocomplete="off" placeholder="sk-..."></div>
      <button onclick="saveNamedKey()" style="white-space:nowrap;margin-bottom:0">➕ 添加</button>
    </div>
    <div class="form-row" id="rowDelKey" style="display:none">
      <button class="danger" onclick="deleteNamedKey()" style="width:100%">🗑 删除当前选中的 Key 档案</button>
    </div>
    <div class="form-row"><label>模型 ID</label>
      <div style="display:flex;gap:8px">
        <select id="f_ai_model" onchange="onModelChange()" style="flex:1"><option value="">— 加载中… —</option></select>
        <button onclick="loadModels()" style="margin-bottom:0;white-space:nowrap">🔄 刷新</button>
      </div>
      <input id="f_ai_model_custom" style="display:none;margin-top:6px" placeholder="手动输入模型 ID，如 qwen3.7-max">
    </div>
    <div class="form-row check"><input type="checkbox" id="f_ai_split"><label for="f_ai_split">拆分模式：十项各发一次小请求再综合（修复 reasoning 模型大请求空响应）</label></div>
    <div class="form-row"><label>思考档位（reasoning 模型，deepseek 系建议 low）</label>
      <select id="f_ai_effort">
        <option value="">自动（不传，默认 medium）</option>
        <option value="low">low（省思考，最不易空响应）</option>
        <option value="medium">medium</option>
        <option value="high">high</option>
        <option value="xhigh">xhigh</option>
        <option value="max">max</option>
      </select>
    </div>
    <button class="primary" onclick="saveStep2()">保存 AI 设置</button>
    <button onclick="testAiConnection()" style="margin-top:8px;width:100%">🔍 测试连接</button>
  </div>

  <div class="step" id="step-3" style="display:none">
    <p class="hint" style="margin-bottom:10px">🎯 选择要交易的<strong>虚拟货币</strong>（OKX 现货）。点击下方币种即可加入/移除；也可在输入框手动输入任意交易对（如 SOL/USDT）。<em>股票行情源暂未接入，后续可扩展。</em></p>
    <div class="form-row"><label>已选交易对（可手动编辑）</label><input id="f_symbols" value="BTC/USDT,ETH/USDT,SOL/USDT" placeholder="BTC/USDT,ETH/USDT"></div>
    <div class="form-row"><label>搜索币种（从交易所实时列表或内置主流币中选）</label><input id="symSearch" placeholder="输入名称过滤，如 BTC / ETH / SOL…" oninput="renderCands()"></div>
    <div id="candGrid"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button onclick="clearSymbols()" style="flex:1">🗑 清空已选</button>
      <button onclick="fillPopular()" style="flex:1">⭐ 填入主流三币</button>
    </div>
    <div class="form-row" style="margin-top:14px"><label>K 线周期</label><select id="f_tf"><option>1h</option><option>4h</option><option>1d</option></select></div>
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
function switchTab(name){ document.querySelectorAll('.tab[data-panel]').forEach(t=>t.classList.toggle('active', t.dataset.panel===name)); document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active', p.id==='panel-'+name)); if(name==='equity') loadEquity(); if(name==='log') loadLog(); if(name==='trades') loadTrades(); if(name==='positions') loadState(); if(name==='setup'){ loadConfig(); loadCandidates(); } }
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
    if(s.auto_restart&&!s.auto_restart.enabled) b.push(`<span class="badge warn">自动守护已熔断: ${esc(s.auto_restart.disabled_reason)}</span>`);
    if(s.auto_restart&&s.auto_restart.count>0) b.push(`<span class="badge">自动守护: ${s.auto_restart.count}次重启</span>`);
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
  // 日志面板激活时同步刷新日志，避免页面停留在旧日志上造成「进程卡住」的错觉
  if(document.querySelector('.tab[data-panel="log"]').classList.contains('active')) loadLog();
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
    document.getElementById('f_proxy_list').value = (c.exchange.proxy_list||[]).join(',');
    document.getElementById('f_ai_enabled').checked = c.ai.enabled;
    document.getElementById('f_ai_split').checked = !!c.ai.split_readings;
    document.getElementById('f_ai_effort').value = c.ai.reasoning_effort||'';
    document.getElementById('f_ai_url').value = c.ai.base_url||'';
    document.getElementById('f_ai_timeout').value = c.ai.timeout_seconds;
    // 模型列表：先设当前值，再异步拉取
    curModel = c.ai.model||'';
    loadModels();
    // Key 档案选择器
    curKeyName = c.ai.api_key_name||'';
    loadKeyNames();
    document.getElementById('f_symbols').value = c.runtime.symbols.join(',');
    document.getElementById('f_tf').value = c.runtime.timeframe;
    document.getElementById('f_poll').value = c.runtime.poll_interval_seconds;
    document.getElementById('f_webhook').value = c.runtime.webhook_url||'';
    renderCands();
  }).catch(()=>{});
}

// ---- 自助选择交易标的 ----
let cands=[], candSource='builtin';
function loadCandidates(){
  api('/api/symbols/candidates').then(c=>{
    cands=c.symbols||[]; candSource=c.source||'builtin';
    document.getElementById('symSearch').placeholder = candSource==='okx'
      ? '已连接 OKX，实时列表（输入名称过滤，如 BTC / ETH / SOL…）'
      : '内置主流币列表（未连接 OKX，输入名称过滤）';
    renderCands();
  }).catch(()=>{});
}
function curSymbols(){ return document.getElementById('f_symbols').value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean); }
function renderCands(){
  const q=document.getElementById('symSearch').value.trim().toUpperCase();
  const sel=new Set(curSymbols());
  const list=cands.filter(s=>!q||s.includes(q));
  document.getElementById('candGrid').innerHTML = list.length
    ? list.map(s=>`<span class="chip ${sel.has(s)?'on':''}" onclick="toggleSymbol('${s}')">${esc(s)}</span>`).join('')
    : '<span class="hint">暂无候选（可直接在上方输入框手动填写交易对）</span>';
}
function toggleSymbol(sym){
  const sel=curSymbols(); const i=sel.indexOf(sym);
  if(i>=0) sel.splice(i,1); else sel.push(sym);
  document.getElementById('f_symbols').value=sel.join(',');
  renderCands();
}
function clearSymbols(){ document.getElementById('f_symbols').value=''; renderCands(); }
function fillPopular(){ document.getElementById('f_symbols').value='BTC/USDT,ETH/USDT,SOL/USDT'; renderCands(); }
function saveStep1(){
  const cred={}; if(document.getElementById('f_ex_key').value.trim()) cred.exchange_api_key=document.getElementById('f_ex_key').value.trim();
  if(document.getElementById('f_ex_secret').value.trim()) cred.exchange_secret=document.getElementById('f_ex_secret').value.trim();
  if(document.getElementById('f_ex_pass').value.trim()) cred.exchange_passphrase=document.getElementById('f_ex_pass').value.trim();
  const cfg={exchange:{id:document.getElementById('f_ex_id').value.trim()||'okx', sandbox:document.getElementById('f_sandbox').checked, proxy_url:document.getElementById('f_proxy').value.trim(), proxy_list:document.getElementById('f_proxy_list').value.split(',').map(s=>s.trim()).filter(Boolean)}};
  Promise.all([
    api('/api/config',{method:'POST',body:JSON.stringify(cfg)}),
    Object.keys(cred).length?api('/api/credentials',{method:'POST',body:JSON.stringify(cred)}):Promise.resolve({ok:true})
  ]).then(()=>{ toast('交易所设置已保存'); refresh(); }).catch(e=>toast(e.message));
}
function saveStep2(){
  const cred={}; if(document.getElementById('f_ai_key').value.trim()) cred.ai_api_key=document.getElementById('f_ai_key').value.trim();
  let model = document.getElementById('f_ai_model').value;
  if(model === '__custom__') model = document.getElementById('f_ai_model_custom').value.trim();
  if(!model){ toast('请选择或输入模型 ID'); return; }
  const cfg={ai:{enabled:document.getElementById('f_ai_enabled').checked, base_url:document.getElementById('f_ai_url').value.trim(), api_key_name:document.getElementById('f_ai_key_name').value, model, timeout_seconds:Number(document.getElementById('f_ai_timeout').value)||120, split_readings:document.getElementById('f_ai_split').checked, reasoning_effort:document.getElementById('f_ai_effort').value}};
  Promise.all([
    api('/api/config',{method:'POST',body:JSON.stringify(cfg)}),
    Object.keys(cred).length?api('/api/credentials',{method:'POST',body:JSON.stringify(cred)}):Promise.resolve({ok:true})
  ]).then(()=>{ toast('AI 设置已保存'); refresh(); }).catch(e=>toast(e.message));
}
function saveStep3(){
  const symbols=curSymbols();
  if(!symbols.length){ toast('请至少选择一个交易对'); return; }
  const cfg={runtime:{symbols, timeframe:document.getElementById('f_tf').value, poll_interval_seconds:Number(document.getElementById('f_poll').value)||60, webhook_url:document.getElementById('f_webhook').value.trim()}};
  const send=(reset_state)=>api('/api/symbols',{method:'POST',body:JSON.stringify({symbols, reset_state})});
  send(false).then(r=>{
    if(r.needs_reset){
      if(!confirm('更换交易标的后，旧交易状态（权益曲线、持仓记录等）与新标的绑定不匹配，需要重置。\n\n旧状态会自动备份到 state_backup_ 文件夹，不会丢失。\n\n确定继续更换吗？')) return null;
      return send(true);
    }
    return r;
  }).then(r=>{
    if(!r) return;
    toast(r.ok?(r.note||'运行设置已保存'):(r.error||'保存失败'));
    if(r.ok) refresh();
  }).catch(e=>toast(e.message));
}

// ---- 多 Key 档案 + 模型列表 ----
let curModel = '', curKeyName = '';

function loadModels(){
  const sel = document.getElementById('f_ai_model');
  document.getElementById('f_ai_model_custom').style.display = 'none';
  sel.innerHTML = '<option value="">— 加载中… —</option>';
  api('/api/ai/models').then(r=>{
    sel.innerHTML = '';
    if(!r.ok){ sel.innerHTML = `<option value="${esc(curModel)}">${esc(curModel)}</option><option value="">⚠ ${esc(r.error)}</option>`; return; }
    r.models.forEach(m=>{ sel.innerHTML += `<option value="${esc(m)}"${m===curModel?' selected':''}>${esc(m)}</option>`; });
    if(curModel && !r.models.includes(curModel)){
      sel.innerHTML = `<option value="${esc(curModel)}" selected>${esc(curModel)} (当前)</option>` + sel.innerHTML;
    }
    sel.innerHTML += '<option value="__custom__">✏ 自定义…</option>';
  }).catch(e=>{
    sel.innerHTML = `<option value="${esc(curModel)}">${esc(curModel)}</option><option value="">⚠ ${esc(e.message)}</option>`;
  });
}

function loadKeyNames(){
  const sel = document.getElementById('f_ai_key_name');
  sel.innerHTML = '<option value="">— 旧式凭据（环境变量） —</option>';
  api('/api/credentials').then(r=>{
    r.ai_key_names.forEach(n=>{ sel.innerHTML += `<option value="${esc(n)}"${n===curKeyName?' selected':''}>${esc(n)}</option>`; });
    onKeyChange();
  }).catch(()=>{});
}

function onKeyChange(){
  const v = document.getElementById('f_ai_key_name').value;
  document.getElementById('rowDelKey').style.display = v ? '' : 'none';
}

function onModelChange(){
  const sel = document.getElementById('f_ai_model');
  const custom = document.getElementById('f_ai_model_custom');
  const isCustom = sel.value === '__custom__';
  custom.style.display = isCustom ? '' : 'none';
  if(isCustom) custom.focus();
}

function saveNamedKey(){
  const name = document.getElementById('f_ai_key_name_new').value.trim();
  const value = document.getElementById('f_ai_key_value_new').value.trim();
  if(!name){ toast('请输入 Key 名称'); return; }
  if(!value){ toast('请输入 Key 值'); return; }
  api('/api/credentials',{method:'POST',body:JSON.stringify({ai_key_name:name, ai_key_value:value})})
    .then(r=>{
      toast(r.ok?r.note:(r.error||'保存失败'));
      if(r.ok){ document.getElementById('f_ai_key_name_new').value=''; document.getElementById('f_ai_key_value_new').value=''; loadKeyNames(); }
    }).catch(e=>toast(e.message));
}

function deleteNamedKey(){
  const name = document.getElementById('f_ai_key_name').value;
  if(!name) return;
  if(!confirm(`确定删除 Key 档案「${name}」？`)) return;
  api('/api/credentials',{method:'POST',body:JSON.stringify({ai_key_delete:name})})
    .then(r=>{
      toast(r.ok?r.note:(r.error||'删除失败'));
      if(r.ok){ curKeyName=''; loadKeyNames(); }
    }).catch(e=>toast(e.message));
}

function testAiConnection(){
  toast('⏳ 正在测试连接…');
  api('/api/ai/test').then(r=>{
    toast(r.ok?r.note:(r.error||'连接失败'));
  }).catch(e=>toast(e.message));
}

if(localStorage.getItem('token')){ document.getElementById('loginModal').classList.remove('active'); }
refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="gufa_console", description=f"{APP_TITLE}（浏览器访问即用）"
    )
    parser.add_argument("--config", default="config.json",
                        help="配置文件路径（默认 config.json）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；手机/局域网访问请用 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--token", default="",
                        help="访问令牌；不指定则随机生成并打印在终端")
    args = parser.parse_args()
    sys.exit(run_console(
        Path(args.config), host=args.host, port=args.port, token=args.token,
    ))
