#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GuFaQuant-Pro 8.0

本文件由 AI 编写（模型：deepseek-v4-flash-0731）；使用前请自行审查，
作者不对代码正确性、安全性或交易结果作任何担保。

面向生产部署的、默认安全的 CCXT 现货多标的量化交易服务。

设计边界：
- 仅支持现货、单向做多；不伪装成可跨交易所安全通用的合约系统。
- 不提供本地纸面账户；默认连接交易所 Sandbox/Testnet/Demo API，所有成交均以交易所回报为准。
- 8.0 起十大古法因子替换为真实排盘（奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱），
  由大语言模型（默认 deepseek-v4-flash-0731，config.ai.model 可配置）作为断卦师解读完整盘面
  并转译为 BUY/SELL/HOLD；decision_mode=full 时 AI 全权决策：
  动作与目标仓位自主，不启用保护性止损/止盈/移动止损，账户级熔断（日内亏损/回撤/成交
  次数）与单币/总仓位/合约名义上限均放开；仅保留 ORDER_UNCERTAIN 硬停、max_order_quote
  单笔上限与现金留存等交易所安全阀。
- 排盘与断卦遵循公开规则（代码内披露），但预测准确性不保证；正式盘必须显式确认风险。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import getpass
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少依赖 pandas，请执行: pip install -r requirements.txt") from exc

try:
    import ccxt
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少依赖 ccxt，请执行: pip install -r requirements.txt") from exc

try:  # 8.0 真实排盘模块（可选）：缺少时回退旧技术因子并记录警告。
    from gufa_paipan import PaipanService
    from gufa_paipan_qimen import QimenPaipan
    from gufa_paipan_liuren import LiurenPaipan
    from gufa_paipan_taiyi import TaiyiPaipan
    from gufa_paipan_bazi import BaziPaipan, SizhuPaipan
    from gufa_paipan_ziwei import ZiweiPaipan
    from gufa_paipan_yijing import YijingPaipan, MeihuaPaipan, BaguaPaipan, FengshuiPaipan
    from gufa_paipan_signal import paipan_signals, paipan_verdicts
    PAIPAN_AVAILABLE = True
    _PAIPAN_IMPORT_ERROR: Optional[BaseException] = None
except ImportError as exc:  # pragma: no cover
    PAIPAN_AVAILABLE = False
    _PAIPAN_IMPORT_ERROR = exc

APP_NAME = "GuFaQuant-Pro"
APP_VERSION = "8.8.0"
CONFIG_VERSION = 3
STATE_VERSION = 5
CREDENTIALS_VERSION = 2
AI_KEYS_VERSION = 1
STRATEGY_NAMES = ("奇门", "六壬", "太乙", "易经", "风水", "八字", "梅花", "紫微", "八卦", "四柱")
AI_ACTIONS = {"BUY", "SELL", "HOLD"}
AI_TARGET_LEVELS = {"FLAT", "HALF", "FULL", "UNCHANGED"}
AI_BIASES = {"bullish", "bearish", "neutral"}
# 市场类型：现货与永续合约（USDT 本位）。AI 可在配置允许的范围内自主选择。
MARKET_SPOT = "spot"
MARKET_SWAP = "swap"
MARKETS = (MARKET_SPOT, MARKET_SWAP)
AI_MARKETS = {MARKET_SPOT, MARKET_SWAP}


def market_symbol(market: str, symbol: str, quote: str) -> str:
    """现货与合约的交易所统一符号：spot -> BTC/USDT；swap -> BTC/USDT:USDT。"""
    if market == MARKET_SWAP:
        return f"{symbol}:{quote}"
    return symbol


def position_key(market: str, symbol: str) -> str:
    """状态文件持仓键：现货保持原符号（向后兼容旧 state），合约加 swap: 前缀。"""
    if market == MARKET_SWAP:
        return f"swap:{symbol}"
    return symbol


def split_position_key(key: str) -> Tuple[str, str]:
    """拆分持仓键 -> (market, base_symbol)。旧 state 中裸符号视为现货。"""
    if isinstance(key, str) and key.startswith("swap:"):
        return MARKET_SWAP, key[len("swap:"):]
    return MARKET_SPOT, str(key)

# 死币特征：无有效行情数据，古法无法分析。这些失败只剔除该标的当日候选资格，
# 不视为系统故障（真实故障如网络/超时/限流仍走 fail-closed）。次日自动重新探测。
DEAD_SYMBOL_MARKERS: Tuple[str, ...] = (
    "无有效价格",
    "无有效 K 线",
    "无有效K线",
    "返回空 K 线",
    "返回空K线",
    "空 K 线",
    "空K线",
    "有效 K 线不足",
    "有效K线不足",
    "行情为空",
    "无报价",
    "no valid price",
    "not enough data",
    "empty data",
    "no data",
    "not found",
    "does not exist",
    "invalid symbol",
    "unsupported symbol",
    "delisted",
    "已下架",
    "停交易",
    "无交易",
    # 行情数据异常（如单根 K 线涨跌超 50%）属标的自身数据质量问题：
    # 当日剔除、次日自动重试，不视为系统故障，避免拖垮整个候选池初选。
    "疑似数据异常",
    "涨跌超过 50%",
)
ANCIENT_METHOD_DESCRIPTIONS = {
    "奇门": "时家转盘奇门：阴阳遁局数与九宫八门九星八神盘，值符星所临宫之门定吉凶。",
    "六壬": "大六壬：月将加时起天地盘，四课三传与课体（贼克/比用/遥克等）断事。",
    "太乙": "太乙神数（简式）：太乙积年与太乙行九宫落宫、十六神、三基五福。",
    "易经": "周易时间起卦：本卦/变卦与动爻，依卦辞爻辞断吉凶。",
    "风水": "玄空飞星：三元九运与流年/流月九星入中飞布盘。",
    "八字": "子平八字：四柱十神、日主旺衰与用神取用，兼看大运。",
    "梅花": "梅花易数：时间起卦，体用生克（用生体/比和吉，用克体凶）。",
    "紫微": "紫微斗数：十二宫主星布列、命身宫与年干四化。",
    "八卦": "京房八宫：卦入八宫定世应，纳甲装卦配六亲。",
    "四柱": "四柱宫位六亲与神煞（贵人/文昌/禄神/驿马等）及纳音。",
}
EPSILON = 1e-12
UTC = timezone.utc
# =============================================================================
# 通用工具
# =============================================================================

class ConfigError(ValueError):
    pass


class AIRelayError(ConfigError):
    """AI 中转站/传输层错误（余额不足、鉴权失败、网络错误等）。

    携带 HTTP 状态与错误摘要，区别于『响应结构无效』——后者可尝试格式修复，
    前者（如余额不足）重试只会继续烧钱/继续失败，必须直接回退。
    """

    def __init__(self, message: str, status: Optional[int] = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class SafetyError(RuntimeError):
    pass


class OrderUncertainError(SafetyError):
    """订单提交结果不确定；必须停止自动交易并人工核对交易所。"""
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def sigmoid_tanh(value: float, scale: float = 1.0) -> float:
    """将任意实数平滑映射到 [0, 1]。"""
    return clamp(0.5 + 0.5 * math.tanh(finite(value) / max(scale, EPSILON)))


def atomic_write_json(path: Path, payload: Mapping[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, mode)
    except OSError:
        pass
    # Windows：目标文件可能被其他进程（如 console/dashboard）短暂以读方式
    # 打开，os.replace 会抛 PermissionError(WinError 5)。短重试后仍失败再抛。
    for attempt in range(4):
        try:
            os.replace(str(temp), str(path))
            return
        except PermissionError:
            if attempt < 3:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_json(path: Path) -> Dict[str, Any]:
    try:
        # utf-8-sig 同时兼容 Windows 工具写入的 UTF-8 BOM 与无 BOM 文件
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 无效: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是 JSON object")
    return data


def reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], section: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(f"配置段 {section} 存在未知字段: {', '.join(unknown)}")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None

def default_credentials_path(config_path: Path) -> Path:
    """为每份配置派生独立凭据文件，避免模拟盘、正式盘或多个账户误共享密钥。"""
    override = os.getenv("GUFA_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg_home = os.getenv("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".config"
    identity = hashlib.sha256(str(config_path.expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return (root / "gufa-quant-pro" / f"credentials-{identity}.json").resolve()


class CredentialStore:
    """权限收紧的本地明文凭据仓库；环境变量始终具有更高优先级。

    支持两类凭据：
    - secrets：按环境变量名存储（exchange key/secret/password, 旧式 AI key）
    - ai_keys：按自定义名称存储（支持多个 AI API Key 自主切换）
    """

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.secrets: Dict[str, str] = {}
        self.ai_keys: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"凭据文件无法读取或 JSON 无效: {self.path}: {exc}") from exc
        if type(payload) is not dict:
            raise ConfigError(f"凭据文件顶层必须是 JSON object: {self.path}")
        # v1: 只有 secrets；v2: 新增 ai_keys
        allowed = {"version", "secrets", "ai_keys"}
        reject_unknown(payload, allowed, "credentials")
        version = require_json_int(payload.get("version"), "credentials.version")
        if version == 1:
            # v1 → v2 自动升级：加载 secrets，新增 ai_keys 字段
            raw = require_json_object(payload.get("secrets", {}), "credentials.secrets")
            self.secrets = {}
            for name, value in raw.items():
                key = require_json_string(name, "credentials.secrets key").strip()
                secret = require_json_string(value, f"credentials.secrets.{key}").strip()
                if key and secret:
                    self.secrets[key] = secret
            self.ai_keys = {}
            self._tighten_permissions()
            self.save()  # 升级到 v2 格式
            return
        if version != CREDENTIALS_VERSION:
            raise ConfigError(
                f"凭据文件版本不兼容: {version} != {CREDENTIALS_VERSION}: {self.path}"
            )
        raw = require_json_object(payload.get("secrets", {}), "credentials.secrets")
        self.secrets = {}
        for name, value in raw.items():
            key = require_json_string(name, "credentials.secrets key").strip()
            secret = require_json_string(value, f"credentials.secrets.{key}").strip()
            if key and secret:
                self.secrets[key] = secret
        # ai_keys 字段（v2 新增；v1 文件不包含此字段，默认为空）
        raw_ai = payload.get("ai_keys", {})
        if raw_ai is not None and not isinstance(raw_ai, dict):
            raise ConfigError("credentials.ai_keys 必须是 JSON object")
        self.ai_keys = {}
        if raw_ai:
            for name, value in raw_ai.items():
                k = str(name).strip()
                v = str(value).strip()
                if k and v:
                    self.ai_keys[k] = v
        self._tighten_permissions()

    def _tighten_permissions(self) -> None:
        if os.name == "nt":
            return
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        with contextlib.suppress(OSError):
            if self.path.exists():
                os.chmod(self.path, 0o600)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tighten_permissions()
        atomic_write_json(
            self.path,
            {
                "version": CREDENTIALS_VERSION,
                "secrets": self.secrets,
                "ai_keys": self.ai_keys,
            },
            mode=0o600,
        )
        self._tighten_permissions()

    def set(self, name: str, value: str) -> None:
        key = name.strip()
        secret = value.strip()
        if not key:
            raise ConfigError("凭据名称不能为空")
        if secret:
            self.secrets[key] = secret
        else:
            self.secrets.pop(key, None)

    def stored(self, name: str) -> str:
        return self.secrets.get(name.strip(), "").strip()

    def resolve(self, name: str, required: bool, purpose: str = "API") -> str:
        if not name:
            if required:
                raise ConfigError(f"{purpose} 缺少凭据名称")
            return ""
        value = os.getenv(name, "").strip() or self.stored(name)
        if required and not value:
            raise ConfigError(
                f"缺少 {purpose} 凭据 {name}。请运行 setup 完成首次配置；"
                f"凭据文件位置: {self.path}"
            )
        return value

    def source(self, name: str) -> str:
        if os.getenv(name, "").strip():
            return "environment"
        if self.stored(name):
            return "credential-store"
        return "missing"

    # ---- 命名 AI Key（多 Key 档案） ----
    def list_ai_key_names(self) -> List[str]:
        """返回已保存的 AI Key 名称列表（不含值）。"""
        return sorted(self.ai_keys)

    def set_ai_key(self, name: str, value: str) -> None:
        """保存或更新一个命名 AI Key。"""
        k = name.strip()
        v = value.strip()
        if not k:
            raise ConfigError("AI Key 名称不能为空")
        if not v:
            self.ai_keys.pop(k, None)
            return
        # 名称限中英文数字短横下划线
        if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9_\-]+', k):
            raise ConfigError("AI Key 名称只能包含中文、字母、数字、短横、下划线")
        self.ai_keys[k] = v

    def delete_ai_key(self, name: str) -> None:
        """删除一个命名 AI Key。"""
        self.ai_keys.pop(name.strip(), None)

    def resolve_ai_key(self, name: str, required: bool) -> str:
        """从 ai_keys 档案中解析指定名称的 Key。"""
        if not name:
            if required:
                raise ConfigError("AI Key 名称不能为空")
            return ""
        value = self.ai_keys.get(name.strip(), "").strip()
        if required and not value:
            raise ConfigError(
                f"缺少 AI API Key「{name}」。请在控制台凭据页添加。"
            )
        return value


def secret_from_env(
    name: str,
    required: bool,
    credentials: Optional[CredentialStore] = None,
    purpose: str = "API",
) -> str:
    """兼容旧调用名：优先读取环境变量，随后读取本地凭据仓库。"""
    if credentials is not None:
        return credentials.resolve(name, required, purpose)
    if not name:
        if required:
            raise ConfigError(f"{purpose} 缺少密钥环境变量名称")
        return ""
    value = os.getenv(name, "").strip()
    if required and not value:
        raise ConfigError(f"{purpose} 要求设置环境变量: {name}")
    return value



def timeframe_seconds(exchange: Any, timeframe: str) -> int:
    try:
        return int(exchange.parse_timeframe(timeframe))
    except Exception as exc:
        raise ConfigError(f"无法解析 timeframe={timeframe}") from exc


def base_asset(symbol: str) -> str:
    return symbol.split("/", 1)[0].strip()


def quote_asset(symbol: str) -> str:
    right = symbol.split("/", 1)[1].strip()
    return right.split(":", 1)[0]

def stable_client_order_id(exchange_id: str, symbol: str, side: str) -> str:
    raw = f"{exchange_id}|{symbol}|{side}|{time.time_ns()}|{uuid.uuid4().hex}"
    # 大多数交易所允许 20~36 字符；避免中文和分隔符。
    return "gufa" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def require_json_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{path} 必须是 JSON boolean，不能使用字符串或数字代替")
    return value


def require_json_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{path} 必须是 JSON integer")
    return value


def require_json_number(value: Any, path: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ConfigError(f"{path} 必须是有限 JSON number")
    return float(value)


def require_json_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} 必须是 JSON string")
    return value


def require_json_object(value: Any, path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ConfigError(f"{path} 必须是 JSON object")
    return value


def require_json_array(value: Any, path: str) -> List[Any]:
    if type(value) is not list:
        raise ConfigError(f"{path} 必须是 JSON array")
    return value


def build_profile_id(config: "AppConfig", account_key: str = "") -> str:
    """绑定状态与交易所账户环境；只保存 API Key 的不可逆摘要，不保存密钥本身。"""
    identity = {
        "exchange": config.exchange.id,
        "sandbox": config.exchange.sandbox,
        "market_type": config.exchange.market_type,
        "quote": config.runtime.quote_currency,
        "symbols": sorted(config.runtime.symbols),
        "api_key_env": config.exchange.api_key_env,
        "account_key_fingerprint": hashlib.sha256(account_key.encode("utf-8")).hexdigest()
        if account_key else "",
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()



# =============================================================================
# 配置模型
# =============================================================================


@dataclass
class ExchangeConfig:
    id: str = "okx"
    sandbox: bool = True
    market_type: str = "spot"
    # AI 可自主选择的市场白名单（现货/合约）。默认仅现货，行为与旧版一致；
    # 启用合约需同时满足：id=okx、risk.max_leverage>1 且账户支持 USDT 本位永续。
    allowed_markets: Tuple[str, ...] = (MARKET_SPOT,)
    api_key_env: str = "GUFA_API_KEY"
    secret_env: str = "GUFA_API_SECRET"
    password_env: str = "GUFA_API_PASSWORD"
    timeout_ms: int = 15000
    max_retries: int = 4
    retry_base_seconds: float = 1.0
    recv_window_ms: int = 10000
    client_order_id_param: str = "clientOrderId"
    proxy_url: str = ""  # 首选代理：仅本应用请求使用的代理（ccxt proxies），如 http://127.0.0.1:7890；留空不走代理
    proxy_list: Tuple[str, ...] = ()  # 代理池（自动切换）：JSON 数组或逗号分隔字符串；当前代理失效时自动轮换到下一个可用代理

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExchangeConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "exchange")
        parsers: Dict[str, Callable[[Any, str], Any]] = {
            "id": require_json_string,
            "sandbox": require_json_bool,
            "market_type": require_json_string,
            "allowed_markets": cls._parse_market_list,
            "api_key_env": require_json_string,
            "secret_env": require_json_string,
            "password_env": require_json_string,
            "timeout_ms": require_json_int,
            "max_retries": require_json_int,
            "retry_base_seconds": require_json_number,
            "recv_window_ms": require_json_int,
            "client_order_id_param": require_json_string,
            "proxy_url": require_json_string,
            "proxy_list": cls._parse_proxy_list,
        }
        values = {key: parsers[key](value, f"exchange.{key}") for key, value in data.items()}
        return cls(**values)

    @staticmethod
    def _parse_proxy_list(value: Any, path: str) -> Tuple[str, ...]:
        """代理池：接受 JSON 数组或逗号分隔字符串；去重保序。"""
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        else:
            items = [require_json_string(item, path) for item in require_json_array(value, path)]
        return tuple(dict.fromkeys(items))

    @staticmethod
    def _parse_market_list(value: Any, path: str) -> Tuple[str, ...]:
        """市场白名单：JSON 数组或逗号分隔字符串；小写去重。"""
        if isinstance(value, str):
            items = [item.strip().lower() for item in value.split(",") if item.strip()]
        else:
            items = [
                require_json_string(item, path).strip().lower()
                for item in require_json_array(value, path)
            ]
        unknown = set(items) - set(MARKETS)
        if unknown:
            raise ConfigError(f"{path} 包含不支持的市场类型: {sorted(unknown)}（支持 {list(MARKETS)}）")
        if not items:
            raise ConfigError(f"{path} 不能为空")
        return tuple(dict.fromkeys(items))

    def effective_proxies(self) -> Tuple[str, ...]:
        """当前生效的代理池：proxy_url 优先，proxy_list 追加去重。"""
        items: List[str] = []
        if self.proxy_url:
            items.append(self.proxy_url)
        for proxy in self.proxy_list:
            if proxy and proxy not in items:
                items.append(proxy)
        return tuple(items)

    def validate(self) -> None:
        self.id = self.id.strip().lower()
        self.market_type = self.market_type.strip().lower()
        if not self.id:
            raise ConfigError("exchange.id 不能为空")
        if self.market_type not in MARKETS:
            raise ConfigError(f"exchange.market_type 必须是 {list(MARKETS)}")
        if self.market_type not in self.allowed_markets:
            raise ConfigError("exchange.market_type 必须包含在 allowed_markets 内")
        if MARKET_SWAP in self.allowed_markets and self.id != "okx":
            raise ConfigError("合约市场目前仅适配 OKX（ccxt okx 适配器）")
        if MARKET_SWAP in self.allowed_markets and not self.sandbox:
            raise ConfigError("合约市场当前仅允许在 sandbox 模拟盘启用；实盘合约需人工复核杠杆与强平风控后再开放")
        if self.timeout_ms < 3000:
            raise ConfigError("exchange.timeout_ms 不应小于 3000")
        if not 0 <= self.max_retries <= 10:
            raise ConfigError("exchange.max_retries 必须在 0..10")
        if self.retry_base_seconds <= 0:
            raise ConfigError("exchange.retry_base_seconds 必须大于 0")
        self.proxy_url = self.proxy_url.strip()
        if self.proxy_url and not (
            self.proxy_url.startswith("http://") or self.proxy_url.startswith("https://")
        ):
            raise ConfigError("exchange.proxy_url 必须以 http:// 或 https:// 开头（如 http://127.0.0.1:7890）")
        cleaned: List[str] = []
        for proxy in self.proxy_list:
            proxy = proxy.strip()
            if not proxy:
                continue
            if not (proxy.startswith("http://") or proxy.startswith("https://")):
                raise ConfigError(
                    f"exchange.proxy_list 中的代理必须以 http:// 或 https:// 开头: {proxy!r}"
                )
            if proxy not in cleaned:
                cleaned.append(proxy)
        self.proxy_list = tuple(cleaned)


@dataclass
class RuntimeConfig:
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT"])
    quote_currency: str = "USDT"
    timeframe: str = "1h"
    ohlcv_limit: int = 250
    poll_interval_seconds: int = 60
    closed_candle_only: bool = True
    max_candle_lag_seconds: int = 900
    state_dir: str = "./runtime"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10
    health_file: str = "health.json"
    webhook_url: str = ""  # 可选：成交/熔断等事件通知端点（留空禁用）
    once_on_start: bool = True
    max_consecutive_cycle_errors: int = 5
    # 信号触发模式（trigger_mode="signal"）：不再按固定周期全量重算，
    # 而是 AI 预设触发条件（价格/涨跌幅/RSI/放量/吉时），监听循环按最小间隔
    # （OKX 批量 tickers 限频 20req/2s，取 2 秒）轮询，条件命中才执行交易。
    trigger_mode: str = "signal"   # "signal"=信号触发（默认）| "cycle"=旧周期模式
    trigger_poll_seconds: int = 2  # 监听轮询间隔（秒），不得小于 1
    trigger_max_wait_hours: float = 24.0  # 触发条件最长等待，超时唤醒 AI 重新评估

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "runtime")
        string_fields = {"quote_currency", "timeframe", "state_dir", "log_level", "health_file", "webhook_url"}
        int_fields = {
            "ohlcv_limit", "poll_interval_seconds", "max_candle_lag_seconds",
            "log_max_bytes", "log_backup_count", "max_consecutive_cycle_errors",
            "trigger_poll_seconds",
        }
        bool_fields = {"closed_candle_only", "once_on_start"}
        float_fields = {"trigger_max_wait_hours"}
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"runtime.{key}"
            if key == "symbols":
                raw_symbols = require_json_array(value, path)
                symbols: List[str] = []
                for index, item in enumerate(raw_symbols):
                    symbol = require_json_string(item, f"{path}[{index}]").strip()
                    if not symbol:
                        raise ConfigError(f"{path}[{index}] 不能为空")
                    symbols.append(symbol)
                values[key] = symbols
            elif key in string_fields:
                values[key] = require_json_string(value, path)
            elif key in int_fields:
                values[key] = require_json_int(value, path)
            elif key in bool_fields:
                values[key] = require_json_bool(value, path)
            elif key in float_fields:
                values[key] = require_json_number(value, path)
            elif key == "trigger_mode":
                mode = require_json_string(value, path).strip().lower()
                if mode not in {"signal", "cycle"}:
                    raise ConfigError("runtime.trigger_mode 必须是 signal 或 cycle")
                values[key] = mode
        cfg = cls(**values)
        cfg.symbols = [symbol.strip() for symbol in cfg.symbols]
        cfg.quote_currency = cfg.quote_currency.strip().upper()
        cfg.timeframe = cfg.timeframe.strip()
        cfg.state_dir = cfg.state_dir.strip()
        cfg.log_level = cfg.log_level.strip().upper()
        cfg.health_file = cfg.health_file.strip()
        cfg.webhook_url = cfg.webhook_url.strip()
        return cfg

    def validate(self) -> None:
        if not self.symbols:
            raise ConfigError("runtime.symbols 不能为空")
        if len(set(self.symbols)) != len(self.symbols):
            raise ConfigError("runtime.symbols 存在重复项")
        bases = [base_asset(s) for s in self.symbols]
        if len(set(bases)) != len(bases):
            raise ConfigError("同一基础资产不能配置多个交易对，否则余额会重复计价")
        for symbol in self.symbols:
            if "/" not in symbol:
                raise ConfigError(f"交易对格式错误: {symbol}")
            if quote_asset(symbol).upper() != self.quote_currency:
                raise ConfigError(f"{symbol} 的计价币必须为 {self.quote_currency}")
        if self.ohlcv_limit < 120:
            raise ConfigError("runtime.ohlcv_limit 至少为 120")
        if self.poll_interval_seconds < 5:
            raise ConfigError("runtime.poll_interval_seconds 至少为 5 秒")
        if self.trigger_poll_seconds < 1:
            raise ConfigError("runtime.trigger_poll_seconds 至少为 1 秒")
        if self.trigger_max_wait_hours <= 0 or self.trigger_max_wait_hours > 168:
            raise ConfigError("runtime.trigger_max_wait_hours 必须在 (0, 168] 小时")
        if not self.timeframe:
            raise ConfigError("runtime.timeframe 不能为空")
        if not self.state_dir or not self.health_file:
            raise ConfigError("runtime.state_dir/health_file 不能为空")
        if self.log_max_bytes < 1024 or self.log_backup_count < 1:
            raise ConfigError("runtime.log_max_bytes 至少 1024 且 log_backup_count 至少 1")
        if self.max_candle_lag_seconds <= 0:
            raise ConfigError("runtime.max_candle_lag_seconds 必须大于 0")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("runtime.log_level 无效")
        if self.max_consecutive_cycle_errors < 1:
            raise ConfigError("runtime.max_consecutive_cycle_errors 至少为 1")


DEFAULT_PREFERRED_BASES: Tuple[str, ...] = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "ATOM", "UNI", "LTC", "BCH", "TRX", "XLM", "NEAR", "APT", "ARB", "OP",
    "SUI", "INJ", "TON", "SHIB", "PEPE", "WIF", "AAVE", "FIL", "ICP", "ALGO",
)


@dataclass
class DailySelectionConfig:
    """每日候选池初选；只使用确定性十项技术因子，不让 AI 扩展人工白名单。

    初筛仅用名字规则（prefilter=True 时），不使用流动性：
    preferred（主流币）优先保留，排除以数字开头的新币/蹭热币与
    exclude_patterns 匹配的标的；零行情请求、绝不 fail-closed。
    候选池全部进入古法扫描，每天重点看哪几个完全由古法得分决定
    （min_score 门槛 + top_n 上限），保持严谨。
    """

    enabled: bool = True
    timeframe: str = "1d"
    ohlcv_limit: int = 250
    top_n: int = 3
    # 选股门槛校准：十项时间起卦信号的实测分布为 0.45~0.52（中位 ~0.49），
    # 旧默认 0.55 高于信号上限导致永远选不出标的（连续数日 selected=[]）。
    # 0.45 保证信号合理区间内总能选出 top3；真实行情下本命盘参与后分差拉开。
    min_score: float = 0.45
    prefilter: bool = True
    preferred: Tuple[str, ...] = ()
    exclude_patterns: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DailySelectionConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "selection")
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"selection.{key}"
            if key in {"enabled", "prefilter"}:
                values[key] = require_json_bool(value, path)
            elif key in {"ohlcv_limit", "top_n"}:
                values[key] = require_json_int(value, path)
            elif key in {"min_score"}:
                values[key] = require_json_number(value, path)
            elif key in {"preferred", "exclude_patterns"}:
                items = require_json_array(value, path)
                values[key] = tuple(str(item).strip() for item in items)
            else:
                values[key] = require_json_string(value, path)
        cfg = cls(**values)
        cfg.timeframe = cfg.timeframe.strip()
        return cfg

    def validate(self) -> None:
        if not self.timeframe:
            raise ConfigError("selection.timeframe 不能为空")
        if self.ohlcv_limit < 120:
            raise ConfigError("selection.ohlcv_limit 至少为 120")
        if self.top_n < 1:
            raise ConfigError("selection.top_n 至少为 1")
        if not 0 <= self.min_score <= 1:
            raise ConfigError("selection.min_score 必须在 0..1")



@dataclass
class StrategyConfig:
    weights: Dict[str, float] = field(default_factory=lambda: {
        # 8.6.3 按"现实历史地位 + 择时实战记录"重排权重（总和=1.0）：
        #   奇门 0.17  三式之首、帝王之学，历代军师择时决策（张良/诸葛亮/刘伯温），体系最庞大
        #   六壬 0.15  三式之一，《大六壬指南》等，日用占卜第一术，最擅断时机应期
        #   易经 0.14  群经之首、最古老占筮体系，孔子系辞，权威最高（偏大方向）
        #   八字 0.11  子平命理宋代定型，断人生格局，非短期择时
        #   八卦 0.10  六爻/文王卦，一事一占，民间实战最普及
        #   太乙 0.08  三式之一但主断国运天时灾异，对个人交易时机适用弱
        #   梅花 0.08  邵雍《梅花易数》，起卦灵活，口碑好但体系简、应期粗
        #   紫微 0.07  与八字并称的命理体系，偏命盘，短期择时适用低
        #   四柱 0.06  与八字同源（四柱即八字），降权避免重复计权
        #   风水 0.04  堪舆主空间环境吉凶，历史上不用于择时
        "奇门": 0.17,
        "六壬": 0.15,
        "易经": 0.14,
        "八字": 0.11,
        "八卦": 0.10,
        "太乙": 0.08,
        "梅花": 0.08,
        "紫微": 0.07,
        "四柱": 0.06,
        "风水": 0.04,
    })
    # 交易阈值校准（8.6）：十项古法（时间起卦）信号实测分布为 0.45~0.52，
    # 中位 ~0.49。旧阈值（half=0.64/full=0.76）是旧技术因子时代的，信号
    # 分布不同导致永远不触发买入（分数恒 < 0.64）。按古法分布重校：
    #   half=0.47 偏多即半仓（信号 ~80% 分位以下仍有纪律）；
    #   full=0.55 高于信号上限，需本命盘/强势时辰才满仓；
    #   exit=0.44 低于弱市下限，跌破即清仓。
    entry_half: float = 0.47
    entry_full: float = 0.55
    exit_score: float = 0.44
    hold_hysteresis: float = 0.03
    min_signal_change: float = 0.015

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "strategy")
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"strategy.{key}"
            if key == "weights":
                raw_weights = require_json_object(value, path)
                weights: Dict[str, float] = {}
                for name, weight in raw_weights.items():
                    strategy_name = require_json_string(name, f"{path} key")
                    weights[strategy_name] = require_json_number(weight, f"{path}.{strategy_name}")
                values[key] = weights
            else:
                values[key] = require_json_number(value, path)
        return cls(**values)

    def validate(self) -> None:
        unknown = set(self.weights) - set(STRATEGY_NAMES)
        missing = set(STRATEGY_NAMES) - set(self.weights)
        if unknown or missing:
            raise ConfigError(f"strategy.weights 指标不完整；缺少={sorted(missing)}，未知={sorted(unknown)}")
        if any((not math.isfinite(v) or v < 0) for v in self.weights.values()):
            raise ConfigError("strategy.weights 必须是非负有限数")
        if sum(self.weights.values()) <= 0:
            raise ConfigError("strategy.weights 总和必须大于 0")
        if not 0 <= self.exit_score < self.entry_half < self.entry_full <= 1:
            raise ConfigError("阈值必须满足 0 <= exit_score < entry_half < entry_full <= 1")
        if not 0 <= self.hold_hysteresis <= 0.15:
            raise ConfigError("strategy.hold_hysteresis 必须在 0..0.15")
        if not 0 <= self.min_signal_change <= 0.25:
            raise ConfigError("strategy.min_signal_change 必须在 0..0.25")


@dataclass
class RiskConfig:
    max_symbol_allocation: float = 0.20
    max_total_allocation: float = 0.70
    cash_reserve_pct: float = 0.20
    max_order_quote: float = 10000.0
    min_order_quote: float = 10.0
    min_rebalance_quote: float = 20.0
    min_rebalance_pct: float = 0.01
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    stop_loss_pct: float = 0.035
    take_profit_pct: float = 0.10
    trailing_stop_pct: float = 0.04
    trailing_activation_pct: float = 0.035
    max_slippage_bps: float = 30.0
    max_trades_per_day: int = 12
    cooldown_seconds: int = 300
    order_fill_timeout_seconds: int = 20
    reject_unmanaged_positions: bool = True
    dust_quote: float = 5.0
    live_trading_ack: str = ""
    # ---- 合约（永续）风控 ----
    max_leverage: float = 1.0              # 合约最大杠杆（1.0 = 无杠杆）
    futures_margin_cap_pct: float = 0.30   # 合约保证金占用占权益上限
    futures_allow_short: bool = False      # 默认禁止做空（SELL 无持仓=开空，高风险）
    max_futures_notional_pct: float = 0.50  # 单标的合约名义敞口占权益上限

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RiskConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "risk")
        int_fields = {"max_trades_per_day", "cooldown_seconds", "order_fill_timeout_seconds"}
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"risk.{key}"
            if key == "reject_unmanaged_positions":
                values[key] = require_json_bool(value, path)
            elif key == "futures_allow_short":
                values[key] = require_json_bool(value, path)
            elif key in int_fields:
                values[key] = require_json_int(value, path)
            elif key == "live_trading_ack":
                values[key] = require_json_string(value, path)
            else:
                values[key] = require_json_number(value, path)
        return cls(**values)

    def validate(self) -> None:
        for name in (
            "max_symbol_allocation", "max_total_allocation", "cash_reserve_pct",
            "max_daily_loss_pct", "max_drawdown_pct", "stop_loss_pct",
            "take_profit_pct", "trailing_stop_pct", "trailing_activation_pct",
        ):
            value = float(getattr(self, name))
            if not 0 <= value < 1:
                raise ConfigError(f"risk.{name} 必须在 [0, 1)")
        if self.max_symbol_allocation <= 0 or self.max_total_allocation <= 0:
            raise ConfigError("最大仓位必须大于 0")
        if self.max_symbol_allocation > self.max_total_allocation:
            raise ConfigError("max_symbol_allocation 不得大于 max_total_allocation")
        if self.max_total_allocation > 1 - self.cash_reserve_pct + 1e-9:
            raise ConfigError("max_total_allocation 与 cash_reserve_pct 冲突")
        if self.max_order_quote <= 0 or self.min_order_quote <= 0:
            raise ConfigError("订单金额上下限必须大于 0")
        if self.min_rebalance_quote < 0 or not 0 <= self.min_rebalance_pct < 1 or self.dust_quote < 0:
            raise ConfigError("最小调仓金额/比例或 dust_quote 无效")
        if self.max_order_quote < self.min_order_quote:
            raise ConfigError("max_order_quote 不得小于 min_order_quote")
        if self.max_slippage_bps <= 0 or self.max_slippage_bps > 1000:
            raise ConfigError("max_slippage_bps 必须在 (0, 1000]")
        if self.max_trades_per_day < 1:
            raise ConfigError("max_trades_per_day 至少为 1")
        if self.cooldown_seconds < 0 or self.order_fill_timeout_seconds < 1:
            raise ConfigError("冷却/成交等待时间无效")
        if not 1.0 <= self.max_leverage <= 50:
            raise ConfigError("risk.max_leverage 必须在 [1, 50]")
        if not 0 <= self.futures_margin_cap_pct < 1:
            raise ConfigError("risk.futures_margin_cap_pct 必须在 [0, 1)")
        if not 0 < self.max_futures_notional_pct <= 1:
            raise ConfigError("risk.max_futures_notional_pct 必须在 (0, 1]")
        if self.max_leverage > 1.0 and self.max_futures_notional_pct > self.max_total_allocation:
            raise ConfigError("合约名义敞口上限不得大于现货最大总仓位上限")


@dataclass
class AIConfig:
    enabled: bool = False
    model: str = "gpt-4.1-mini"
    api_key_env: str = "OPENAI_API_KEY"
    api_key_name: str = ""          # 命名 AI Key（多 Key 档案），优先于 api_key_env
    base_url: str = ""
    timeout_seconds: int = 20
    fail_closed: bool = True
    minimum_allow_confidence: float = 0.60
    decision_mode: str = "bounded"
    max_output_tokens: int = 1200
    # 思考档位（reasoning 模型专用，如 deepseek-v4-flash）：low/medium/high/xhigh/max。
    # 空字符串 = 不传该参数（默认档位，通常为 medium）。
    # flash 等模型 medium 思考会耗尽输出预算导致空正文，设 low 可显著降低该概率。
    reasoning_effort: str = ""
    # 拆分模式：十项古法各发一次小请求（每次输出 ~100-300 token），再做一次综合请求。
    # 对 reasoning 型模型（如 deepseek-v4-flash）可靠，避免单次大请求输出被思考预算耗尽返回空正文。
    split_readings: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AIConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "ai")
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"ai.{key}"
            if key in {"enabled", "fail_closed", "split_readings"}:
                values[key] = require_json_bool(value, path)
            elif key in {"timeout_seconds", "max_output_tokens"}:
                values[key] = require_json_int(value, path)
            elif key == "minimum_allow_confidence":
                values[key] = require_json_number(value, path)
            else:
                values[key] = require_json_string(value, path)
        return cls(**values)

    def validate(self) -> None:
        if not 0 <= self.minimum_allow_confidence <= 1:
            raise ConfigError("ai.minimum_allow_confidence 必须在 0..1")
        if self.timeout_seconds < 1:
            raise ConfigError("ai.timeout_seconds 必须大于 0")
        if not 300 <= self.max_output_tokens <= 4000:
            raise ConfigError("ai.max_output_tokens 必须在 300..4000")
        self.model = self.model.strip()
        self.api_key_env = self.api_key_env.strip()
        self.api_key_name = self.api_key_name.strip()
        self.base_url = self.base_url.strip()
        self.decision_mode = self.decision_mode.strip().lower()
        if self.decision_mode not in {"bounded", "explain_only", "full"}:
            raise ConfigError("ai.decision_mode 必须是 bounded / explain_only / full")
        self.reasoning_effort = self.reasoning_effort.strip().lower()
        if self.reasoning_effort not in {"", "low", "medium", "high", "xhigh", "max"}:
            raise ConfigError("ai.reasoning_effort 必须是 low/medium/high/xhigh/max 之一（或留空）")
        if self.enabled and not self.model:
            raise ConfigError("启用 AI 时 ai.model 不能为空")


@dataclass
class PaipanConfig:
    """8.0 真实古法排盘配置。enabled=false 时回退到旧技术因子，便于回滚。"""

    enabled: bool = True
    true_solar_time: bool = True        # 真太阳时修正（经度/纬度计算均时差+黄赤交角差）
    longitude: float = 116.4074         # 默认北京东经
    latitude: float = 39.9042           # 默认北京北纬
    listing_time_source: str = "ohlcv"  # 本命盘上市时间来源: ohlcv(最早K线) / manual(配置)
    listing_times: Dict[str, str] = field(default_factory=dict)  # manual: symbol -> ISO-8601
    exchange_timezone: str = "UTC"      # 交易所行情时间戳时区

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaipanConfig":
        reject_unknown(data, cls.__dataclass_fields__.keys(), "paipan")
        values: Dict[str, Any] = {}
        for key, value in data.items():
            path = f"paipan.{key}"
            if key in {"enabled", "true_solar_time"}:
                values[key] = require_json_bool(value, path)
            elif key in {"longitude", "latitude"}:
                values[key] = require_json_number(value, path)
            elif key == "listing_times":
                raw = require_json_object(value, path)
                times: Dict[str, str] = {}
                for symbol, iso in raw.items():
                    times[require_json_string(symbol, f"{path} key")] = require_json_string(
                        iso, f"{path}.{symbol}"
                    )
                values[key] = times
            else:
                values[key] = require_json_string(value, path)
        return cls(**values)

    def validate(self) -> None:
        if not -180 <= self.longitude <= 180:
            raise ConfigError("paipan.longitude 必须在 [-180, 180]")
        if not -90 <= self.latitude <= 90:
            raise ConfigError("paipan.latitude 必须在 [-90, 90]")
        if self.listing_time_source not in {"ohlcv", "manual"}:
            raise ConfigError("paipan.listing_time_source 必须是 ohlcv 或 manual")
        if self.listing_time_source == "manual" and not self.listing_times:
            raise ConfigError("paipan.listing_time_source=manual 时必须提供 listing_times")
        for symbol, iso in self.listing_times.items():
            try:
                parse_iso(iso)
            except ValueError as exc:
                raise ConfigError(f"paipan.listing_times.{symbol} 不是有效 ISO 时间: {iso}") from exc


@dataclass
class AppConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    selection: DailySelectionConfig = field(default_factory=DailySelectionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    paipan: PaipanConfig = field(default_factory=PaipanConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppConfig":
        reject_unknown(
            data,
            {"version", "exchange", "runtime", "selection", "strategy", "risk", "ai", "paipan"},
            "root",
        )
        version = require_json_int(data.get("version"), "version")
        if version not in {2, CONFIG_VERSION}:
            raise ConfigError(f"不支持的配置版本: {version}，当前要求 {CONFIG_VERSION}")
        cfg = cls(
            exchange=ExchangeConfig.from_dict(require_json_object(data.get("exchange", {}), "exchange")),
            runtime=RuntimeConfig.from_dict(require_json_object(data.get("runtime", {}), "runtime")),
            selection=DailySelectionConfig.from_dict(
                require_json_object(data.get("selection", {}), "selection")
            ),
            strategy=StrategyConfig.from_dict(require_json_object(data.get("strategy", {}), "strategy")),
            risk=RiskConfig.from_dict(require_json_object(data.get("risk", {}), "risk")),
            ai=AIConfig.from_dict(require_json_object(data.get("ai", {}), "ai")),
            paipan=PaipanConfig.from_dict(require_json_object(data.get("paipan", {}), "paipan")),
        )
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        return cls.from_dict(load_json(path))

    def validate(self) -> None:
        self.exchange.validate()
        self.runtime.validate()
        self.selection.validate()
        self.strategy.validate()
        self.risk.validate()
        self.ai.validate()
        self.paipan.validate()
        if not self.exchange.sandbox and self.risk.live_trading_ack != "I_UNDERSTAND_LIVE_TRADING_RISK":
            raise ConfigError(
                "正式盘被拒绝：exchange.sandbox=false 时，risk.live_trading_ack 必须精确设置为 "
                "I_UNDERSTAND_LIVE_TRADING_RISK"
            )


# =============================================================================
# 日志、锁与状态
# =============================================================================


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("symbol", "order_id", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging(runtime: RuntimeConfig) -> logging.Logger:
    state_dir = Path(runtime.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(getattr(logging, runtime.log_level))
    logger.propagate = False
    logger.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    file_handler = RotatingFileHandler(
        state_dir / "gufa_quant.jsonl",
        maxBytes=runtime.log_max_bytes,
        backupCount=runtime.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


class InstanceLock:
    """跨平台单实例锁；进程异常退出时自动清理残留锁。"""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Optional[Any] = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """检查进程是否存活（跨平台，不依赖 subprocess）。"""
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                kernel32.CloseHandle(handle)
                return code.value == 259  # STILL_ACTIVE
            kernel32.CloseHandle(handle)
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _read_lock_pid(path: Path) -> int:
        """读取锁文件中的 PID；文件被独占/损坏时返回 -1（未知）。"""
        try:
            text = path.read_text(encoding="utf-8").strip()
            return int(text) if text.isdigit() else -1
        except Exception:
            return -1

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            # 自动清理残留锁：PID 无效或进程已死，清锁重试
            stale_pid = self._read_lock_pid(self.path)
            if stale_pid <= 0 or not self._pid_alive(stale_pid):
                # 死进程：尝试清理锁文件，失败则小延迟后重试锁
                try:
                    self.path.unlink(missing_ok=True)
                except PermissionError:
                    time.sleep(0.5)
                self.handle = self.path.open("a+", encoding="utf-8")
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.handle.seek(0)
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, BlockingIOError):
                    self.handle.close()
                    self.handle = None
                    pid_text = "未知（锁被占用，无法读取）" if stale_pid < 0 else f"{stale_pid}"
                    raise SafetyError(
                        f"已有实例正在运行（PID {pid_text}），无法取得锁: {self.path}\n"
                        f"请先停止正在运行的交易进程，或确认无进程后删除该锁文件重试。"
                    ) from exc
            else:
                raise SafetyError(
                    f"已有实例正在运行（PID {stale_pid}），无法取得锁: {self.path}\n"
                    f"请先停止 PID {stale_pid} 对应的交易进程。"
                ) from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.handle:
            return
        with contextlib.suppress(Exception):
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


@dataclass
class PositionState:
    amount: float = 0.0
    avg_entry: float = 0.0
    high_water: float = 0.0
    opened_at: str = ""
    updated_at: str = ""
    # 多市场支持：market=spot/swap；swap 侧 side 固定 long（默认禁止做空）。
    market: str = MARKET_SPOT
    side: str = "long"
    leverage: float = 1.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PositionState":
        allowed = cls.__dataclass_fields__.keys()
        values = {k: data[k] for k in allowed if k in data}
        values.setdefault("market", MARKET_SPOT)
        values.setdefault("side", "long")
        values.setdefault("leverage", 1.0)
        return cls(**values)


@dataclass
class TriggerCondition:
    """一条触发条件。

    kind: price_above / price_below        —— 现价（相对 ref_price 基准价）
          change_pct_above / below         —— 相对基准价的涨跌幅（如 0.05=+5%）
          rsi_above / rsi_below            —— RSI 阈值
          volume_surge                     —— 成交量突增倍数（value=倍数）
          time_after                       —— 到点唤醒（value=ISO 时间戳）
    value: 条件数值
    ref_price: 基准价（买入价基准；price/change 类必须）
    note: AI 给出的可读理由
    """

    kind: str
    value: float
    ref_price: float = 0.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], now_iso: Optional[str] = None) -> "TriggerCondition":
        kind = str(data.get("kind", ""))
        if kind not in {
            "price_above", "price_below", "change_pct_above", "change_pct_below",
            "rsi_above", "rsi_below", "volume_surge", "time_after",
        }:
            raise SafetyError(f"未知触发条件类型: {kind!r}")
        if kind == "time_after":
            # time_after 的 value 是 ISO 时间字符串；兼容 AI 输出纯数字 N
            # （表示 N 小时后），此时需要 now_iso 上下文换算成绝对时刻。
            value: Any = cls._normalize_time_after(data.get("value"), now_iso)
        else:
            # 数值类条件，不能走 finite() 吞掉非法值。
            value = finite(data.get("value"))
        return cls(
            kind=kind,
            value=value,
            ref_price=finite(data.get("ref_price")),
            note=str(data.get("note", "")),
        )

    @staticmethod
    def _normalize_time_after(raw_value: Any, now_iso: Optional[str]) -> str:
        """把 time_after 的 value 规范为 ISO 时间字符串。

        AI 可能输出 ISO 时间戳（推荐）或纯数字 N（表示 N 小时后）；两者都
        无法解析时返回空串，由调用方丢弃该条件（绝不产生永不触发/误触发）。
        """
        raw = str(raw_value or "").strip()
        if not raw:
            return ""
        dt = parse_iso(raw)
        if dt is not None:
            return dt.isoformat()
        if now_iso:
            try:
                hours = float(raw)
                if 0 < hours <= 720:  # 最多 30 天，防止异常大数
                    base = parse_iso(now_iso)
                    if base is not None:
                        return (base + timedelta(hours=hours)).isoformat()
            except (TypeError, ValueError):
                pass
        return ""


@dataclass
class TriggerSet:
    """单个标的的触发条件集：入场（AI-1 古法决策）与出场（AI-2 古法决策）。

    entry: 入场条件列表；命中任一即按 entry_target 建仓。
    exit:  出场条件列表；命中任一即平仓。
    entry_target: 入场目标仓位（0.5=半仓，1.0=满仓，绝对权益占比）。
    entry_market / entry_leverage: 入场市场（spot/swap）与杠杆。
    first_trigger_at: 古法择时确定的当日首次触发时刻（ISO）；未到则只监听不入场。
    ref_price: 买入成交均价（AI-2 出场涨跌幅的基准），入场后由系统回填。
    created_at / updated_at: 时间戳。
    """

    symbol: str
    entry: List[TriggerCondition] = field(default_factory=list)
    exit: List[TriggerCondition] = field(default_factory=list)
    entry_target: float = 0.5
    entry_market: str = MARKET_SPOT
    entry_leverage: float = 1.0
    first_trigger_at: str = ""
    ref_price: float = 0.0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry": [c.to_dict() for c in self.entry],
            "exit": [c.to_dict() for c in self.exit],
            "entry_target": self.entry_target,
            "entry_market": self.entry_market,
            "entry_leverage": self.entry_leverage,
            "first_trigger_at": self.first_trigger_at,
            "ref_price": self.ref_price,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TriggerSet":
        entry = [
            TriggerCondition.from_dict(item)
            for item in dict(data.get("entry", {})).values() if isinstance(item, dict)
        ] if isinstance(data.get("entry"), dict) else [
            TriggerCondition.from_dict(item)
            for item in data.get("entry", []) if isinstance(item, dict)
        ]
        exit_conds = [
            TriggerCondition.from_dict(item)
            for item in data.get("exit", []) if isinstance(item, dict)
        ]
        # 双保险：丢弃无法解析为绝对时刻的 time_after（防止空值/非法值误触发）。
        exit_conds = [c for c in exit_conds if not (c.kind == "time_after" and not c.value)]
        entry = [c for c in entry if not (c.kind == "time_after" and not c.value)]
        return cls(
            symbol=str(data.get("symbol", "")),
            entry=entry,
            exit=exit_conds,
            entry_target=clamp(finite(data.get("entry_target")), 0.0, 1.0),
            entry_market=str(data.get("entry_market", MARKET_SPOT)),
            entry_leverage=clamp(finite(data.get("entry_leverage")), 1.0, 50.0),
            first_trigger_at=str(data.get("first_trigger_at", "")),
            ref_price=finite(data.get("ref_price")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class BotState:
    version: int = STATE_VERSION
    profile_id: str = ""
    positions: Dict[str, PositionState] = field(default_factory=dict)
    pending_orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_trade_at: Dict[str, str] = field(default_factory=dict)
    trade_day: str = ""
    trades_today: int = 0
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    last_equity: float = 0.0
    last_cycle_at: str = ""
    last_scores: Dict[str, float] = field(default_factory=dict)
    daily_selection_date: str = ""
    daily_selection_key: str = ""
    daily_selected_symbols: List[str] = field(default_factory=list)
    daily_selection_candidates: List[str] = field(default_factory=list)
    daily_selection_scores: Dict[str, float] = field(default_factory=dict)
    daily_selection_candle_times: Dict[str, str] = field(default_factory=dict)
    daily_selection_dead: Dict[str, str] = field(default_factory=dict)
    # 当日订单簿深度不足（buy 拒单）的标的：symbol -> UTC 日期。次日自动失效重试，
    # 避免薄盘币每周期重复下单失败刷日志；有持仓时卖出/保护性退出不受影响。
    depth_blocked: Dict[str, str] = field(default_factory=dict)
    halted_reason: str = ""
    # 信号触发模式状态：symbol -> 触发条件集（入场/出场），由 AI 预设、监听循环消费。
    # 结构见 TriggerSet.to_dict；不参与旧周期模式（trigger_mode=cycle 时为空）。
    triggers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # AI-1 入场评估冷却：symbol -> ISO 时间。古法判定"暂不交易"或 AI 调用失败时写入，
    # 监听循环在该时间前不再唤醒 AI-1，避免每 2 秒轮询反复调用 AI 烧钱/打爆限频。
    trigger_skip_until: Dict[str, str] = field(default_factory=dict)
    # 最近一次 AI-1 布防返回的十项古法读数（奇门/六壬/.../四柱），供大屏雷达展示。
    last_readings: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BotState":
        positions = {
            str(symbol): PositionState.from_dict(value)
            for symbol, value in dict(data.get("positions", {})).items()
            if isinstance(value, dict)
        }
        return cls(
            version=int(data.get("version", 1)),
            profile_id=str(data.get("profile_id", "")),
            positions=positions,
            pending_orders=dict(data.get("pending_orders", {})),
            last_trade_at=dict(data.get("last_trade_at", {})),
            trade_day=str(data.get("trade_day", "")),
            trades_today=int(data.get("trades_today", 0)),
            day_start_equity=finite(data.get("day_start_equity")),
            peak_equity=finite(data.get("peak_equity")),
            last_equity=finite(data.get("last_equity")),
            last_cycle_at=str(data.get("last_cycle_at", "")),
            last_scores={str(k): finite(v) for k, v in dict(data.get("last_scores", {})).items()},
            daily_selection_date=str(data.get("daily_selection_date", "")),
            daily_selection_key=str(data.get("daily_selection_key", "")),
            daily_selected_symbols=[str(item) for item in data.get("daily_selected_symbols", [])],
            daily_selection_candidates=[
                str(item) for item in data.get("daily_selection_candidates", [])
            ],
            daily_selection_scores={
                str(k): finite(v) for k, v in dict(data.get("daily_selection_scores", {})).items()
            },
            daily_selection_candle_times={
                str(k): str(v) for k, v in dict(data.get("daily_selection_candle_times", {})).items()
            },
            daily_selection_dead={
                str(k): str(v) for k, v in dict(data.get("daily_selection_dead", {})).items()
            },
            depth_blocked={
                str(k): str(v) for k, v in dict(data.get("depth_blocked", {})).items()
            },
            halted_reason=str(data.get("halted_reason", "")),
            triggers={
                str(k): dict(v) for k, v in dict(data.get("triggers", {})).items()
                if isinstance(v, dict)
            },
            # AI-1 暂不交易/失败的冷却：symbol -> ISO 时间，在此之前不再唤醒 AI-1。
            trigger_skip_until={
                str(k): str(v) for k, v in dict(data.get("trigger_skip_until", {})).items()
            },
            last_readings=dict(data.get("last_readings", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


class StateStore:
    def __init__(self, path: Path, profile_id: str):
        self.path = path
        self.profile_id = profile_id
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> BotState:
        if not self.path.exists():
            state = BotState(profile_id=self.profile_id)
            atomic_write_json(self.path, state.to_dict())
            return state
        try:
            state = BotState.from_dict(load_json(self.path))
        except Exception as exc:
            raise SafetyError(f"状态文件损坏，拒绝启动: {self.path}: {exc}") from exc
        if state.version != STATE_VERSION:
            # v4 -> v5：新增 triggers 字段（信号触发模式状态），旧状态自动补空。
            if state.version == 4 and STATE_VERSION == 5:
                state.version = 5
                # 迁移写盘：self.state 尚未赋值，不能走 self.save()。
                atomic_write_json(self.path, state.to_dict())
            else:
                raise SafetyError(
                    f"状态文件版本不兼容: {state.version} != {STATE_VERSION}。"
                    "请备份状态文件并按升级文档迁移，禁止自动降级或跳过检查。"
                )
        if state.profile_id != self.profile_id:
            if not state.profile_id:
                raise SafetyError(
                    f"状态文件缺少运行身份绑定，拒绝自动迁移: {self.path}。"
                    "请备份后删除/改名状态文件，或继续使用与旧状态完全一致的程序版本。"
                )
            raise SafetyError(
                f"状态文件属于其他运行配置，拒绝启动: {self.path}。"
                "sandbox/正式盘、交易所账户、计价币或 symbols 发生变化时必须使用独立 state_dir。"
            )
        return state

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self.state.to_dict())


# =============================================================================
# 行情质量与十大信号
# =============================================================================


@dataclass
class SignalResult:
    score: float
    signals: Dict[str, float]
    diagnostics: Dict[str, float]
    candle_time: str


class MarketDataValidator:
    REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")

    @classmethod
    def validate(cls, frame: pd.DataFrame, minimum_rows: int = 100) -> pd.DataFrame:
        if frame is None or frame.empty:
            raise SafetyError("行情为空")
        missing = set(cls.REQUIRED) - set(frame.columns)
        if missing:
            raise SafetyError(f"行情缺列: {sorted(missing)}")
        df = frame.loc[:, cls.REQUIRED].copy()
        for column in cls.REQUIRED:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df = df.dropna().drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
        if len(df) < minimum_rows:
            raise SafetyError(f"有效 K 线不足: {len(df)} < {minimum_rows}")
        if not df["timestamp"].is_monotonic_increasing:
            raise SafetyError("K 线时间戳未递增")
        positive = (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        sane = (
            (df["high"] >= df[["open", "close", "low"]].max(axis=1))
            & (df["low"] <= df[["open", "close", "high"]].min(axis=1))
            & (df["volume"] >= 0)
        )
        if not bool((positive & sane).all()):
            raise SafetyError("K 线包含非正价格或 OHLC 关系异常")
        returns = df["close"].pct_change().abs()
        if finite(returns.iloc[-1]) > 0.50:
            raise SafetyError("最新闭合 K 线涨跌超过 50%，疑似数据异常，拒绝交易")
        return df.reset_index(drop=True)


def build_paipan_service(paipan_config: Any) -> PaipanService:
    """组装十项排盘器并返回 PaipanService（report/backtest/策略共用）。"""
    if not PAIPAN_AVAILABLE:
        raise ConfigError("排盘模块不可用: " + str(_PAIPAN_IMPORT_ERROR))
    service = PaipanService(paipan_config)
    for panzer in (QimenPaipan(), LiurenPaipan(), TaiyiPaipan(),
                   YijingPaipan(), FengshuiPaipan(), BaziPaipan(),
                   MeihuaPaipan(), ZiweiPaipan(), BaguaPaipan(), SizhuPaipan()):
        service.register(panzer)
    return service


class StrategyEngine:
    """十类互补信号，输出 [0,1] 看多置信度。

    8.0 起：当 paipan 配置启用时，信号来自十项真实古法排盘
    （确定性简化断卦规则，见 gufa_paipan_signal）；否则回退旧技术因子。
    """

    def __init__(self, config: StrategyConfig, paipan_config: Optional[Any] = None):
        self.config = config
        self.paipan_service: Optional[PaipanService] = None
        if paipan_config is not None and getattr(paipan_config, "enabled", False):
            self.paipan_service = build_paipan_service(paipan_config)

    @staticmethod
    def _series_last(series: pd.Series, default: float = 0.0) -> float:
        if series.empty:
            return default
        return finite(series.iloc[-1], default)

    def calculate(self, raw: pd.DataFrame, symbol: Optional[str] = None) -> SignalResult:
        if self.paipan_service is not None:
            return self._calculate_paipan(raw, symbol)
        return self._calculate_technical(raw)

    def _calculate_paipan(self, raw: pd.DataFrame, symbol: str) -> SignalResult:
        """真实排盘信号：以最新 K 线时间作为当前时辰基准，标的上市时间做本命盘。"""
        df = MarketDataValidator.validate(raw, minimum_rows=20)
        close = df["close"].astype(float)
        price = self._series_last(close)
        candle_ms = int(finite(df["timestamp"].iloc[-1]))
        candle_time = datetime.fromtimestamp(candle_ms / 1000, UTC).isoformat()
        now_dt = datetime.fromtimestamp(candle_ms / 1000, UTC)
        listing_ts = float(df["timestamp"].iloc[0])  # 本命盘：最早 K 线时间戳

        if symbol is None:
            symbol = "UNKNOWN"
        result = self.paipan_service.paipan(symbol, now_dt=now_dt, listing_ts=listing_ts)
        signals = paipan_signals(result.to_dict())
        total_weight = sum(self.config.weights.values())
        score = sum(signals[name] * self.config.weights[name] for name in STRATEGY_NAMES) / total_weight
        diagnostics = {
            "price": price,
            "paipan": result.to_dict(),
            "paipan_score": {name: signals[name] for name in STRATEGY_NAMES},
        }
        return SignalResult(clamp(score), signals, diagnostics, candle_time)

    def _calculate_technical(self, raw: pd.DataFrame) -> SignalResult:
        df = MarketDataValidator.validate(raw, minimum_rows=100)
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        prev_close = close.shift(1)
        true_range = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        atr_last = max(self._series_last(atr), EPSILON)
        price = self._series_last(close)

        # 1 奇门：EMA 趋势差，相对 ATR 标准化。
        ema8 = close.ewm(span=8, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        qimen_raw = (self._series_last(ema8) - self._series_last(ema21)) / atr_last
        qimen = sigmoid_tanh(qimen_raw, 1.2)

        # 2 六壬：MACD 柱动量。
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - macd_signal
        liuren_raw = self._series_last(hist) / atr_last
        liuren = sigmoid_tanh(liuren_raw, 0.45)

        # 3 太乙：布林带位置，避免单纯“高于均线=看多”的二值跳变。
        mid20 = close.rolling(20).mean()
        std20 = close.rolling(20).std(ddof=0)
        upper = mid20 + 2 * std20
        lower = mid20 - 2 * std20
        band_width = max(self._series_last(upper - lower), EPSILON)
        taiyi = clamp((price - self._series_last(lower)) / band_width)

        # 4 易经：RSI 水平 + 最近反转；极端超买不继续盲目加分。
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = gain / loss.replace(0, EPSILON)
        rsi = 100 - 100 / (1 + rs)
        rsi_last = self._series_last(rsi, 50.0)
        rsi_slope = rsi_last - finite(rsi.iloc[-4], rsi_last)
        if rsi_last >= 75:
            yijing = clamp(0.55 + 0.02 * min(rsi_slope, 5.0))
        elif rsi_last <= 25:
            yijing = clamp(0.40 + 0.05 * max(rsi_slope, -5.0))
        else:
            yijing = clamp(0.20 + 0.60 * (rsi_last - 25) / 50 + 0.015 * rsi_slope)

        # 5 风水：OBV 近期方向，以同期成交量归一化。
        direction = close.diff().fillna(0).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        obv = (direction * volume).cumsum()
        vol20 = max(finite(volume.tail(20).sum()), EPSILON)
        obv_impulse = (self._series_last(obv) - finite(obv.iloc[-11])) / vol20
        fengshui = sigmoid_tanh(obv_impulse, 0.25)

        # 6 八字：Wilder ADX / DMI，趋势越强时方向差权重越高。
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_w = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w.replace(0, EPSILON)
        minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w.replace(0, EPSILON)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, EPSILON)
        adx = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        dmi_diff = self._series_last(plus_di) - self._series_last(minus_di)
        adx_strength = clamp(self._series_last(adx, 20.0) / 35.0)
        bazi = clamp(0.5 + (sigmoid_tanh(dmi_diff, 12.0) - 0.5) * (0.5 + adx_strength))

        # 7 梅花：随机指标 K/D，兼顾所处区间与交叉方向。
        low14 = low.rolling(14).min()
        high14 = high.rolling(14).max()
        stochastic_k = 100 * (close - low14) / (high14 - low14).replace(0, EPSILON)
        stochastic_d = stochastic_k.rolling(3).mean()
        k_last = self._series_last(stochastic_k, 50.0)
        d_last = self._series_last(stochastic_d, 50.0)
        meihua = clamp(0.5 * (k_last / 100.0) + 0.5 * sigmoid_tanh(k_last - d_last, 8.0))

        # 8 紫微：20 周期收益，按当前 ATR 波动标准化。
        roc20 = price / max(finite(close.iloc[-21], price), EPSILON) - 1
        normalized_roc = roc20 / max(atr_last / price * math.sqrt(20), EPSILON)
        ziwei = sigmoid_tanh(normalized_roc, 1.5)

        # 9 八卦：Donchian 通道位置；通道使用前序数据，避免前视偏差。
        donchian_high = high.shift(1).rolling(20).max()
        donchian_low = low.shift(1).rolling(20).min()
        channel = max(self._series_last(donchian_high - donchian_low), EPSILON)
        bagua = clamp((price - self._series_last(donchian_low)) / channel)

        # 10 四柱：四周期价格冲量 / ATR。
        momentum4 = price - finite(close.iloc[-5], price)
        sizhu = sigmoid_tanh(momentum4 / atr_last, 2.0)

        signals = {
            "奇门": qimen,
            "六壬": liuren,
            "太乙": taiyi,
            "易经": yijing,
            "风水": fengshui,
            "八字": bazi,
            "梅花": meihua,
            "紫微": ziwei,
            "八卦": bagua,
            "四柱": sizhu,
        }
        total_weight = sum(self.config.weights.values())
        score = sum(signals[name] * self.config.weights[name] for name in STRATEGY_NAMES) / total_weight
        diagnostics = {
            "price": price,
            "atr": atr_last,
            "atr_pct": atr_last / price,
            "rsi": rsi_last,
            "adx": self._series_last(adx, 0.0),
            "macd_hist": self._series_last(hist),
            "volume": self._series_last(volume),
        }
        candle_ms = int(finite(df["timestamp"].iloc[-1]))
        candle_time = datetime.fromtimestamp(candle_ms / 1000, UTC).isoformat()
        return SignalResult(clamp(score), signals, diagnostics, candle_time)

    def target_fraction(self, score: float, current_fraction: float) -> Tuple[float, str]:
        """带滞回的目标仓位，防止阈值附近反复成交。"""
        h = self.config.hold_hysteresis
        current = clamp(current_fraction)
        if score >= self.config.entry_full:
            return 1.0, f"score={score:.4f} >= full={self.config.entry_full:.4f}"
        if score >= self.config.entry_half:
            return 0.5, f"score={score:.4f} >= half={self.config.entry_half:.4f}"
        if score <= self.config.exit_score:
            return 0.0, f"score={score:.4f} <= exit={self.config.exit_score:.4f}"
        # 已持有时，只有明确跌破带滞回边界才降档。
        if current > 0.75 and score >= self.config.entry_half - h:
            return 1.0, "full position hysteresis hold"
        if current > 0 and score >= self.config.exit_score + h:
            return 0.5, "half position hysteresis hold"
        return 0.0, "neutral zone"


# =============================================================================
# 交易所适配、账户快照与订单
# =============================================================================


@dataclass
class AccountPosition:
    symbol: str
    amount: float
    price: float
    quote_value: float
    avg_entry: float
    high_water: float
    # 多市场支持（spot 仓位这些字段用默认值）
    market: str = MARKET_SPOT
    side: str = "long"
    notional: float = 0.0          # 合约名义价值（long 为正）；现货=quote_value
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    contracts: float = 0.0         # 合约张数（现货为 0）
    liquidation_price: float = 0.0


@dataclass
class AccountSnapshot:
    equity: float
    quote_free: float
    quote_total: float
    positions: Dict[str, AccountPosition]
    timestamp: str


@dataclass
class OrderPlan:
    symbol: str
    side: str
    amount: float
    reference_price: float
    estimated_quote: float
    target_allocation: float
    current_allocation: float
    reason: str
    # 多市场支持：spot 合约（swap）按张数下单
    market: str = MARKET_SPOT
    leverage: float = 1.0
    contracts: float = 0.0


@dataclass
class FillResult:
    order_id: str
    symbol: str
    side: str
    requested_amount: float
    filled_amount: float
    average_price: float
    fee_quote: float
    status: str
    raw: Dict[str, Any] = field(default_factory=dict)
    market: str = MARKET_SPOT
    leverage: float = 1.0


class ExchangeGateway:
    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        logger: logging.Logger,
        credentials: Optional[CredentialStore] = None,
    ):
        self.config = config
        self.exchange_cfg = config.exchange
        self.risk = config.risk
        self.runtime = config.runtime
        self.state_store = state_store
        self.log = logger
        self.credentials = credentials
        self.client: Any = None
        self.market_client: Any = None  # 实盘只读行情客户端（沙盒模式专用），_connect 里创建
        self.markets: Dict[str, Any] = {}
        # 多代理自动切换：代理池 + 当前索引 + 冷却期（网络错误后暂时跳过，避免反复打坏代理）
        self._proxies: Tuple[str, ...] = config.exchange.effective_proxies()
        self._proxy_index: int = 0
        self._proxy_cooldown: Dict[str, float] = {}
        self._proxy_switches: int = 0
        self._connect()

    def exchange_symbol(self, market: str, symbol: str) -> str:
        """现货/合约的交易所统一符号（合约 = base:quote）。"""
        return market_symbol(market, symbol, self.runtime.quote_currency)

    @property
    def allowed_markets(self) -> Tuple[str, ...]:
        """配置允许的市场列表；旧配置对象/测试桩缺字段时回退纯现货。"""
        value = getattr(self.exchange_cfg, "allowed_markets", None)
        if value:
            return tuple(value)
        return (MARKET_SPOT,)

    def _connect(self) -> None:
        exchange_id = self.exchange_cfg.id
        if not hasattr(ccxt, exchange_id):
            raise ConfigError(f"CCXT 不支持交易所: {exchange_id}")
        exchange_class = getattr(ccxt, exchange_id)
        # 默认市场类型：纯合约配置走 swap，否则保持 spot（load_markets 对 OKX 会加载全部市场）
        default_type = (
            MARKET_SWAP
            if tuple(self.allowed_markets) == (MARKET_SWAP,)
            else MARKET_SPOT
        )
        params: Dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": self.exchange_cfg.timeout_ms,
            "options": {
                "defaultType": default_type,
                "adjustForTimeDifference": True,
                "recvWindow": self.exchange_cfg.recv_window_ms,
            },
        }
        if self.exchange_cfg.proxy_url:
            # 仅本应用请求走代理，不影响系统/其他进程网络（ccxt 原生 proxies 支持）
            params["proxies"] = {"http": self.exchange_cfg.proxy_url, "https": self.exchange_cfg.proxy_url}
        api_key = secret_from_env(
            self.exchange_cfg.api_key_env, required=True,
            credentials=self.credentials, purpose="交易所 API",
        )
        secret = secret_from_env(
            self.exchange_cfg.secret_env, required=True,
            credentials=self.credentials, purpose="交易所 API",
        )
        password = secret_from_env(
            self.exchange_cfg.password_env, required=False,
            credentials=self.credentials, purpose="交易所 API",
        )
        params["apiKey"] = api_key
        params["secret"] = secret
        if password:
            params["password"] = password
        self.client = exchange_class(params)
        if self.exchange_cfg.sandbox:
            if not hasattr(self.client, "set_sandbox_mode"):
                raise ConfigError(f"{exchange_id} 的 CCXT 适配器不支持 set_sandbox_mode")
            self.client.set_sandbox_mode(True)
            # 沙盒模式的 demo-okx.com 返回模拟行情（部分币价格冻结/失真），
            # 而行情评估与大屏实时价必须以真实市场为准。单独建一个实盘只读
            # 行情客户端（公开行情接口无需 API key）：行情读取走它，交易/账户
            # 仍走沙盒 self.client，互不干扰。
            self.market_client = self._build_market_client(require_markets=True)
            self.log.warning(
                "行情源: 实盘公开行情(%s) | 交易源: EXCHANGE-SANDBOX",
                self.market_client.hostname if hasattr(self.market_client, "hostname") else "okx",
            )
        else:
            self.market_client = self.client
        self.markets = self._safe_call("load_markets", lambda: self.client.load_markets())
        self._validate_markets()
        mode = "EXCHANGE-SANDBOX" if self.exchange_cfg.sandbox else "EXCHANGE-PRODUCTION"
        self.log.warning("交易所已连接: %s | mode=%s | market=spot", exchange_id.upper(), mode)

    def _current_proxy(self) -> str:
        """当前生效代理（空串 = 直连）。"""
        if not self._proxies:
            return ""
        return self._proxies[self._proxy_index % len(self._proxies)]

    def _build_client(self, proxy_url: str) -> Any:
        """按指定代理构建 ccxt 客户端（不含 load_markets，供初始连接与代理切换共用）。"""
        exchange_id = self.exchange_cfg.id
        if not hasattr(ccxt, exchange_id):
            raise ConfigError(f"CCXT 不支持交易所: {exchange_id}")
        exchange_class = getattr(ccxt, exchange_id)
        default_type = (
            MARKET_SWAP
            if tuple(self.allowed_markets) == (MARKET_SWAP,)
            else MARKET_SPOT
        )
        params: Dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": self.exchange_cfg.timeout_ms,
            "options": {
                "defaultType": default_type,
                "adjustForTimeDifference": True,
                "recvWindow": self.exchange_cfg.recv_window_ms,
            },
        }
        if proxy_url:
            # 仅本应用请求走代理，不影响系统/其他进程网络（ccxt 原生 proxies 支持）
            params["proxies"] = {"http": proxy_url, "https": proxy_url}
        api_key = secret_from_env(
            self.exchange_cfg.api_key_env, required=True,
            credentials=self.credentials, purpose="交易所 API",
        )
        secret = secret_from_env(
            self.exchange_cfg.secret_env, required=True,
            credentials=self.credentials, purpose="交易所 API",
        )
        password = secret_from_env(
            self.exchange_cfg.password_env, required=False,
            credentials=self.credentials, purpose="交易所 API",
        )
        params["apiKey"] = api_key
        params["secret"] = secret
        if password:
            params["password"] = password
        client = exchange_class(params)
        if self.exchange_cfg.sandbox:
            if not hasattr(client, "set_sandbox_mode"):
                raise ConfigError(f"{exchange_id} 的 CCXT 适配器不支持 set_sandbox_mode")
            client.set_sandbox_mode(True)
        return client

    def _build_market_client(self, require_markets: bool = False) -> Any:
        """实盘只读行情客户端：公开行情接口无需 API key，用于行情评估与大屏实时价。

        沙盒 demo 环境的行情是模拟数据（部分币价格冻结/失真），交易/账户仍走
        沙盒客户端，但 tickers / K 线 / 单币价一律以真实市场为准。
        """
        exchange_id = self.exchange_cfg.id
        if not hasattr(ccxt, exchange_id):
            raise ConfigError(f"CCXT 不支持交易所: {exchange_id}")
        exchange_class = getattr(ccxt, exchange_id)
        params: Dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": self.exchange_cfg.timeout_ms,
            "options": {
                "defaultType": MARKET_SPOT,
                "adjustForTimeDifference": True,
            },
        }
        if self.exchange_cfg.proxy_url:
            params["proxies"] = {"http": self.exchange_cfg.proxy_url, "https": self.exchange_cfg.proxy_url}
        client = exchange_class(params)
        if require_markets:
            self._safe_call("load_markets", lambda: client.load_markets())
        return client

    def _try_switch_proxy(self, name: str, exc: BaseException) -> bool:
        """网络错误后自动切换代理：当前代理进冷却，轮换到下一个可用代理重建客户端。

        返回 True 表示已切换到可用代理（调用方应继续重试）；False 表示无可用代理。
        只在读取/行情路径调用（_safe_call）；下单路径不走 _safe_call，天然不会中途切代理。
        """
        if len(getattr(self, "_proxies", ()) or ()) <= 1:
            return False
        now = time.time()
        current = self._current_proxy()
        if current:
            self._proxy_cooldown[current] = now + 300.0  # 冷却 5 分钟，避免反复打坏同一代理
            self.log.warning("代理 %s 不可用（%s），尝试自动切换", current, str(exc)[:160])
        retry_types = self._network_error_types()
        for _ in range(len(self._proxies)):
            self._proxy_index = (self._proxy_index + 1) % len(self._proxies)
            candidate = self._current_proxy()
            if candidate and self._proxy_cooldown.get(candidate, 0.0) > now:
                continue  # 该代理仍在冷却期，跳过
            try:
                self.client = self._build_client(candidate)
                self.markets = self.client.load_markets()
                self._validate_markets()
                self._proxy_switches += 1
                via = f" -> {candidate}" if candidate else " -> 直连"
                self.log.warning("已自动切换代理%s（累计 %d 次），继续 %s", via, self._proxy_switches, name)
                return True
            except retry_types as switch_exc:
                if candidate:
                    self._proxy_cooldown[candidate] = now + 300.0
                self.log.warning("代理 %s 也不可用（%s），继续轮换", candidate or "直连", str(switch_exc)[:120])
                continue
            except Exception:
                return False  # 配置/逻辑错误不属网络问题，不再轮换
        return False

    def _validate_markets(self) -> None:
        allowed = set(self.allowed_markets)
        for symbol in self.runtime.symbols:
            if MARKET_SPOT in allowed:
                market = self.markets.get(symbol)
                if not market:
                    raise ConfigError(f"交易所不存在现货交易对: {symbol}")
                if not market.get("spot", False):
                    raise ConfigError(f"交易对不是现货市场: {symbol}")
                if market.get("active") is False:
                    raise ConfigError(f"交易对已停用: {symbol}")
                if str(market.get("quote", "")).upper() != self.runtime.quote_currency:
                    raise ConfigError(f"交易所市场 {symbol} 的 quote 与配置不一致")
            if MARKET_SWAP in allowed:
                swap_symbol = self.exchange_symbol(MARKET_SWAP, symbol)
                swap_market = self.markets.get(swap_symbol)
                if not swap_market or not swap_market.get("swap", False):
                    # 混合白名单下个别币无永续合约：允许（AI 只选有 swap 的币，execute 前会再校验），
                    # 但记录警告便于排查。纯合约配置（allowed_markets==("swap",)）仍硬校验。
                    if tuple(self.allowed_markets) == (MARKET_SWAP,):
                        raise ConfigError(f"交易所不存在永续合约交易对: {swap_symbol}")
                    self.log.warning("%s 无永续合约，该标的仅可使用现货", symbol)
                    continue
                if swap_market.get("active") is False:
                    raise ConfigError(f"永续合约已停用: {swap_symbol}")
                if str(swap_market.get("quote", "")).upper() != self.runtime.quote_currency:
                    raise ConfigError(f"合约市场 {swap_symbol} 的 quote 与配置不一致")
                contract_size = finite(swap_market.get("contractSize"), 0.0)
                if contract_size <= 0:
                    raise ConfigError(f"合约市场缺少有效 contractSize: {swap_symbol}")
        required_timeframes = {self.runtime.timeframe}
        if self.config.selection.enabled:
            required_timeframes.add(self.config.selection.timeframe)
        supported_timeframes = self.client.timeframes or {}
        for timeframe in sorted(required_timeframes):
            if timeframe not in supported_timeframes:
                raise ConfigError(f"交易所不支持 timeframe={timeframe}")

    @staticmethod
    def _network_error_types() -> Tuple[type, ...]:
        return tuple(
            cls for cls in (
                getattr(ccxt, "NetworkError", None),
                getattr(ccxt, "RequestTimeout", None),
                getattr(ccxt, "DDoSProtection", None),
                getattr(ccxt, "ExchangeNotAvailable", None),
                getattr(ccxt, "RateLimitExceeded", None),
            ) if isinstance(cls, type)
        )

    def _safe_call(self, name: str, function: Callable[[], Any]) -> Any:
        retry_types = self._network_error_types()
        attempts = self.exchange_cfg.max_retries + 1
        # 每个代理至多重试一轮；多代理时一轮耗尽自动切换下一个可用代理继续
        proxies = getattr(self, "_proxies", ()) or ()
        rounds = max(1, len(proxies))
        last_exc: Optional[BaseException] = None
        for _ in range(rounds):
            for attempt in range(attempts):
                try:
                    return function()
                except retry_types as exc:
                    last_exc = exc
                    if attempt >= attempts - 1:
                        break
                    delay = self.exchange_cfg.retry_base_seconds * (2 ** attempt) + random.random() * 0.4
                    self.log.warning("%s 网络错误，%.2fs 后重试 (%d/%d): %s", name, delay, attempt + 1, attempts, exc)
                    time.sleep(delay)
                except Exception:
                    raise
            # 当前代理重试耗尽：尝试自动切换代理后继续重试
            if last_exc is not None and self._try_switch_proxy(name, last_exc):
                continue
            raise last_exc if last_exc is not None else RuntimeError(f"unreachable safe_call: {name}")
        raise last_exc if last_exc is not None else RuntimeError(f"unreachable safe_call: {name}")

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Optional[str] = None,
        ohlcv_limit: Optional[int] = None,
        market: str = MARKET_SPOT,
    ) -> pd.DataFrame:
        requested_timeframe = timeframe or self.runtime.timeframe
        requested_limit = ohlcv_limit or self.runtime.ohlcv_limit
        limit = requested_limit + 2
        exchange_symbol = self.exchange_symbol(market, symbol)
        rows = self._safe_call(
            f"fetch_ohlcv:{exchange_symbol}:{requested_timeframe}",
            lambda: self.market_client.fetch_ohlcv(exchange_symbol, requested_timeframe, limit=limit),
        )
        if not rows:
            raise SafetyError(f"{symbol} {requested_timeframe} 返回空 K 线")
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        period_seconds = timeframe_seconds(self.market_client, requested_timeframe)
        period_ms = period_seconds * 1000
        now_ms = int(self.market_client.milliseconds())
        if self.runtime.closed_candle_only:
            df = df[df["timestamp"] + period_ms <= now_ms]
        if df.empty:
            raise SafetyError(f"{symbol} {requested_timeframe} 没有闭合 K 线")
        last_close_ms = int(df["timestamp"].iloc[-1]) + period_ms
        lag_seconds = max(0, (now_ms - last_close_ms) // 1000)
        allowed_lag = max(self.runtime.max_candle_lag_seconds, period_seconds * 2)
        if lag_seconds > allowed_lag:
            raise SafetyError(
                f"{symbol} {requested_timeframe} K 线陈旧: lag={lag_seconds}s > {allowed_lag}s"
            )
        return df.tail(requested_limit).reset_index(drop=True)

    def fetch_last_price(self, symbol: str, market: str = MARKET_SPOT) -> float:
        exchange_symbol = self.exchange_symbol(market, symbol)
        ticker = self._safe_call(
            f"fetch_ticker:{exchange_symbol}",
            lambda: self.market_client.fetch_ticker(exchange_symbol),
        )
        price = finite(ticker.get("last") or ticker.get("close"))
        if price <= 0:
            raise SafetyError(f"{exchange_symbol} ticker 无有效价格")
        return price

    def fetch_quote_volumes(
        self, symbols: Optional[Iterable[str]] = None, market: str = MARKET_SPOT
    ) -> Dict[str, float]:
        """批量 24h 成交额（quoteVolume，计价币）。公开接口，失败由调用方降级。"""
        exchange_symbols = (
            [self.exchange_symbol(market, s) for s in symbols] if symbols else None
        )
        tickers = self._safe_call(
            "fetch_tickers",
            lambda: self.market_client.fetch_tickers(exchange_symbols),
        )
        result: Dict[str, float] = {}
        for symbol, ticker in (tickers or {}).items():
            try:
                result[str(symbol)] = finite(ticker.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                result[str(symbol)] = 0.0
        return result

    def fetch_all_tickers(
        self, symbols: Optional[Iterable[str]] = None
    ) -> Dict[str, float]:
        """批量最新价（一次请求），返回键为状态键（spot 裸名 / swap:SYMBOL）。

        供信号触发模式每 2 秒轮询使用：OKX 批量 tickers 接口限频 20req/2s，
        全部标的合并为一个请求，避免逐个 fetch_ticker 打爆限频。
        """
        prices, _ = self.fetch_all_tickers_full(symbols)
        return prices

    def fetch_all_tickers_full(
        self, symbols: Optional[Iterable[str]] = None
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """批量最新价 + 24h 涨跌幅（一次请求），返回 (prices, changes)。

        prices: 状态键 → 最新价；changes: 状态键 → 24h 涨跌幅百分比。
        供信号模式健康报告写入 change_pct，驱动大屏行情涨跌箭头。
        """
        if symbols is None:
            symbols = self.runtime.symbols
        target_map: Dict[str, str] = {}
        for symbol in symbols:
            if not symbol:
                continue
            market, base = split_position_key(str(symbol))
            target_map[self.exchange_symbol(market, base)] = str(symbol)
        if not target_map:
            return {}, {}
        tickers = self._safe_call(
            "fetch_tickers", lambda: self.market_client.fetch_tickers(list(target_map)),
        )
        prices: Dict[str, float] = {}
        changes: Dict[str, float] = {}
        for ex_symbol, ticker in (tickers or {}).items():
            state_key = target_map.get(str(ex_symbol))
            if state_key is None:
                continue
            price = finite(ticker.get("last") or ticker.get("close"))
            if price > 0:
                prices[state_key] = price
            pct = finite(ticker.get("percentage"))
            if -100 <= pct <= 1000:
                changes[state_key] = pct
        return prices, changes

    def reconcile_pending_orders(self) -> None:
        state = self.state_store.state
        changed = False
        for key, pending in list(state.pending_orders.items()):
            market, base_symbol = split_position_key(key)
            exchange_symbol = str(pending.get("exchange_symbol") or self.exchange_symbol(market, base_symbol))
            order_id = str(pending.get("id", ""))
            if not order_id:
                client_id = str(pending.get("client_id", ""))
                message = (
                    f"{key} 存在无交易所 order id 的提交记录"
                    + (f" (client_id={client_id})" if client_id else "")
                    + "，无法证明订单未成交"
                )
                state.halted_reason = "ORDER_UNCERTAIN: " + message
                self.state_store.save()
                raise OrderUncertainError(message + "；禁止自动重下，请人工核对交易所订单和余额")
            try:
                order = self._safe_call(
                    f"fetch_order:{order_id}",
                    lambda oid=order_id, sym=exchange_symbol: self.client.fetch_order(oid, sym),
                )
                status = str(order.get("status", "")).lower()
                if status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
                    del state.pending_orders[key]
                    changed = True
                    self.log.info("挂单已终结: %s %s status=%s", key, order_id, status)
            except getattr(ccxt, "OrderNotFound"):
                # 无法确认时不立即重下；保留一个周期并记录时间。
                created = parse_iso(str(pending.get("created_at", "")))
                if created and (utc_now() - created).total_seconds() > 3600:
                    self.log.error("挂单超过 1h 且无法查询，人工确认后再清理状态: %s %s", key, order_id)
            except self._network_error_types() as exc:
                self.log.warning("挂单对账网络失败，保留 pending: %s %s: %s", key, order_id, exc)
            except Exception as exc:
                message = f"{key} 挂单对账发生非网络异常，无法安全确认订单状态: {exc}"
                state.halted_reason = "ORDER_UNCERTAIN: " + message
                self.state_store.save()
                raise OrderUncertainError(message + "；必须人工核对交易所订单和余额") from exc
        if changed:
            self.state_store.save()

    def _live_balance(self) -> Mapping[str, Any]:
        return self._safe_call("fetch_balance", self.client.fetch_balance)

    def account_snapshot(self, prices: Mapping[str, float]) -> AccountSnapshot:
        state = self.state_store.state
        positions: Dict[str, AccountPosition] = {}
        balance = self._live_balance()
        free_map = balance.get("free") or {}
        total_map = balance.get("total") or {}
        quote_free = max(0.0, finite(free_map.get(self.runtime.quote_currency)))
        quote_total = max(0.0, finite(total_map.get(self.runtime.quote_currency)))
        equity = quote_total
        unmanaged: List[str] = []
        allowed = set(self.allowed_markets)
        # ---- 现货持仓估值（保持旧逻辑；键 = 裸 symbol，向后兼容）----
        for symbol in self.runtime.symbols:
            base = base_asset(symbol)
            amount = max(0.0, finite(total_map.get(base)))
            if symbol not in prices:
                if amount > 0:
                    raise SafetyError(
                        f"{symbol} 存在持仓余额但无有效价格，无法估值（安全停止）"
                    )
                # 无余额的僵尸币（已停交易/无报价）跳过，不影响主流程
                continue
            price = prices[symbol]
            value = amount * price
            pos_state = state.positions.get(symbol)
            if value > self.risk.dust_quote and pos_state is None:
                unmanaged.append(f"spot:{symbol}={amount}")
            avg_entry = finite(pos_state.avg_entry) if pos_state else 0.0
            high_water = finite(pos_state.high_water) if pos_state else 0.0
            equity += value
            positions[symbol] = AccountPosition(
                symbol, amount, price, value, avg_entry, high_water,
                market=MARKET_SPOT, notional=value,
            )
        # ---- 合约持仓估值（fetch_positions；键 = swap:SYMBOL）----
        if MARKET_SWAP in allowed:
            raw_positions = self._safe_call("fetch_positions", self.client.fetch_positions)
            for raw in raw_positions or []:
                raw_symbol = str(raw.get("symbol", ""))
                base = base_asset(raw_symbol)
                if base not in {base_asset(s) for s in self.runtime.symbols}:
                    continue  # 白名单外的合约仓位不接管（也不估值，避免误算权益）
                contracts = max(0.0, finite(raw.get("contracts"), 0.0))
                side = str(raw.get("side", "long")).lower()
                if contracts <= 0:
                    continue
                if side != "long":
                    if self.risk.futures_allow_short:
                        # 做空支持暂未开放下单路径；若账户出现空头仓位则拒绝自动接管
                        raise SafetyError(f"检测到合约空头仓位 {raw_symbol}，当前版本不支持做空自动管理")
                    raise SafetyError(
                        f"检测到合约空头仓位 {raw_symbol}，但 risk.futures_allow_short=false，"
                        "拒绝自动接管；请人工处理或启用做空配置"
                    )
                key = position_key(MARKET_SWAP, base)
                mark_price = finite(raw.get("markPrice"), 0.0)
                if mark_price <= 0:
                    raise SafetyError(f"{raw_symbol} 合约仓位缺少有效 markPrice，无法估值（安全停止）")
                contract_size = finite(raw.get("contractSize"), 0.0)
                if contract_size <= 0:
                    market_info = self.markets.get(raw_symbol) or {}
                    contract_size = finite(market_info.get("contractSize"), 0.0)
                amount = contracts * max(contract_size, EPSILON)
                notional = finite(raw.get("notional"), amount * mark_price)
                unrealized = finite(raw.get("unrealizedPnl"), 0.0)
                pos_state = state.positions.get(key)
                avg_entry = finite(pos_state.avg_entry) if pos_state else finite(raw.get("entryPrice"), mark_price)
                high_water = finite(pos_state.high_water) if pos_state else avg_entry
                leverage = finite(raw.get("leverage"), 1.0) or 1.0
                liquidation = finite(raw.get("liquidationPrice"), 0.0)
                if notional > self.risk.dust_quote and pos_state is None:
                    unmanaged.append(f"{key} contracts={contracts}")
                equity += unrealized
                positions[key] = AccountPosition(
                    base, amount, mark_price, notional, avg_entry, high_water,
                    market=MARKET_SWAP, side="long", notional=notional,
                    unrealized_pnl=unrealized, leverage=leverage,
                    contracts=contracts, liquidation_price=liquidation,
                )
        if unmanaged and self.risk.reject_unmanaged_positions:
            raise SafetyError(
                "检测到未被状态文件管理的交易所仓位，拒绝自动接管: " + ", ".join(unmanaged)
                + "。确认成本后使用 adopt-positions 命令接管。"
            )
        return AccountSnapshot(equity, quote_free, quote_total, positions, iso_now())

    def has_open_order(self, symbol: str, market: str = MARKET_SPOT) -> bool:
        key = position_key(market, symbol)
        if key in self.state_store.state.pending_orders:
            return True
        exchange_symbol = self.exchange_symbol(market, symbol)
        orders = self._safe_call(
            f"fetch_open_orders:{exchange_symbol}",
            lambda: self.client.fetch_open_orders(exchange_symbol),
        )
        if orders:
            self.log.warning("%s 存在 %d 个交易所挂单，本周期跳过", key, len(orders))
            return True
        return False

    def estimate_vwap(self, symbol: str, side: str, amount: float, fallback_price: float) -> Tuple[float, float]:
        # 订单簿必须用实盘公开行情（market_client）：沙盒 demo 的订单簿是模拟薄盘，
        # 会让 SOL 这类实盘深度充足的币被误判滑点超限。取 100 档评估（OKX 公开
        # 接口支持），避免小市值币在 20 档内被误判"深度不足"。
        book = self._safe_call(
            f"fetch_order_book:{symbol}",
            lambda: self.market_client.fetch_order_book(symbol, 100),
        )
        levels = book.get("asks" if side == "buy" else "bids") or []
        remaining = amount
        cost = 0.0
        filled = 0.0
        for level in levels:
            level_price, level_amount = finite(level[0]), finite(level[1])
            if level_price <= 0 or level_amount <= 0:
                continue
            take = min(remaining, level_amount)
            cost += take * level_price
            filled += take
            remaining -= take
            if remaining <= EPSILON:
                break
        if filled + EPSILON < amount:
            raise SafetyError(f"{symbol} 订单簿深度不足以成交 amount={amount}")
        vwap = cost / max(filled, EPSILON)
        slippage_bps = abs(vwap / fallback_price - 1) * 10000
        return vwap, slippage_bps

    def order_book_fill_info(
        self, symbol: str, side: str, amount: float, fallback_price: float
    ) -> Optional[Dict[str, float]]:
        """实盘订单簿成交能力评估（不抛异常，供 AI 缩量决策使用）。

        返回 100 档内可成交数量/金额、对应滑点、可成交比例；
        订单簿拉取失败或无可成交档位时返回 None。
        """
        try:
            book = self._safe_call(
                f"fetch_order_book:{symbol}",
                lambda: self.market_client.fetch_order_book(symbol, 100),
            )
            levels = book.get("asks" if side == "buy" else "bids") or []
        except Exception:  # noqa: BLE001 评估失败由调用方降级（保守放弃）
            return None
        remaining = amount
        cost = 0.0
        filled = 0.0
        for level in levels:
            level_price, level_amount = finite(level[0]), finite(level[1])
            if level_price <= 0 or level_amount <= 0:
                continue
            take = min(remaining, level_amount)
            cost += take * level_price
            filled += take
            remaining -= take
            if remaining <= EPSILON:
                break
        if filled <= 0:
            return None
        vwap = cost / filled
        slippage_bps = abs(vwap / fallback_price - 1) * 10000 if fallback_price > 0 else 0.0
        return {
            "fillable_amount": filled,
            "fillable_quote": cost,
            "slippage_bps": slippage_bps,
            "fill_ratio": min(1.0, filled / amount) if amount > 0 else 0.0,
        }

    def normalize_amount(self, symbol: str, side: str, amount: float, price: float) -> float:
        if amount <= 0 or price <= 0:
            return 0.0
        try:
            normalized = float(self.client.amount_to_precision(symbol, amount))
        except Exception as exc:
            raise SafetyError(f"{symbol} 数量精度转换失败: {exc}") from exc
        market = self.markets[symbol]
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        min_amount = finite(amount_limits.get("min"), 0.0)
        max_amount = finite(amount_limits.get("max"), float("inf"))
        min_cost = max(self.risk.min_order_quote, finite(cost_limits.get("min"), 0.0))
        max_cost = min(self.risk.max_order_quote, finite(cost_limits.get("max"), float("inf")))
        cost = normalized * price
        if normalized <= 0 or normalized + EPSILON < min_amount or cost + EPSILON < min_cost:
            return 0.0
        if normalized > max_amount or cost > max_cost + EPSILON:
            capped = min(max_amount, max_cost / price)
            normalized = float(self.client.amount_to_precision(symbol, capped))
        return max(0.0, normalized)

    def plan_rebalance(
        self,
        snapshot: AccountSnapshot,
        symbol: str,
        target_allocation: float,
        reason: str,
        market: str = MARKET_SPOT,
        leverage: float = 1.0,
    ) -> Optional[OrderPlan]:
        if snapshot.equity <= 0:
            raise SafetyError("账户权益不大于 0")
        key = position_key(market, symbol)
        position = snapshot.positions.get(key)
        if position is None:
            # 开新仓：账户尚无该市场持仓。目标分配>0 时用最新价构造零持仓位置；
            # 否则无仓可减，返回 None。
            if target_allocation <= 0:
                return None
            try:
                price = self.fetch_last_price(symbol, market)
            except Exception as exc:
                raise SafetyError(f"{key} 无有效价格，无法开新仓: {exc}") from exc
            position = AccountPosition(
                symbol, 0.0, price, 0.0, 0.0, 0.0,
                market=market, notional=0.0,
            )
        leverage = clamp(leverage, 1.0, self.risk.max_leverage)
        if market == MARKET_SWAP:
            return self._plan_swap(snapshot, symbol, position, target_allocation, reason, leverage)
        return self._plan_spot(snapshot, symbol, position, target_allocation, reason)

    def _plan_spot(
        self,
        snapshot: AccountSnapshot,
        symbol: str,
        position: AccountPosition,
        target_allocation: float,
        reason: str,
    ) -> Optional[OrderPlan]:
        current_quote = position.quote_value
        allocation_cap = 1.0 if self.config.ai.decision_mode == "full" else self.risk.max_symbol_allocation
        desired_quote = snapshot.equity * clamp(target_allocation, 0, allocation_cap)
        delta_quote = desired_quote - current_quote
        current_allocation = current_quote / snapshot.equity
        min_delta = max(self.risk.min_rebalance_quote, snapshot.equity * self.risk.min_rebalance_pct)
        if abs(delta_quote) < min_delta:
            return None
        side = "buy" if delta_quote > 0 else "sell"
        if side == "buy":
            spendable = max(0.0, snapshot.quote_free - snapshot.equity * self.risk.cash_reserve_pct)
            delta_quote = min(delta_quote, spendable, self.risk.max_order_quote)
            amount = delta_quote / position.price
        else:
            amount = min(position.amount, abs(delta_quote) / position.price)
        amount = self.normalize_amount(symbol, side, amount, position.price)
        if amount <= 0:
            return None
        estimated_quote = amount * position.price
        return OrderPlan(
            symbol=symbol,
            side=side,
            amount=amount,
            reference_price=position.price,
            estimated_quote=estimated_quote,
            target_allocation=target_allocation,
            current_allocation=current_allocation,
            reason=reason,
            market=MARKET_SPOT,
        )

    def _plan_swap(
        self,
        snapshot: AccountSnapshot,
        symbol: str,
        position: AccountPosition,
        target_allocation: float,
        reason: str,
        leverage: float,
    ) -> Optional[OrderPlan]:
        """合约（永续）调仓：按名义敞口（notional）计算，long-only。

        - desired_notional = equity × target_allocation（受 max_symbol_allocation 钳制）
        - 只做多：sell 最多平到 0，不反向开空（futures_allow_short 预留）
        - 硬顶：单笔名义 ≤ max_futures_notional_pct×equity；保证金 = 名义/杠杆 ≤ futures_margin_cap_pct×equity
        - amount 以张（contracts）为单位
        """
        exchange_symbol = self.exchange_symbol(MARKET_SWAP, symbol)
        market_info = self.markets.get(exchange_symbol) or {}
        if not market_info.get("swap"):
            raise SafetyError(
                f"{exchange_symbol} 无永续合约市场，无法生成合约计划（请确认 allowed_markets 与交易所市场）"
            )
        contract_size = finite(market_info.get("contractSize"), 1.0)
        current_notional = position.notional  # long 为正
        allocation_cap = 1.0 if self.config.ai.decision_mode == "full" else self.risk.max_symbol_allocation
        desired_notional = snapshot.equity * clamp(target_allocation, 0, allocation_cap)
        delta_notional = desired_notional - current_notional
        current_allocation = current_notional / snapshot.equity
        min_delta = max(self.risk.min_rebalance_quote, snapshot.equity * self.risk.min_rebalance_pct)
        if abs(delta_notional) < min_delta:
            return None
        side = "buy" if delta_notional > 0 else "sell"
        if side == "sell":
            # long-only：最多平到 0
            delta_notional = max(delta_notional, -current_notional)
            if abs(delta_notional) < min_delta:
                return None
        # 硬风控顶（full 模式放开：AI 全权决定合约名义敞口与保证金占用）
        if self.config.ai.decision_mode == "full":
            notional_cap = snapshot.equity  # 名义敞口不设额外上限
        else:
            notional_cap = snapshot.equity * self.risk.max_futures_notional_pct
        if abs(delta_notional) > notional_cap + EPSILON:
            delta_notional = math.copysign(notional_cap, delta_notional)
        margin_required = abs(delta_notional) / leverage
        if (
            self.config.ai.decision_mode != "full"
            and margin_required > snapshot.equity * self.risk.futures_margin_cap_pct + EPSILON
        ):
            raise SafetyError(
                f"{exchange_symbol} 合约保证金需求 {margin_required:.2f} USDT 超过上限 "
                f"{snapshot.equity * self.risk.futures_margin_cap_pct:.2f} USDT "
                f"(notional={abs(delta_notional):.2f}, leverage={leverage:.1f})"
            )
        contracts = abs(delta_notional) / position.price / contract_size
        contracts = self._normalize_contracts(exchange_symbol, contracts)
        if contracts <= 0:
            return None
        estimated_quote = contracts * contract_size * position.price
        # 最小量校验：estimated_quote 不得低于 min_order_quote（与现货一致），
        # 低于则放弃该计划（避免 OKX 51020 最小量不足）。
        if estimated_quote < self.risk.min_order_quote:
            return None
        return OrderPlan(
            symbol=symbol,
            side=side,
            amount=contracts,  # 合约订单量以张为单位
            reference_price=position.price,
            estimated_quote=estimated_quote,
            target_allocation=target_allocation,
            current_allocation=current_allocation,
            reason=reason,
            market=MARKET_SWAP,
            leverage=leverage,
            contracts=contracts,
        )

    def _normalize_contracts(self, exchange_symbol: str, contracts: float) -> float:
        if contracts <= 0:
            return 0.0
        try:
            normalized = float(self.client.amount_to_precision(exchange_symbol, contracts))
        except Exception as exc:
            raise SafetyError(f"{exchange_symbol} 张数精度转换失败: {exc}") from exc
        if normalized <= 0:
            return 0.0
        return normalized

    def execute(self, plan: OrderPlan) -> FillResult:
        key = position_key(plan.market, plan.symbol)
        exchange_symbol = self.exchange_symbol(plan.market, plan.symbol)
        if self.has_open_order(plan.symbol, plan.market):
            raise SafetyError(f"{key} 存在未完成订单，拒绝重复下单")
        vwap, slippage_bps = self.estimate_vwap(
            exchange_symbol, plan.side, plan.amount, plan.reference_price
        )
        if slippage_bps > self.risk.max_slippage_bps:
            raise SafetyError(
                f"{exchange_symbol} 预计滑点 {slippage_bps:.2f}bps 超过上限 {self.risk.max_slippage_bps:.2f}bps"
            )
        client_id = stable_client_order_id(self.exchange_cfg.id, exchange_symbol, plan.side)
        params: Dict[str, Any] = {}
        if self.exchange_cfg.client_order_id_param:
            params[self.exchange_cfg.client_order_id_param] = client_id
        if plan.market == MARKET_SWAP:
            # 合约：组合保证金账户（acctLv=3）+ 对冲模式（posMode=long_short_mode）下单必须带 posSide。
            # 当前版本 long-only：买入=开多/加多，卖出=平多，posSide 恒为 long。
            if self.exchange_cfg.id != "okx":
                raise SafetyError("合约市场仅适配 OKX")
            # 杠杆设置是保证金计算的前提：失败即中止，避免按错误杠杆下单。
            try:
                self._safe_call(
                    f"set_leverage:{exchange_symbol}",
                    lambda: self.client.set_leverage(
                        plan.leverage, exchange_symbol, {"mgnMode": "cross", "posSide": "long"}
                    ),
                )
            except Exception as exc:
                raise SafetyError(f"{exchange_symbol} 设置杠杆 {plan.leverage}x 失败: {exc}") from exc
            params["marginMode"] = "cross"
            params["posSide"] = "long"
        elif self.exchange_cfg.id == "okx" and self.exchange_cfg.market_type == "spot":
            # OKX 跨币种保证金账户（acctLv=3，经 GET /api/v5/account/config 实测）只接受
            # tdMode=cross 的现货单：
            #   1) ccxt 4.5.71 对现货默认 tdMode=cash -> OKX 拒绝 51000 "Parameter tdMode error"
            #   2) ccxt 自动附加 tgtCcy=base_ccy 在该账户模式下同样触发 51000
            # 传 marginMode=cross 让 ccxt 走杠杆分支：tdMode=cross 且不再附加 tgtCcy。
            # 实测（2026-08-07 demo）：加此参数下单成功，去掉必现 51000。
            params["marginMode"] = "cross"
            # 3) cross 模式下 OKX 把市价买单的 sz 默认解释为 quote(USDT) 金额，
            #    导致"计划买 89.4 AAVE"实际只买 89.4 USDT（成交缩水 ~90 倍）。
            #    显式声明 tgtCcy=base_ccy，让 sz 按 base(AAVE) 数量解释。
            #    实测：cross + tgtCcy=base_ccy + sz=2 -> 全额成交 2 AAVE（cost=178.46 USDT）；
            #          不加 tgtCcy -> 只成交 2 USDT 等值。ccxt 第 261 行 extend 会把该参数透传。
            params["tgtCcy"] = "base_ccy"
        state = self.state_store.state
        # Write-ahead intent：必须先落盘再发请求。进程若在请求期间崩溃，重启后会停机人工核对，
        # 绝不能把“未收到响应”当作“订单未提交”并自动重试。
        state.pending_orders[key] = {
            "id": "",
            "client_id": client_id,
            "side": plan.side,
            "amount": plan.amount,
            "created_at": iso_now(),
            "stage": "submitting",
            "uncertain": False,
            "market": plan.market,
            "exchange_symbol": exchange_symbol,
        }
        self.state_store.save()
        try:
            # 下单属于非幂等副作用，禁止使用带自动重试的 _safe_call。
            order = self.client.create_order(
                exchange_symbol, "market", plan.side, plan.amount, None, params
            )
        except self._network_error_types() as exc:
            message = f"{key} 下单请求发生网络/超时异常，结果未知: {exc}"
            state.pending_orders[key]["uncertain"] = True
            state.pending_orders[key]["submit_error"] = repr(exc)
            state.halted_reason = "ORDER_UNCERTAIN: " + message
            self.state_store.save()
            raise OrderUncertainError(message + "；禁止自动重试，必须人工核对") from exc
        except Exception:
            # 非网络类错误按交易所明确拒绝处理；清理尚未提交的本地 intent。
            state.pending_orders.pop(key, None)
            self.state_store.save()
            raise
        if not isinstance(order, Mapping):
            message = f"{key} 下单响应不是 JSON object，订单结果未知"
            state.pending_orders[key]["uncertain"] = True
            state.pending_orders[key]["response_type"] = type(order).__name__
            state.halted_reason = "ORDER_UNCERTAIN: " + message
            self.state_store.save()
            raise OrderUncertainError(message + "；必须人工核对交易所")
        order_id = str(order.get("id", "")).strip()
        if not order_id:
            message = f"{key} 下单响应缺少 order id，订单结果未知"
            state.pending_orders[key]["uncertain"] = True
            state.pending_orders[key]["response_client_id"] = order.get("clientOrderId")
            state.halted_reason = "ORDER_UNCERTAIN: " + message
            self.state_store.save()
            raise OrderUncertainError(message + "；禁止自动重下，必须人工核对")
        state.pending_orders[key].update({
            "id": order_id,
            "stage": "accepted",
            "uncertain": False,
        })
        self.state_store.save()
        final_order = self._wait_for_order(exchange_symbol, order_id, order)
        result = self._parse_fill(plan, final_order)
        if result.status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
            state.pending_orders.pop(key, None)
        if result.filled_amount > 0:
            self._apply_fill(result)
        else:
            self.state_store.save()
        return result

    def _wait_for_order(self, symbol: str, order_id: str, initial: Mapping[str, Any]) -> Dict[str, Any]:
        order = dict(initial)
        deadline = time.monotonic() + self.risk.order_fill_timeout_seconds
        while time.monotonic() < deadline:
            status = str(order.get("status", "")).lower()
            if status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
                return order
            time.sleep(1.0)
            try:
                order = self._safe_call(
                    f"fetch_order:{order_id}",
                    lambda: self.client.fetch_order(order_id, symbol),
                )
            except getattr(ccxt, "OrderNotFound"):
                continue
        self.log.warning("订单等待超时，保留 pending 状态供下周期对账: %s %s", symbol, order_id)
        return order

    def _parse_fill(self, plan: OrderPlan, order: Mapping[str, Any]) -> FillResult:
        filled = finite(order.get("filled"), 0.0)
        average = finite(order.get("average"), 0.0)
        cost = finite(order.get("cost"), 0.0)
        if average <= 0 and filled > 0 and cost > 0:
            average = cost / filled
        if average <= 0:
            average = plan.reference_price
        # 合约订单的 filled 以张为单位；统一换算为 base 数量（amount=contracts×contractSize）。
        # 否则 _apply_fill 会把 1.23 张当成 1.23 BTC 累加，仓位估值放大 ~100 倍。
        if plan.market == MARKET_SWAP and filled > 0:
            market_info = self.markets.get(plan.symbol) or {}
            contract_size = finite(market_info.get("contractSize"), 0.0)
            if contract_size <= 0:
                market_info = self.markets.get(self.exchange_symbol(MARKET_SWAP, plan.symbol)) or {}
                contract_size = finite(market_info.get("contractSize"), 0.0)
            if contract_size <= 0:
                raise SafetyError(
                    f"{self.exchange_symbol(MARKET_SWAP, plan.symbol)} 缺少 contractSize，无法换算成交张数"
                )
            filled = filled * contract_size
        fee_quote = 0.0
        fee = order.get("fee") or {}
        if str(fee.get("currency", "")).upper() == self.runtime.quote_currency:
            fee_quote += finite(fee.get("cost"))
        for item in order.get("fees") or []:
            if str(item.get("currency", "")).upper() == self.runtime.quote_currency:
                fee_quote += finite(item.get("cost"))
        return FillResult(
            order_id=str(order.get("id", "")),
            symbol=plan.symbol,
            side=plan.side,
            requested_amount=plan.amount,
            filled_amount=filled,
            average_price=average,
            fee_quote=fee_quote,
            status=str(order.get("status", "unknown")).lower(),
            raw={"clientOrderId": order.get("clientOrderId")},
            market=plan.market,
            leverage=plan.leverage,
        )

    def _apply_fill(self, fill: FillResult) -> None:
        state = self.state_store.state
        key = position_key(fill.market, fill.symbol)
        pos = state.positions.get(key, PositionState())
        pos.market = fill.market
        pos.side = "long"
        pos.leverage = fill.leverage if fill.market == MARKET_SWAP else 1.0
        amount = max(0.0, fill.filled_amount)
        if fill.side == "buy":
            gross = pos.amount * pos.avg_entry + amount * fill.average_price + fill.fee_quote
            new_amount = pos.amount + amount
            pos.avg_entry = gross / max(new_amount, EPSILON)
            pos.amount = new_amount
            pos.high_water = max(pos.high_water, fill.average_price)
            if not pos.opened_at:
                pos.opened_at = iso_now()
        else:
            sold = min(pos.amount, amount)
            pos.amount = max(0.0, pos.amount - sold)
            if pos.amount * fill.average_price <= self.risk.dust_quote:
                pos = PositionState()
        pos.updated_at = iso_now()
        if pos.amount > 0:
            state.positions[key] = pos
        else:
            state.positions.pop(key, None)
        state.last_trade_at[key] = iso_now()
        self._roll_trade_day()
        state.trades_today += 1
        self.state_store.save()

    def _roll_trade_day(self) -> None:
        state = self.state_store.state
        today = utc_now().date().isoformat()
        if state.trade_day != today:
            state.trade_day = today
            state.trades_today = 0

    def update_high_water(self, snapshot: AccountSnapshot) -> None:
        changed = False
        state = self.state_store.state
        for symbol, account_pos in snapshot.positions.items():
            if account_pos.amount <= 0:
                continue
            pos = state.positions.get(symbol)
            if pos and account_pos.price > pos.high_water:
                pos.high_water = account_pos.price
                pos.updated_at = iso_now()
                changed = True
        if changed:
            self.state_store.save()

    def adopt_positions(self, prices: Mapping[str, float], entries: Mapping[str, float]) -> List[str]:
        balance = self._live_balance()
        total_map = balance.get("total") or {}
        adopted: List[str] = []
        for symbol in self.runtime.symbols:
            amount = max(0.0, finite(total_map.get(base_asset(symbol))))
            value = amount * prices[symbol]
            if value <= self.risk.dust_quote:
                continue
            entry = finite(entries.get(symbol), 0.0)
            if entry <= 0:
                raise ConfigError(f"接管 {symbol} 时必须通过 --entry 提供正数成本价")
            self.state_store.state.positions[symbol] = PositionState(
                amount=amount,
                avg_entry=entry,
                high_water=max(entry, prices[symbol]),
                opened_at=iso_now(),
                updated_at=iso_now(),
            )
            adopted.append(symbol)
        self.state_store.save()
        return adopted


# =============================================================================
# 风控与 AI 否决器
# =============================================================================


@dataclass
class RiskStatus:
    allowed: bool
    reason: str


class RiskManager:
    def __init__(self, config: RiskConfig, store: StateStore, logger: logging.Logger):
        self.config = config
        self.store = store
        self.log = logger

    def evaluate(self, snapshot: AccountSnapshot) -> RiskStatus:
        state = self.store.state
        equity = snapshot.equity
        if equity <= 0 or not math.isfinite(equity):
            return RiskStatus(False, "账户权益无效")
        today = utc_now().date().isoformat()
        if state.trade_day != today:
            state.trade_day = today
            state.trades_today = 0
            state.day_start_equity = equity
            state.halted_reason = ""
        if state.day_start_equity <= 0:
            state.day_start_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        state.last_equity = equity

        daily_loss = max(0.0, 1 - equity / max(state.day_start_equity, EPSILON))
        drawdown = max(0.0, 1 - equity / max(state.peak_equity, EPSILON))
        reason = ""
        if daily_loss >= self.config.max_daily_loss_pct:
            reason = f"日内亏损 {daily_loss:.2%} 达到上限 {self.config.max_daily_loss_pct:.2%}"
        elif drawdown >= self.config.max_drawdown_pct:
            reason = f"峰值回撤 {drawdown:.2%} 达到上限 {self.config.max_drawdown_pct:.2%}"
        elif state.trades_today >= self.config.max_trades_per_day:
            reason = f"当日成交次数达到上限 {self.config.max_trades_per_day}"
        if reason:
            state.halted_reason = reason
        self.store.save()
        # 组合级熔断禁止新开仓，但保护性平仓仍允许，由控制器单独处理。
        return RiskStatus(not bool(reason), reason)

    def protective_exits(self, snapshot: AccountSnapshot) -> Dict[str, str]:
        exits: Dict[str, str] = {}
        state = self.store.state
        for symbol, account_pos in snapshot.positions.items():
            if account_pos.amount <= 0:
                continue
            pos = state.positions.get(symbol)
            if not pos or pos.avg_entry <= 0:
                continue
            price = account_pos.price
            entry = pos.avg_entry
            high_water = max(pos.high_water, price)
            pnl = price / entry - 1
            if pnl <= -self.config.stop_loss_pct:
                exits[symbol] = f"stop_loss pnl={pnl:.2%}"
            elif pnl >= self.config.take_profit_pct:
                exits[symbol] = f"take_profit pnl={pnl:.2%}"
            elif (
                high_water >= entry * (1 + self.config.trailing_activation_pct)
                and price <= high_water * (1 - self.config.trailing_stop_pct)
            ):
                draw_from_high = price / high_water - 1
                exits[symbol] = f"trailing_stop from_high={draw_from_high:.2%}"
        return exits

    def cooldown_ok(self, symbol: str) -> Tuple[bool, str]:
        last = parse_iso(self.store.state.last_trade_at.get(symbol))
        if not last:
            return True, ""
        elapsed = (utc_now() - last).total_seconds()
        if elapsed < self.config.cooldown_seconds:
            return False, f"cooldown {elapsed:.0f}/{self.config.cooldown_seconds}s"
        return True, ""


@dataclass
class AncientMethodReading:
    bias: str
    confidence: float
    reading: str


@dataclass
class AIDecision:
    action: str
    target_level: str
    confidence: float
    summary: str
    readings: Dict[str, AncientMethodReading]
    conflicts: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    enabled: bool = True
    fallback: bool = False
    # AI 建议的下一次行情复查间隔（分钟）；None 表示未提供，由系统用默认轮询间隔。
    next_review_minutes: Optional[int] = None
    # 多市场支持：AI 自主选择现货/合约（仅限配置允许范围）与合约杠杆。
    market: str = MARKET_SPOT
    leverage: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AIAdvisor:
    """逐项解读十大技术因子，并把叙述转译为受规则与风控约束的交易目标。"""

    def __init__(
        self,
        config: AIConfig,
        logger: logging.Logger,
        credentials: Optional[CredentialStore] = None,
    ):
        self.config = config
        self.log = logger
        self.credentials = credentials
        self.client: Any = None
        self.last_error: str = ""  # 最近一次 AI 失败的可读原因（供日志/控制台排查）
        # AI 熔断器：连续失败达阈值后暂停 AI 请求，避免上游 503 时一波请求打爆
        # 自身限频（一次 tick 评估 54 币 × 内部 5 次重试 = 270 次请求）。
        self._fail_streak = 0
        self._circuit_open_until = 0.0
        if not config.enabled:
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError("启用 AI 需要安装 openai") from exc
        key = (
                self.credentials.resolve_ai_key(config.api_key_name, required=True)
                if config.api_key_name
                else secret_from_env(
                    config.api_key_env, required=True,
                    credentials=self.credentials, purpose="AI API",
                )
            )
        kwargs: Dict[str, Any] = {
            "api_key": key,
            "timeout": config.timeout_seconds,
            # 禁止 SDK 对超时进行隐式重试：避免交易周期被成倍阻塞及重复计费请求。
            # AI 失败由 fail_closed 安全回退处理；JSON 格式修复仍只在收到正文后执行一次。
            "max_retries": 0,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = OpenAI(**kwargs)

    @staticmethod
    def _response_template() -> Dict[str, Any]:
        return {
            "action": "HOLD",
            "target_level": "UNCHANGED",
            "confidence": 0.0,
            "market": "spot",
            "leverage": 1.0,
            "summary": "中文总结",
            "next_review_minutes": 60,
            "readings": {
                name: {
                    "bias": "neutral",
                    "confidence": 0.0,
                    "reading": f"{name}的中文技术解读",
                }
                for name in STRATEGY_NAMES
            },
            "conflicts": [],
            "risk_notes": ["不保证未来收益"],
        }

    @classmethod
    def _response_contract(cls) -> str:
        template = json.dumps(cls._response_template(), ensure_ascii=False, separators=(",", ":"))
        return (
            "输出协议是强制接口契约：只能输出一个 JSON object，不得输出 Markdown、代码块、解释、前后缀或思维过程。"
            "顶层必须且只能包含 action、target_level、confidence、market、leverage、summary、next_review_minutes、readings、conflicts、risk_notes。"
            "action 必须是 JSON string，并且只能精确等于 BUY、SELL、HOLD 之一；不得为 null、数字或对象。"
            "target_level 必须是 JSON string，并且只能精确等于 FLAT、HALF、FULL、UNCHANGED 之一。"
            "market 必须是 JSON string，只能精确等于 spot 或 swap（永续合约）；只能从输入给出的 allowed_markets 中选择，"
            "未给出 swap 时只能选 spot。看多且希望放大敞口才选 swap，并同时给出 1..max_leverage 的 leverage；"
            "方向不确定或盘面偏弱时必须选 spot（现货不放大风险）。"
            "leverage 必须是 1 到 50 的 JSON number；现货市场必须为 1。"
            "confidence 必须是 0 到 1 的 JSON number；summary 必须是 JSON string。"
            "next_review_minutes 必须是 1 到 360 的 JSON integer，表示你建议的下一次行情复查间隔（分钟）："
            "波动剧烈、持仓重或方向不确定时用短间隔（如 5-30），市场平淡、空仓且无信号时可用长间隔（如 120-360）；"
            "无法判断时必须用 60，不得缺失。"
            "readings 必须是 JSON object，必须完整且只包含奇门、六壬、太乙、易经、风水、八字、梅花、紫微、八卦、四柱。"
            "每个 readings 项必须且只能包含 bias、confidence、reading；bias 只能是 bullish、bearish、neutral；"
            "reading 必须是该古法盘面的断卦解读（如体用生克、三传与日干关系、值符吉门、命宫主星、日主旺衰等），"
            "不得泛泛重复 value 数值。"
            "conflicts 与 risk_notes 必须是 JSON string array。不得改字段名，不得把字段放入 decision、result、data 等嵌套对象。"
            "若无法确定交易动作，必须使用 action=HOLD、target_level=UNCHANGED、market=spot、leverage=1、低 confidence，仍须完整填写十项 readings。"
            f"严格结构示例（内容应根据输入重写，但结构和字段名不得改变）：{template}"
        )

    @staticmethod
    def _relay_error(exc: Exception) -> AIRelayError:
        """把 openai SDK / 传输层异常转成带状态与错误摘要的 AIRelayError。"""
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        detail = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message", ""))
            elif isinstance(err, str):
                detail = err
            if not detail:
                try:
                    detail = json.dumps(body, ensure_ascii=False)[:300]
                except Exception:
                    detail = str(body)[:300]
        elif isinstance(body, str):
            detail = body[:300]
        status_text = f"HTTP {status}" if status else "传输层"
        message = f"AI 中转站错误（{status_text}）: {detail or exc}"
        return AIRelayError(message, status=status, detail=detail)

    def _completion_content(self, messages: Sequence[Mapping[str, str]]) -> str:
        """请求 AI 并返回非空正文；空正文/网络抖动自动重试（最多 5 次）。

        对 reasoning 型模型（如 deepseek-v4-flash），中转站偶发把输出预算
        耗尽在思考上、返回空 content（finish=length）。重试时依次去掉
        response_format / reasoning_effort 并放宽 max_tokens，最大化成功概率。
        503/429/5xx 瞬时繁忙指数退避重试；4xx 配置类错误不重试直接抛。
        """
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.config.reasoning_effort:
            # 思考档位：low 可显著降低 reasoning 模型（如 deepseek-v4-flash）
            # 把输出预算耗尽在思考上导致空正文的概率。
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        last_exc: Optional[BaseException] = None
        for attempt in range(5):
            try:
                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if isinstance(content, str) and content.strip():
                    return content.strip()
                last_exc = ConfigError("AI 响应正文必须是非空 JSON string")
            except Exception as exc:  # noqa: BLE001 - SDK/网络/中转站错误统一转成可读信息
                last_exc = self._relay_error(exc)
            # 配置类错误（401/400/404 等）重试无意义，立即失败；429/503/5xx 属瞬时繁忙继续重试。
            if (isinstance(last_exc, AIRelayError) and last_exc.status
                    and 400 <= last_exc.status < 500 and last_exc.status != 429):
                raise last_exc
            if attempt < 4:
                self.log.warning(
                    "AI 响应为空或请求失败（第 %d 次），调整参数重试: %s",
                    attempt + 1, last_exc,
                )
                time.sleep(0.8 * (2 ** attempt))  # 0.8/1.6/3.2/6.4/12.8 指数退避
                if attempt == 0:
                    kwargs.pop("response_format", None)
                elif attempt == 1:
                    kwargs.pop("reasoning_effort", None)
                    kwargs["max_tokens"] = min(
                        int(kwargs.get("max_tokens", 3000)) + 1000, 4000
                    )
                # 后续尝试保持宽松参数继续重试（503 瞬时繁忙需要更长退避）
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _target_level(target: float, current_fraction: float) -> str:
        target = clamp(target)
        current = clamp(current_fraction)
        if abs(target - current) <= 0.05:
            return "UNCHANGED"
        if target <= 0.05:
            return "FLAT"
        if target <= 0.75:
            return "HALF"
        return "FULL"

    @staticmethod
    def _action_for_target(target: float, current_fraction: float) -> str:
        if target > current_fraction + 0.05:
            return "BUY"
        if target < current_fraction - 0.05:
            return "SELL"
        return "HOLD"

    @staticmethod
    def _bias_from_value(value: float) -> str:
        if value >= 0.58:
            return "bullish"
        if value <= 0.42:
            return "bearish"
        return "neutral"

    def rule_fallback(
        self,
        result: SignalResult,
        rule_target: float,
        current_fraction: float,
        reason: str,
        error: str = "",
    ) -> AIDecision:
        readings: Dict[str, AncientMethodReading] = {}
        for name in STRATEGY_NAMES:
            value = clamp(result.signals[name])
            bias = self._bias_from_value(value)
            confidence = clamp(abs(value - 0.5) * 2)
            readings[name] = AncientMethodReading(
                bias=bias,
                confidence=confidence,
                reading=f"{ANCIENT_METHOD_DESCRIPTIONS[name]} 当前归一化值={value:.3f}，规则判定={bias}。",
            )
        summary = f"规则引擎目标={self._target_level(rule_target, current_fraction)}；{reason}"
        notes = ["AI 未启用，使用确定性规则引擎。"]
        if error:
            notes = [f"AI 解读失败，按 fail_closed 使用安全回退：{error[:240]}"]
        return AIDecision(
            action=self._action_for_target(rule_target, current_fraction),
            target_level=self._target_level(rule_target, current_fraction),
            confidence=clamp(abs(result.score - 0.5) * 2),
            summary=summary,
            readings=readings,
            risk_notes=notes,
            enabled=self.config.enabled,
            fallback=True,
        )

    @staticmethod
    def _parse_string_list(value: Any, path: str) -> List[str]:
        items = require_json_array(value, path)
        output: List[str] = []
        for index, item in enumerate(items):
            text = require_json_string(item, f"{path}[{index}]").strip()
            if text:
                output.append(text[:300])
        return output[:10]

    def _parse_decision(self, content: str, require_readings: bool = True) -> AIDecision:
        parsed = require_json_object(json.loads(content), "AI response")
        required_fields = {
            "action", "target_level", "confidence", "summary", "conflicts", "risk_notes",
        }
        if require_readings:
            required_fields.add("readings")
        allowed_fields = required_fields | {"next_review_minutes", "market", "leverage"}
        if not require_readings:
            # 拆分模式的聚合响应：模型可能回显 readings，允许但忽略（已单独获取）
            allowed_fields.add("readings")
        reject_unknown(parsed, allowed_fields, "AI response")
        missing_fields = sorted(required_fields - set(parsed))
        if missing_fields:
            raise ConfigError(f"AI response 缺少顶层字段: {missing_fields}")
        action = require_json_string(parsed.get("action"), "AI response.action").strip().upper()
        target_level = require_json_string(
            parsed.get("target_level"), "AI response.target_level"
        ).strip().upper()
        if action not in AI_ACTIONS:
            raise ConfigError(f"AI response.action 必须是 {sorted(AI_ACTIONS)}")
        if target_level not in AI_TARGET_LEVELS:
            raise ConfigError(f"AI response.target_level 必须是 {sorted(AI_TARGET_LEVELS)}")
        confidence = require_json_number(parsed.get("confidence"), "AI response.confidence")
        if not 0 <= confidence <= 1:
            raise ConfigError("AI response.confidence 必须在 0..1")
        summary = require_json_string(parsed.get("summary"), "AI response.summary").strip()[:500]
        readings: Dict[str, AncientMethodReading] = {}
        if require_readings:
            raw_readings = require_json_object(parsed.get("readings"), "AI response.readings")
            reject_unknown(raw_readings, STRATEGY_NAMES, "AI response.readings")
            missing = set(STRATEGY_NAMES) - set(raw_readings)
            if missing:
                raise ConfigError(f"AI response.readings 缺少古法项: {sorted(missing)}")
            for name in STRATEGY_NAMES:
                item = require_json_object(raw_readings[name], f"AI response.readings.{name}")
                reject_unknown(item, {"bias", "confidence", "reading"}, f"AI response.readings.{name}")
                bias = require_json_string(
                    item.get("bias"), f"AI response.readings.{name}.bias"
                ).strip().lower()
                if bias not in AI_BIASES:
                    raise ConfigError(f"AI response.readings.{name}.bias 无效")
                item_confidence = require_json_number(
                    item.get("confidence"), f"AI response.readings.{name}.confidence"
                )
                if not 0 <= item_confidence <= 1:
                    raise ConfigError(f"AI response.readings.{name}.confidence 必须在 0..1")
                reading = require_json_string(
                    item.get("reading"), f"AI response.readings.{name}.reading"
                ).strip()[:400]
                readings[name] = AncientMethodReading(bias, item_confidence, reading)
        conflicts = self._parse_string_list(parsed["conflicts"], "AI response.conflicts")
        risk_notes = self._parse_string_list(parsed["risk_notes"], "AI response.risk_notes")
        next_review_minutes: Optional[int] = None
        raw_next = parsed.get("next_review_minutes")
        if raw_next is not None:
            if type(raw_next) is not int or not (1 <= raw_next <= 360):
                raise ConfigError("AI response.next_review_minutes 必须是 1..360 的 JSON integer")
            next_review_minutes = raw_next
        market = MARKET_SPOT
        raw_market = parsed.get("market")
        if raw_market is not None:
            market = require_json_string(raw_market, "AI response.market").strip().lower()
            if market not in AI_MARKETS:
                raise ConfigError(f"AI response.market 必须是 {sorted(AI_MARKETS)}")
        leverage = 1.0
        raw_leverage = parsed.get("leverage")
        if raw_leverage is not None:
            leverage = require_json_number(raw_leverage, "AI response.leverage")
            if not 1.0 <= leverage <= 50:
                raise ConfigError("AI response.leverage 必须在 1..50")
            if market == MARKET_SPOT and abs(leverage - 1.0) > 1e-9:
                leverage = 1.0  # 现货强制无杠杆，不因模型幻觉放大风险
        return AIDecision(
            action, target_level, confidence, summary, readings, conflicts, risk_notes,
            next_review_minutes=next_review_minutes,
            market=market,
            leverage=leverage,
        )

    def _parse_with_one_format_repair(
        self, content: str, require_readings: bool = True
    ) -> Tuple[AIDecision, bool]:
        """严格解析；仅『非空正文且结构无效』时请求一次格式修复。

        空正文（余额不足/中转站异常常表现为空响应）与 AIRelayError
        （传输层/HTTP 错误）直接抛出不修复——重试只会继续失败或继续扣费。
        require_readings=False 用于拆分模式的聚合响应（readings 已单独获取）。
        """
        if not isinstance(content, str) or not content.strip():
            raise ConfigError("AI 响应正文必须是非空 JSON string")
        try:
            return self._parse_decision(content, require_readings=require_readings), False
        except AIRelayError:
            raise
        except (ConfigError, json.JSONDecodeError) as parse_exc:
            self.log.warning("AI 首次响应结构无效，尝试一次格式修复: %s", parse_exc)
            repair_system = (
                "你是 JSON 接口格式修复器。只能修复字段结构与 JSON 类型，不得重新分析行情，"
                "不得改变原响应中可识别的交易方向、目标、置信度或解读含义。"
                "若原响应没有可识别的合法 action，必须使用 HOLD；若没有合法 target_level，必须使用 UNCHANGED。"
                "缺失的十项 readings 必须基于原响应已有文字补齐；无法恢复时使用 neutral、0、‘原响应缺失该项’。"
                + self._response_contract()
            )
            try:
                repaired = self._completion_content([
                    {"role": "system", "content": repair_system},
                    {"role": "user", "content": json.dumps({
                        "validation_error": str(parse_exc),
                        "invalid_response": content,
                    }, ensure_ascii=False)},
                ])
            except AIRelayError:
                raise  # 修复请求同样遇到中转站错误（如余额不足），直接上抛，不再尝试解析
            return self._parse_decision(repaired), True

    def schema_check(self) -> Dict[str, Any]:
        """只测试 AI 接口与严格响应结构；使用合成数据，不连接交易所。"""
        if not self.config.enabled:
            raise ConfigError("ai-check 要求 ai.enabled=true；请先运行 setup 启用 AI")
        synthetic_request = {
            "test_only": True,
            "notice": "这是接口结构测试，不是实时行情，不得视为投资建议或交易信号。",
            "symbol": "SCHEMA/TEST",
            "aggregate_score": 0.5,
            "rule_target_level": "UNCHANGED",
            "rule_target_fraction": 0.0,
            "rule_reason": "AI Schema 自检",
            "current_fraction": 0.0,
            "position": {
                "amount": 0.0,
                "quote_value": 0.0,
                "average_entry": 0.0,
                "high_water": 0.0,
                "account_equity": 0.0,
            },
            "methods": {
                name: {
                    "value": 0.5,
                    "weight": 0.1,
                    "meaning": ANCIENT_METHOD_DESCRIPTIONS[name],
                }
                for name in STRATEGY_NAMES
            },
            "diagnostics": {"synthetic": True},
        }
        system = (
            "你正在执行 GuFaQuant-Pro 的 AI Schema 自检。输入全部是合成数据，不代表任何市场，"
            "不得输出真实投资建议。请保守输出 HOLD 与 UNCHANGED，并逐项填写十项 readings。"
            + self._response_contract()
        )
        content = self._completion_content([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(synthetic_request, ensure_ascii=False)},
        ])
        decision, repaired = self._parse_with_one_format_repair(content)
        return {
            "ok": True,
            "test": "AI_SCHEMA_TEST",
            "model": self.config.model,
            "base_url": self.config.base_url or "https://api.openai.com/v1",
            "format_repair_used": repaired,
            "exchange_connected": False,
            "orders_possible": False,
            "decision": decision.to_dict(),
        }

    def interpret(
        self,
        symbol: str,
        result: SignalResult,
        rule_target: float,
        current_fraction: float,
        rule_reason: str,
        position: AccountPosition,
        account_equity: float,
        selection_context: Optional[Mapping[str, Any]] = None,
        market_context: Optional[Mapping[str, Any]] = None,
    ) -> AIDecision:
        if not self.config.enabled:
            return self.rule_fallback(result, rule_target, current_fraction, rule_reason)
        if self.config.split_readings:
            # 拆分模式：十项各发一次小请求（输出 ~100-300 token），再做一次综合决策。
            # 对 reasoning 型模型（如 deepseek-v4-flash）可靠，避免单次大请求空响应。
            return self._interpret_split(
                symbol, result, rule_target, current_fraction, rule_reason,
                position, account_equity, selection_context, market_context,
            )
        methods = {
            name: {
                "value": round(clamp(result.signals[name]), 6),
                "weight": round(float(self.configured_weight(name)), 6),
                "meaning": ANCIENT_METHOD_DESCRIPTIONS[name],
            }
            for name in STRATEGY_NAMES
        }
        paipan = result.diagnostics.get("paipan")
        request = {
            "symbol": symbol,
            "candle_time": result.candle_time,
            "aggregate_score": round(result.score, 6),
            "rule_target_level": self._target_level(rule_target, current_fraction),
            "rule_target_fraction": round(clamp(rule_target), 6),
            "rule_reason": rule_reason,
            "current_fraction": round(clamp(current_fraction), 6),
            "position": {
                "amount": position.amount,
                "quote_value": position.quote_value,
                "average_entry": position.avg_entry,
                "high_water": position.high_water,
                "account_equity": account_equity,
            },
            "daily_selection": dict(selection_context or {}),
            "market_context": dict(market_context or {}),
            "methods": methods,
            "diagnostics": result.diagnostics,
        }
        if paipan is not None:
            request["paipan_charts"] = paipan  # 完整排盘盘面（时空盘+本命盘）
        system = (
            "你是中国古法十项断卦师（奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱）。"
            "输入包含两项：1) 每项古法的确定性简化置信度（value，由公开规则生成，仅作参考）；"
            "2) paipan_charts 中每项古法的完整盘面（真实排盘结果：卦象/四课三传/九宫/星盘等）。"
            "你必须以断卦师身份解读盘面本身（如梅花体用生克、六壬三传与日干关系、奇门值符吉门、"
            "紫微命宫主星、八字日主旺衰用神、风水飞星吉凶），识别多法共振与冲突，再转译为 "
            "BUY/SELL/HOLD，并按 market_context.allowed_markets 自主选择现货（spot）或合约（swap）表达仓位："
            "只有强烈看多且愿意承担更高波动时才选 swap 并给出 1..max_leverage 的杠杆；"
            "方向不明、看空或盘面中性时选 spot。你是全权决策主体：rule_target 仅是规则引擎的参考意见，"
            "你可以自主决定高于或低于它的目标仓位（FLAT/HALF/FULL/UNCHANGED），为自己给出的 target_level "
            "负责；保护性止损与组合风控（单币/总仓位上限）仍由系统强制执行，你无需也无法绕过。"
            "不得声称预知未来或保证收益，不得编造输入中没有的盘面数据。"
            + self._response_contract()
        )
        try:
            content = self._completion_content([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ])
            decision, _ = self._parse_with_one_format_repair(content)
            self.last_error = ""
            return decision
        except AIRelayError as exc:
            # 中转站/传输层错误（余额不足、鉴权失败、网络错误）：单行日志即可，
            # 不打印堆栈；重试只会继续失败或继续扣费。
            self.last_error = str(exc)
            self.log.error("AI 十项古法解读失败（中转站错误）: %s", exc)
        except Exception as exc:
            self.last_error = repr(exc)
            self.log.error("AI 十项古法解读失败: %s", exc, exc_info=True)
        if self.config.fail_closed:
            return self.rule_fallback(
                result, min(rule_target, current_fraction), current_fraction, rule_reason, self.last_error
            )
        return self.rule_fallback(result, rule_target, current_fraction, rule_reason, self.last_error)

    # ------------------------------------------------------------------
    # 拆分模式：十项各发一次小请求，再做一次综合决策
    # ------------------------------------------------------------------

    def _interpret_split(
        self,
        symbol: str,
        result: SignalResult,
        rule_target: float,
        current_fraction: float,
        rule_reason: str,
        position: AccountPosition,
        account_equity: float,
        selection_context: Optional[Mapping[str, Any]] = None,
        market_context: Optional[Mapping[str, Any]] = None,
    ) -> AIDecision:
        """拆分模式主流程：十项小请求 → 综合决策请求。"""
        try:
            readings = self._readings_split(
                symbol, result, position, account_equity, selection_context
            )
            try:
                decision = self._aggregate_decision(
                    symbol, result, rule_target, current_fraction, rule_reason,
                    position, account_equity, selection_context, readings, market_context,
                )
            except Exception as exc:
                # 聚合决策失败（如空响应）：保留十项 AI 解读，动作/仓位用规则兜底。
                # fallback=True 使仓位只降不增，保持安全。
                self.log.warning("AI 拆分聚合决策失败，使用规则动作 + AI 十项解读: %s", exc)
                decision = self.rule_fallback(
                    result, rule_target, current_fraction, rule_reason, str(exc)
                )
                decision.readings = readings
                return decision
            decision.readings = readings
            self.last_error = ""
            return decision
        except AIRelayError as exc:
            # 中转站整体故障（余额/鉴权/网络）：不再浪费后续请求，整体回退。
            self.last_error = str(exc)
            self.log.error("AI 十项古法解读失败（拆分模式·中转站错误）: %s", exc)
        except Exception as exc:
            self.last_error = repr(exc)
            self.log.error("AI 十项古法解读失败（拆分模式）: %s", exc, exc_info=True)
        if self.config.fail_closed:
            return self.rule_fallback(
                result, min(rule_target, current_fraction), current_fraction, rule_reason, self.last_error
            )
        return self.rule_fallback(result, rule_target, current_fraction, rule_reason, self.last_error)

    def _readings_split(
        self,
        symbol: str,
        result: SignalResult,
        position: AccountPosition,
        account_equity: float,
        selection_context: Optional[Mapping[str, Any]],
    ) -> Dict[str, AncientMethodReading]:
        readings: Dict[str, AncientMethodReading] = {}
        for name in STRATEGY_NAMES:
            try:
                readings[name] = self._read_single_method(
                    name, symbol, result, position, account_equity, selection_context
                )
            except AIRelayError:
                raise  # 中转站整体故障：上抛，由 _interpret_split 统一回退
            except Exception as exc:
                # 单项失败（空正文/结构无效/超时）：该项用规则解读兜底，不影响其余九项
                self.log.warning("AI 拆分解读「%s」失败，该项使用规则解读兜底: %s", name, exc)
                readings[name] = self._rule_reading(name, result)
        return readings

    def _rule_reading(self, name: str, result: SignalResult) -> AncientMethodReading:
        value = clamp(result.signals[name])
        bias = self._bias_from_value(value)
        confidence = clamp(abs(value - 0.5) * 2)
        return AncientMethodReading(
            bias=bias,
            confidence=confidence,
            reading=f"（AI 单项解读失败，规则兜底）{ANCIENT_METHOD_DESCRIPTIONS[name]} 当前归一化值={value:.3f}，规则判定={bias}。",
        )

    def _read_single_method(
        self,
        name: str,
        symbol: str,
        result: SignalResult,
        position: AccountPosition,
        account_equity: float,
        selection_context: Optional[Mapping[str, Any]],
    ) -> AncientMethodReading:
        value = round(clamp(result.signals[name]), 6)
        weight = round(float(self.configured_weight(name)), 6)
        paipan = result.diagnostics.get("paipan")
        method_paipan = None
        if isinstance(paipan, dict):
            method_paipan = paipan.get(name)
        request: Dict[str, Any] = {
            "symbol": symbol,
            "candle_time": result.candle_time,
            "aggregate_score": round(result.score, 6),
            "method": name,
            "value": value,
            "weight": weight,
            "meaning": ANCIENT_METHOD_DESCRIPTIONS[name],
            "position": {
                "amount": position.amount,
                "quote_value": position.quote_value,
                "average_entry": position.avg_entry,
                "high_water": position.high_water,
                "account_equity": account_equity,
            },
            "daily_selection": dict(selection_context or {}),
        }
        if method_paipan is not None:
            request["paipan"] = method_paipan
        system = (
            f"你是中国古法「{name}」断卦师。只负责解读「{name}」这一项，不要解读其他古法，"
            "不要给出交易动作。输出必须是一个 JSON object，字段只能且必须包含 bias、confidence、reading。"
            "bias 只能是 bullish、bearish、neutral 之一；confidence 是 0 到 1 的 JSON number；"
            "reading 是中文断卦解读（如体用生克、三传与日干关系、值符吉门、命宫主星、日主旺衰、"
            "风水飞星吉凶等），须结合盘面具体断卦，不得泛泛重复 value 数值，200 字以内。"
        )
        content = self._completion_content([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ])
        return self._parse_single_method(name, content)

    def _parse_single_method(self, name: str, content: str) -> AncientMethodReading:
        if not isinstance(content, str) or not content.strip():
            raise ConfigError(f"AI 响应正文必须是非空 JSON string（{name}）")
        try:
            return self._parse_single_method_strict(name, content)
        except AIRelayError:
            raise
        except (ConfigError, json.JSONDecodeError) as parse_exc:
            self.log.warning("AI 拆分解读「%s」首次响应结构无效，尝试一次格式修复: %s", name, parse_exc)
            repaired = self._completion_content([
                {"role": "system", "content": (
                    "你是 JSON 接口格式修复器。只能修复字段结构与 JSON 类型，不得重新分析行情。"
                    "只输出包含 bias、confidence、reading 的 JSON object，不得输出其他字段。"
                )},
                {"role": "user", "content": json.dumps({
                    "validation_error": str(parse_exc),
                    "invalid_response": content,
                }, ensure_ascii=False)},
            ])
            return self._parse_single_method_strict(name, repaired)

    @staticmethod
    def _parse_single_method_strict(name: str, content: str) -> AncientMethodReading:
        parsed = require_json_object(json.loads(content), f"AI response.{name}")
        reject_unknown(parsed, {"bias", "confidence", "reading"}, f"AI response.{name}")
        bias = require_json_string(parsed.get("bias"), f"AI response.{name}.bias").strip().lower()
        if bias not in AI_BIASES:
            raise ConfigError(f"AI response.{name}.bias 无效")
        confidence = require_json_number(parsed.get("confidence"), f"AI response.{name}.confidence")
        if not 0 <= confidence <= 1:
            raise ConfigError(f"AI response.{name}.confidence 必须在 0..1")
        reading = require_json_string(parsed.get("reading"), f"AI response.{name}.reading").strip()[:400]
        return AncientMethodReading(bias, confidence, reading)

    def _note_ai_failure(self) -> None:
        """记录一次 AI 失败；连续失败达阈值则打开熔断（暂停 AI 请求一段时间）。"""
        self._fail_streak += 1
        if self._fail_streak >= 4:
            self._circuit_open_until = time.monotonic() + 90
            self.log.warning(
                "AI-1 连续失败 %d 次，熔断 90 秒暂停 AI 请求（上游不稳时避免打爆限频）",
                self._fail_streak,
            )
            self._fail_streak = 0

    def _note_ai_success(self) -> None:
        """AI 调用成功：清零连续失败计数并关闭熔断。"""
        self._fail_streak = 0
        self._circuit_open_until = 0.0

    def decide_entry(
        self,
        symbol: str,
        current_price: float,
        paipan_payload: Optional[Mapping[str, Any]],
        account_equity: float,
        position_quote: float,
        now_iso: str,
    ) -> Tuple[List[TriggerCondition], str, str, str, Optional[Dict[str, Any]]]:
        """AI-1 入场决策（信号触发模式）：按古法判断交不交易，并给出触发条件。

        返回 (conditions, summary, decision_mode, target_level, readings)。
        decision_mode: "enter"=建议入场 | "wait"=继续等待 | "no_trade"=古法不宜交易
                       | "error"=AI 调用/响应失败（调用方应退避重试）。
        target_level: "HALF" / "FULL"，仅 decision=enter 时有效。
        readings: 十项古法读数字典（奇门/六壬/.../四柱），AI 未提供时为 None。
        """
        if not self.config.enabled:
            return [], "AI 已关闭，不设触发条件", "wait", "HALF", None
        # 熔断器打开：暂停 AI 请求，直接快速失败（调用方会写冷却，不会反复重试）。
        if self._circuit_open_until and time.monotonic() < self._circuit_open_until:
            return [], "AI 熔断中（上游不稳），保守等待", "error", "HALF", None
        system = (
            "你是中国古法十项综合断卦师（奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱）。"
            "现在使用信号触发模式：系统不再定时重算，而是由你设定『触发条件』，条件命中才交易。"
            "你必须基于完整盘面与当前行情，判断是否交易，并把入场时机翻译成明确、可执行的触发条件。"
            "输出必须是单个 JSON object，不得输出 Markdown/代码块/解释。字段只能包含："
            "decision（string，精确等于 enter / wait / no_trade 之一）、"
            "summary（string，中文一句话总结断卦结论）、"
            "conditions（array，0 到 4 个触发条件，每个是 object："
            "kind（string，精确等于 price_above/price_below/change_pct_above/change_pct_below/"
            "rsi_above/rsi_below/volume_surge/time_after 之一）、"
            "value（number，条件数值：price 类为价格、change_pct 类为涨跌幅小数如 0.03=+3%、"
            "rsi 类为 0-100、volume_surge 为倍数、time_after 为 ISO 时间字符串）、"
            "ref_price（number，基准价，price/change_pct 类必填，其余给 0）、"
            "note（string，中文说明触发理由））、"
            "target_level（string，精确等于 HALF/FULL 之一，建议入场时的目标仓位）、"
            "first_trigger_at（string，ISO 时间，由你结合古法择时判断的当日首次触发时刻；"
            "表示在此之前不监听该标的，可给当前时间）、"
            "readings（object，十项古法读数，key 为奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱，"
            "value 为 object 含 bias（bullish/bearish/neutral）、confidence（0..1）、reading（中文简述））"
            "。readings 为可选字段，但建议每次都完整填写。"
        )
        request = {
            "symbol": symbol,
            "current_price": current_price,
            "account_equity": account_equity,
            "current_position_quote": position_quote,
            "now_iso": now_iso,
            "paipan": dict(paipan_payload or {}),
            "note": "古法判断交易时机并给出可执行触发条件；decision=no_trade 时 conditions 必须为空数组。",
        }
        try:
            content = self._completion_content([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ])
        except Exception as exc:  # noqa: BLE001 触发决策失败不阻断监听循环
            self.log.error("AI-1 入场决策失败 %s: %s", symbol, exc)
            self._note_ai_failure()
            return [], "AI-1 决策失败，保守等待", "error", "HALF", None
        try:
            parsed = require_json_object(json.loads(content), "AI-1 entry response")
            reject_unknown(
                parsed,
                {"decision", "summary", "conditions", "target_level", "first_trigger_at", "readings"},
                "AI-1 entry response",
            )
        except (ConfigError, json.JSONDecodeError) as exc:
            self.log.warning("AI-1 响应结构无效 %s: %s", symbol, exc)
            self._note_ai_failure()
            return [], "AI-1 响应无效，保守等待", "error", "HALF", None
        self._note_ai_success()
        decision = str(parsed.get("decision", "wait")).strip().lower()
        if decision not in {"enter", "wait", "no_trade"}:
            decision = "wait"
        conditions: List[TriggerCondition] = []
        raw_conds = parsed.get("conditions")
        if isinstance(raw_conds, list):
            for item in raw_conds[:4]:
                if not isinstance(item, dict):
                    continue
                try:
                    cond = TriggerCondition.from_dict(item, now_iso=now_iso)
                    if cond.kind == "time_after" and not cond.value:
                        continue  # time_after 无法解析为绝对时刻，丢弃
                    conditions.append(cond)
                except Exception:  # noqa: BLE001 单条无效忽略
                    continue
        if decision == "no_trade":
            conditions = []
        target_level = str(parsed.get("target_level", "HALF")).strip().upper()
        if target_level not in {"HALF", "FULL"}:
            target_level = "HALF"
        first_at = str(parsed.get("first_trigger_at", "")).strip()
        summary = str(parsed.get("summary", "")).strip()[:200]
        raw_readings = parsed.get("readings")
        readings: Optional[Dict[str, Any]] = None
        if isinstance(raw_readings, dict):
            readings = {}
            for name in ["奇门", "六壬", "太乙", "易经", "风水", "八字", "梅花", "紫微", "八卦", "四柱"]:
                rd = raw_readings.get(name)
                if isinstance(rd, dict):
                    try:
                        readings[name] = {
                            "bias": str(rd.get("bias", "neutral")).strip().lower(),
                            "confidence": max(0.0, min(1.0, float(rd.get("confidence", 0.0)))),
                            "reading": str(rd.get("reading", ""))[:200],
                        }
                    except Exception:  # noqa: BLE001 单项损坏忽略
                        continue
        self.log.warning(
            "AI-1 入场决策 %s | decision=%s target=%s conditions=%d first_at=%s readings=%d | %s",
            symbol, decision, target_level, len(conditions), first_at[:19] or "-",
            len(readings or {}), summary,
        )
        return conditions, summary, decision, target_level, readings

    def decide_downsize(
        self,
        symbol: str,
        reason: str,
        plan_amount: float,
        plan_quote: float,
        fillable_amount: float,
        fillable_quote: float,
        slippage_bps: float,
        max_slippage_bps: float,
        now_iso: str,
    ) -> Tuple[str, float]:
        """滑点/深度检查失败后的缩量仲裁：AI 判断少买还是放弃。

        返回 (action, amount_pct)：
        - action: "buy" = 缩量买入（amount_pct 为新数量占原计划比例，0.05~1.0）
        - action: "skip" = 放弃该标的
        AI 调用失败或熔断时保守返回 ("skip", 0.0)。
        """
        if not self.config.enabled:
            return "skip", 0.0
        if self._circuit_open_until and time.monotonic() < self._circuit_open_until:
            self.log.warning("AI 熔断中，缩量决策走保守放弃 %s", symbol)
            return "skip", 0.0
        fill_ratio = (fillable_amount / plan_amount) if plan_amount > 0 else 0.0
        system = (
            "你是中国古法量化交易系统的风控仲裁 AI。当系统因订单簿深度不足或滑点超限，"
            "无法按原计划数量买入某标的时，由你决定：缩量买入（减小数量）还是放弃。"
            "输出必须是单个 JSON object，不得输出 Markdown/代码块/解释。字段只能包含："
            "action（string，精确等于 buy / skip 之一）、"
            "amount_pct（number，0.05 到 1.0 之间，buy 时表示新下单数量=原计划数量×amount_pct；skip 时给 0）、"
            "reason（string，中文一句话说明决策理由）。不得输出其他字段。"
        )
        request = {
            "symbol": symbol,
            "plan_amount": plan_amount,
            "plan_quote": plan_quote,
            "failure_reason": reason,
            "fillable_amount": round(fillable_amount, 6),
            "fillable_quote": round(fillable_quote, 2),
            "fill_ratio": round(fill_ratio, 4),
            "estimated_slippage_bps": round(slippage_bps, 2),
            "max_slippage_bps": max_slippage_bps,
            "now_iso": now_iso,
            "note": (
                "原计划无法成交：可成交比例 fill_ratio 是 100 档订单簿能承接的原计划比例。"
                "若缩量后仍会大幅滑点超限（如 fill_ratio 过低或原滑点远高于上限），选择 skip；"
                "若只是略微超限或深度差一点，缩量（amount_pct 取较小值如 0.3~0.7）后滑点可控，选择 buy。"
            ),
        }
        try:
            content = self._completion_content([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ])
            parsed = require_json_object(json.loads(content), "AI downsize response")
            reject_unknown(parsed, {"action", "amount_pct", "reason"}, "AI downsize response")
        except Exception as exc:  # noqa: BLE001 决策失败保守放弃
            self.log.error("AI 缩量决策失败 %s: %s", symbol, exc)
            self._note_ai_failure()
            return "skip", 0.0
        self._note_ai_success()
        action = str(parsed.get("action", "skip")).strip().lower()
        if action not in {"buy", "skip"}:
            action = "skip"
        try:
            amount_pct = float(parsed.get("amount_pct", 0.0))
        except (TypeError, ValueError):
            amount_pct = 0.0
        amount_pct = min(1.0, max(0.05, amount_pct)) if action == "buy" else 0.0
        summary = str(parsed.get("reason", "")).strip()[:200]
        self.log.warning(
            "AI 缩量仲裁 %s | action=%s amount_pct=%.0f%% fill_ratio=%.1f%% slip=%.1f/%.0fbps | %s",
            symbol, action, amount_pct * 100, fill_ratio * 100, slippage_bps, max_slippage_bps, summary,
        )
        return action, amount_pct

    def decide_exit(
        self,
        symbol: str,
        current_price: float,
        ref_price: float,
        paipan_payload: Optional[Mapping[str, Any]],
        account_equity: float,
        position_quote: float,
        now_iso: str,
    ) -> Tuple[List[TriggerCondition], str, str, Optional[Dict[str, Any]]]:
        """AI-2 出场决策（信号触发模式）：按古法判断卖出条件，输出出场触发条件。

        返回 (exit_conditions, summary, status, readings)。
        status: "ok"=正常 | "error"=调用/响应失败。
        readings: 十项古法读数字典，AI 未提供时为 None。
        """
        if not self.config.enabled:
            return [], "AI 已关闭，不设出场条件", "ok", None
        system = (
            "你是中国古法十项综合断卦师（奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱）。"
            "系统使用信号触发模式。现在你负责『出场决策』：为已持仓标的设定卖出触发条件。"
            "必须基于完整盘面、当前价格与持仓成本（买入价基准）判断，并给出明确可执行的触发条件。"
            "输出必须是单个 JSON object，不得输出 Markdown/代码块/解释。字段只能包含："
            "summary（string，中文一句话总结断卦结论）、"
            "conditions（array，1 到 5 个出场触发条件，每个是 object："
            "kind（string，精确等于 price_above/price_below/change_pct_above/change_pct_below/"
            "rsi_above/rsi_below/volume_surge/time_after 之一）、"
            "value（number，条件数值：change_pct 类为涨跌幅小数如 0.08=+8% 止盈、-0.05=-5% 止损，"
            "其余同上）、"
            "ref_price（number，基准价，必须使用给定持仓成本价）、"
            "note（string，中文说明卖出理由））"
            "。conditions 至少 1 条；若古法认为应继续持有，可只给 time_after 一条作为最迟复查。"
            "readings（object，十项古法读数，key 为奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱，"
            "value 为 object 含 bias（bullish/bearish/neutral）、confidence（0..1）、reading（中文简述））"
            "。readings 为可选字段，但建议每次都完整填写。"
        )
        request = {
            "symbol": symbol,
            "current_price": current_price,
            "ref_price": ref_price,
            "account_equity": account_equity,
            "position_quote": position_quote,
            "now_iso": now_iso,
            "paipan": dict(paipan_payload or {}),
            "note": "以买入价为基准设定卖出触发条件（止盈/止损/时间兜底）。",
        }
        try:
            content = self._completion_content([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ])
        except Exception as exc:  # noqa: BLE001
            self.log.error("AI-2 出场决策失败 %s: %s", symbol, exc)
            return [], "AI-2 决策失败，沿用现有出场条件", "error", None
        try:
            parsed = require_json_object(json.loads(content), "AI-2 exit response")
            reject_unknown(parsed, {"summary", "conditions", "readings"}, "AI-2 exit response")
        except (ConfigError, json.JSONDecodeError) as exc:
            self.log.warning("AI-2 响应结构无效 %s: %s", symbol, exc)
            return [], "AI-2 响应无效，沿用现有出场条件", "error", None
        conditions: List[TriggerCondition] = []
        raw_conds = parsed.get("conditions")
        if isinstance(raw_conds, list):
            for item in raw_conds[:5]:
                if not isinstance(item, dict):
                    continue
                try:
                    cond = TriggerCondition.from_dict(item, now_iso=now_iso)
                    if cond.kind == "time_after" and not cond.value:
                        continue  # time_after 无法解析为绝对时刻，丢弃
                    conditions.append(cond)
                except Exception:  # noqa: BLE001
                    continue
        summary = str(parsed.get("summary", "")).strip()[:200]
        raw_readings = parsed.get("readings")
        readings: Optional[Dict[str, Any]] = None
        if isinstance(raw_readings, dict):
            readings = {}
            for name in ["奇门", "六壬", "太乙", "易经", "风水", "八字", "梅花", "紫微", "八卦", "四柱"]:
                rd = raw_readings.get(name)
                if isinstance(rd, dict):
                    try:
                        readings[name] = {
                            "bias": str(rd.get("bias", "neutral")).strip().lower(),
                            "confidence": max(0.0, min(1.0, float(rd.get("confidence", 0.0)))),
                            "reading": str(rd.get("reading", ""))[:200],
                        }
                    except Exception:  # noqa: BLE001
                        continue
        self.log.warning(
            "AI-2 出场决策 %s | ref=%.4f conditions=%d readings=%d | %s",
            symbol, ref_price, len(conditions), len(readings or {}), summary,
        )
        return conditions, summary, "ok", readings

    def _aggregate_decision(
        self,
        symbol: str,
        result: SignalResult,
        rule_target: float,
        current_fraction: float,
        rule_reason: str,
        position: AccountPosition,
        account_equity: float,
        selection_context: Optional[Mapping[str, Any]],
        readings: Mapping[str, AncientMethodReading],
        market_context: Optional[Mapping[str, Any]] = None,
    ) -> AIDecision:
        readings_payload = {
            name: {"bias": r.bias, "confidence": r.confidence, "reading": r.reading[:60]}
            for name, r in readings.items()
        }
        request = {
            "symbol": symbol,
            "candle_time": result.candle_time,
            "aggregate_score": round(result.score, 6),
            "rule_target_level": self._target_level(rule_target, current_fraction),
            "rule_target_fraction": round(clamp(rule_target), 6),
            "rule_reason": rule_reason,
            "current_fraction": round(clamp(current_fraction), 6),
            "position": {
                "amount": position.amount,
                "quote_value": position.quote_value,
                "average_entry": position.avg_entry,
                "high_water": position.high_water,
                "account_equity": account_equity,
            },
            "daily_selection": dict(selection_context or {}),
            "market_context": dict(market_context or {}),
            "readings": readings_payload,  # 十项已解读结果，只做综合，不再逐项重读
        }
        system = (
            "你是中国古法十项断卦的汇总决策师。输入已包含十项古法的完整解读（bias/confidence/reading），"
            "你不得重新解读各项，只需综合十项解读、规则目标与持仓状态，输出最终交易决策。"
            "你是仓位决策者：rule_target_level 是规则引擎给出的默认仓位目标，"
            "你必须以它为准：当前仓位低于规则目标且十项解读无明确看空时，默认 BUY 至规则目标；"
            "SELL 只能在明确看空时降低风险；HOLD 只能用于持仓已接近规则目标或信号中性（分数接近 0.5）时。"
            "空仓 + 规则目标为 HALF/FULL 时，不要输出 HOLD，除非十项解读强烈看空。"
            "market 只能从 market_context.allowed_markets 中选择：允许 swap 且强烈看多时才选 swap 放大敞口"
            "（leverage 取 1..max_leverage 且不超过 max_futures_notional_pct 约束）；其余情况选 spot、leverage=1。"
            "注意：输出中不得包含 readings 字段（十项解读已由上游单独提供），"
            "只需输出 action、target_level、confidence、market、leverage、summary、conflicts、risk_notes、next_review_minutes。"
            + self._response_contract()
        )
        content = self._completion_content([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ])
        # 聚合响应不含 readings（已单独获取），解析时跳过 readings 校验
        try:
            decision, _ = self._parse_with_one_format_repair(content, require_readings=False)
        except (ConfigError, json.JSONDecodeError) as exc:
            # 聚合请求较大（十项解读载荷），flash 偶发空正文/截断：
            # 削减载荷重试一次，仍失败才上抛由 _interpret_split 规则兜底。
            self.log.warning("AI 聚合决策首次失败，削减载荷重试一次: %s", exc)
            retry_readings = {
                name: {"bias": r.bias, "confidence": r.confidence, "reading": r.reading[:40]}
                for name, r in readings.items()
            }
            retry_request = dict(request, readings=retry_readings)
            retry_content = self._completion_content([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(retry_request, ensure_ascii=False)},
            ])
            decision, _ = self._parse_with_one_format_repair(retry_content, require_readings=False)
        return decision

    def configured_weight(self, name: str) -> float:
        # 由控制器在构造后绑定 StrategyConfig；保留默认值以便独立测试。
        weights = getattr(self, "strategy_weights", {})
        return finite(weights.get(name), 0.0)

    def bind_strategy_weights(self, weights: Mapping[str, float]) -> None:
        self.strategy_weights = dict(weights)


# =============================================================================
# 主控制器
# =============================================================================


@dataclass
class DailySelectionResult:
    enabled: bool
    date: str
    timeframe: str
    complete: bool
    cached: bool
    selected_symbols: List[str]
    scores: Dict[str, float]
    candle_times: Dict[str, str]
    candidates: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    dead: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolDecision:
    symbol: str
    score: float
    target_fraction: float
    target_allocation: float
    reason: str
    signal_result: SignalResult
    ai_decision: AIDecision
    # AI 选择的市场（spot/swap）与合约杠杆
    market: str = MARKET_SPOT
    leverage: float = 1.0


def _send_webhook(url: str, payload: Dict[str, Any], timeout: int = 5) -> bool:
    """发送 JSON POST 到 webhook；失败返回 False（best-effort，不影响主流程）。"""
    try:
        import urllib.request
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(128)
        return True
    except Exception:  # noqa: BLE001 通知失败不向上抛
        return False


class GuFaQuantPro:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        credentials: Optional[CredentialStore] = None,
    ):
        self.config = config
        self.log = logger
        self.credentials = credentials
        self.state_dir = Path(config.runtime.state_dir).expanduser().resolve()
        account_key = secret_from_env(
            config.exchange.api_key_env,
            required=True,
            credentials=credentials,
            purpose="交易所 API",
        )
        self.store = StateStore(
            self.state_dir / "state.json",
            build_profile_id(config, account_key),
        )
        self.gateway = ExchangeGateway(config, self.store, logger, credentials)
        self.engine = StrategyEngine(config.strategy, paipan_config=config.paipan)
        # 信号触发模式需要直接访问排盘服务（古法择时 + AI 盘面载荷）；
        # 周期模式走 engine，这里独立初始化一份，失败只降级不阻塞。
        self.paipan_service: Optional[PaipanService] = None
        try:
            if getattr(config.paipan, "enabled", False):
                self.paipan_service = build_paipan_service(config.paipan)
        except Exception as exc:  # noqa: BLE001 排盘降级：择时回退立即监听、AI 载荷为空
            self.log.warning("排盘服务初始化失败，古法择时/盘面载荷降级: %s", exc)
        self.risk = RiskManager(config.risk, self.store, logger)
        self.ai = AIAdvisor(config.ai, logger, credentials)
        self.ai.bind_strategy_weights(config.strategy.weights)
        self.audit_path = self.state_dir / "orders.audit.jsonl"
        self.stop_event = threading.Event()
        self._indicator_cache: Dict[str, Tuple[float, float, float]] = {}
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self.log.warning("收到退出信号 %s，等待当前原子操作结束", signum)
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
    def _prefilter_symbols(self, symbols: Sequence[str]) -> List[str]:
        """名字初筛：只用名字规则，不用流动性，返回古法扫描候选池。

        规则（零行情请求，绝不 fail-closed）：
        1. preferred（主流币）全部保留；
        2. 排除以数字开头的新币/蹭热币（知名币可加入 preferred）；
        3. 排除 exclude_patterns 匹配的标的。

        不做成交额/市值截断：候选池全部进入古法扫描，每天重点看哪几个
        完全由古法得分决定（min_score 门槛 + top_n 上限），保持严谨。
        """
        selection = self.config.selection
        if not selection.prefilter:
            return list(symbols)
        preferred = set(selection.preferred) if selection.preferred else set(DEFAULT_PREFERRED_BASES)
        preferred_hits: List[str] = []
        rest: List[str] = []
        for symbol in symbols:
            base = base_asset(symbol).upper()
            if base in preferred:
                preferred_hits.append(symbol)
                continue
            if re.match(r"^\d", base):
                # 以数字开头多为新上线/蹭热币（如 2Z；1INCH 等知名币可加入 preferred）
                continue
            if any(re.search(pattern, base, re.IGNORECASE) for pattern in selection.exclude_patterns):
                continue
            rest.append(symbol)
        candidates = preferred_hits + rest
        self.log.info(
            "名字初筛（无流动性，纯名字规则）| %d -> %d 候选 (preferred=%d, rest=%d)",
            len(symbols),
            len(candidates),
            len(preferred_hits),
            len(rest),
        )
        return candidates

    def _selection_cache_key(self) -> str:
        selection = self.config.selection
        payload = {
            "app_version": APP_VERSION,
            "symbols": self.config.runtime.symbols,
            "timeframe": selection.timeframe,
            "ohlcv_limit": selection.ohlcv_limit,
            "top_n": selection.top_n,
            "min_score": selection.min_score,
            "weights": self.config.strategy.weights,
            "prefilter": {
                "enabled": selection.prefilter,
                "preferred": list(selection.preferred),
                "exclude_patterns": list(selection.exclude_patterns),
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _daily_selection(self) -> DailySelectionResult:
        """按 UTC 自然日对候选池扫描；扫描不完整时返回空入选集，禁止新增风险。"""
        selection = self.config.selection
        today = utc_now().date().isoformat()
        if not selection.enabled:
            return DailySelectionResult(
                enabled=False,
                date=today,
                timeframe=selection.timeframe,
                complete=True,
                cached=False,
                selected_symbols=list(self.config.runtime.symbols),
                scores={},
                candle_times={},
                candidates=list(self.config.runtime.symbols),
            )

        state = self.store.state
        cache_key = self._selection_cache_key()
        if state.daily_selection_date == today and state.daily_selection_key == cache_key:
            allowed = set(self.config.runtime.symbols)
            selected = [symbol for symbol in state.daily_selected_symbols if symbol in allowed]
            candidates = [
                symbol for symbol in state.daily_selection_candidates if symbol in allowed
            ]
            dead = {
                symbol: reason
                for symbol, reason in state.daily_selection_dead.items()
                if symbol in allowed
            }
            return DailySelectionResult(
                enabled=True,
                date=today,
                timeframe=selection.timeframe,
                complete=True,
                cached=True,
                selected_symbols=selected,
                scores=dict(state.daily_selection_scores),
                candle_times=dict(state.daily_selection_candle_times),
                candidates=candidates,
                dead=dead,
            )

        candidates = self._prefilter_symbols(self.config.runtime.symbols)
        scores: Dict[str, float] = {}
        candle_times: Dict[str, str] = {}
        errors: Dict[str, str] = {}
        dead: Dict[str, str] = {}
        for symbol in candidates:
            try:
                frame = self.gateway.fetch_ohlcv(
                    symbol,
                    timeframe=selection.timeframe,
                    ohlcv_limit=selection.ohlcv_limit,
                )
                result = self.engine.calculate(frame, symbol=symbol)
                scores[symbol] = result.score
                candle_times[symbol] = result.candle_time
            except Exception as exc:
                message = str(exc)[:300]
                if self._is_dead_symbol_error(message):
                    # 死币：无行情数据，古法无法分析。只剔除该标的当日候选资格，
                    # 不算系统故障；次日自动重新探测，恢复行情后自动回归候选池。
                    dead[symbol] = message
                    self.log.warning("%s 无有效行情，当日剔除出候选池: %s", symbol, message)
                else:
                    # 真实故障（网络/超时/限流等）：严格 fail-closed，禁止新开仓。
                    errors[symbol] = message
                    self.log.error("%s 每日古法初选失败: %s", symbol, exc, exc_info=True)

        # 活候选（死币不计入，当日不可用古法分析）
        live_candidates = [s for s in candidates if s not in dead]

        if errors:
            self.log.warning(
                "每日古法初选不完整，fail-closed：本周期禁止新开仓；失败标的=%s",
                sorted(errors),
            )
            return DailySelectionResult(
                enabled=True,
                date=today,
                timeframe=selection.timeframe,
                complete=False,
                cached=False,
                selected_symbols=[],
                scores=scores,
                candle_times=candle_times,
                candidates=live_candidates,
                errors=errors,
                dead=dead,
            )

        ranked = sorted(scores, key=lambda symbol: (-scores[symbol], symbol))
        selected = [
            symbol for symbol in ranked if scores[symbol] >= selection.min_score
        ][:selection.top_n]
        state.daily_selection_date = today
        state.daily_selection_key = cache_key
        state.daily_selected_symbols = list(selected)
        state.daily_selection_candidates = list(live_candidates)
        state.daily_selection_scores = dict(scores)
        state.daily_selection_candle_times = dict(candle_times)
        state.daily_selection_dead = dict(dead)
        self.store.save()
        self.log.info(
            "每日古法初选完成 | timeframe=%s | candidates=%d | dead=%s | selected=%s | scores=%s",
            selection.timeframe,
            len(candidates),
            sorted(dead) or "-",
            selected,
            {symbol: round(scores[symbol], 3) for symbol in ranked},
        )
        return DailySelectionResult(
            enabled=True,
            date=today,
            timeframe=selection.timeframe,
            complete=True,
            cached=False,
            selected_symbols=selected,
            scores=scores,
            candle_times=candle_times,
            candidates=live_candidates,
            dead=dead,
        )

    @staticmethod
    def _is_dead_symbol_error(message: str) -> bool:
        """判断一个初选失败是否属于死币（无行情数据），而非系统故障。"""
        return any(marker in message for marker in DEAD_SYMBOL_MARKERS)

    @staticmethod
    def _is_depth_error(message: str) -> bool:
        """判断订单失败是否属于订单簿深度不足（薄盘拒单），而非系统故障。"""
        return "订单簿深度不足以成交" in message

    def _depth_blocked_today(self, symbol: str) -> bool:
        """该标的是否已在当日深度不足缓存中（仅拦截新买入，卖出/保护退出不拦）。"""
        today = utc_now().date().isoformat()
        return self.store.state.depth_blocked.get(symbol) == today

    def _prune_depth_blocked(self) -> None:
        """清理过期深度缓存：仅保留当日的记录，次日自动失效重试。"""
        today = utc_now().date().isoformat()
        stale = {
            symbol: day
            for symbol, day in self.store.state.depth_blocked.items()
            if day != today
        }
        if stale:
            self.log.info("深度不足缓存过期清理 %d 个标的: %s", len(stale), sorted(stale))
            for symbol in stale:
                self.store.state.depth_blocked.pop(symbol, None)

    def _account_prices(self) -> Dict[str, float]:
        """账户估值必须覆盖完整白名单，即使标的未通过当日初选。

        当日已确认的死币（无有效行情，见 daily_selection_dead）且无持仓时直接
        跳过，避免每周期重复拉价刷屏告警；有持仓的币仍硬校验（估值失败即保守
        停止），绝不因死币缓存而放松持仓保护。
        """
        dead = self.store.state.daily_selection_dead
        prices: Dict[str, float] = {}
        for symbol in self.config.runtime.symbols:
            if symbol in dead and symbol not in self.store.state.positions:
                continue
            try:
                prices[symbol] = self.gateway.fetch_last_price(symbol)
            except Exception as exc:  # noqa: BLE001 无持仓死币跳过，不影响主流程
                if symbol in self.store.state.positions:
                    raise
                self.log.warning("%s 无有效价格，跳过估值（该标的无持仓）: %s", symbol, exc)
        return prices

    def _prices_and_signals(
        self,
        symbols: Optional[Iterable[str]] = None,
    ) -> Tuple[Dict[str, float], Dict[str, SignalResult]]:
        prices: Dict[str, float] = {}
        results: Dict[str, SignalResult] = {}
        allowed = set(self.config.runtime.symbols)
        requested = allowed if symbols is None else set(symbols)
        unknown = requested - allowed
        if unknown:
            raise SafetyError(f"精筛请求包含候选池外标的: {sorted(unknown)}")
        for symbol in self.config.runtime.symbols:
            if symbol not in requested:
                continue
            df = self.gateway.fetch_ohlcv(symbol)
            result = self.engine.calculate(df, symbol=symbol)
            prices[symbol] = result.diagnostics["price"]
            results[symbol] = result
        return prices, results

    @staticmethod
    def _level_fraction(level: str, current_fraction: float) -> float:
        return {
            "FLAT": 0.0,
            "HALF": 0.5,
            "FULL": 1.0,
            "UNCHANGED": clamp(current_fraction),
        }[level]

    def _apply_ai_bounds(
        self,
        ai: AIDecision,
        rule_target: float,
        current_fraction: float,
    ) -> Tuple[float, str]:
        requested = self._level_fraction(ai.target_level, current_fraction)
        if not ai.enabled or self.config.ai.decision_mode == "explain_only":
            return clamp(rule_target), "AI explanation only; deterministic rule target retained"
        if ai.fallback:
            # 规则兜底：rule_fallback 已按 rule_target vs current 生成 BUY/SELL/HOLD。
            # 空仓且规则目标 > 0 时遵循规则目标（AI 失败不应阻止确定性规则建仓，
            # 与 rule_fallback 的 BUY 语义一致）；有持仓时保持 fail-closed：只减不增。
            if current_fraction <= 0.001 and rule_target > 0.001:
                fallback_target = min(requested, rule_target)
            else:
                fallback_target = min(requested, current_fraction, rule_target)
            return clamp(fallback_target), (
                f"AI fallback; requested={requested:.2f}; current={current_fraction:.2f}; "
                f"rule_cap={rule_target:.2f}; applied={fallback_target:.2f}"
            )
        if self.config.ai.decision_mode == "full":
            # AI 全权模式：动作与目标档位直接生效，不受规则分数封顶、不受置信度门槛拦截。
            # 置信度仅作记录；protective 已在周期入口置空（止损/止盈/移动止损不触发），
            # 账户级熔断已忽略（仅记录），单币 20%/总仓位 70%/合约名义与保证金上限均已放开，
            # 下单金额仍受 max_order_quote 与现金留存约束。
            if ai.action == "BUY":
                requested = max(current_fraction, requested)
            elif ai.action == "SELL":
                requested = min(current_fraction, requested)
            # HOLD：按 AI 档位（UNCHANGED=维持 / FLAT=清仓 / HALF、FULL=按档位调）
            return clamp(requested), (
                f"AI full-authority; AI {ai.action}/{ai.target_level}; "
                f"confidence={ai.confidence:.2f}; requested={requested:.2f}; "
                f"rule_ref={rule_target:.2f}; current={current_fraction:.2f}"
            )
        if ai.confidence < self.config.ai.minimum_allow_confidence:
            requested = current_fraction
            confidence_reason = (
                f"AI confidence {ai.confidence:.2f} < "
                f"{self.config.ai.minimum_allow_confidence:.2f}; HOLD"
            )
        else:
            confidence_reason = f"AI confidence={ai.confidence:.2f}"
        if ai.action == "BUY":
            requested = max(current_fraction, requested)
            bounded = min(requested, rule_target)
        elif ai.action == "SELL":
            requested = min(current_fraction, requested)
            bounded = min(requested, rule_target)
        else:
            # HOLD：持仓接近规则目标时维持；空仓且规则目标 > 0 时，
            # 让规则目标生效（AI 未明确看空不应阻止建仓，见聚合 prompt）。
            if current_fraction <= 0.001 and rule_target > 0.001:
                bounded = clamp(rule_target)
            else:
                bounded = min(current_fraction, rule_target)
        return clamp(bounded), (
            f"{confidence_reason}; AI {ai.action}/{ai.target_level}; "
            f"requested={requested:.2f}; rule_cap={rule_target:.2f}; applied={bounded:.2f}"
        )

    def _build_decisions(
        self,
        snapshot: AccountSnapshot,
        results: Mapping[str, SignalResult],
        protective: Mapping[str, str],
        risk_status: RiskStatus,
        selected_symbols: Iterable[str],
        daily_selection: Optional[DailySelectionResult] = None,
    ) -> Dict[str, SymbolDecision]:
        selected = set(selected_symbols)
        unknown = selected - set(self.config.runtime.symbols)
        if unknown:
            raise SafetyError(f"每日初选包含候选池外标的: {sorted(unknown)}")

        full_mode = self.config.ai.decision_mode == "full"
        raw: Dict[str, Tuple[float, str, AIDecision]] = {}
        for symbol, result in results.items():
            position = snapshot.positions.get(symbol)
            if position is None:
                position = AccountPosition(
                    symbol, 0.0, result.diagnostics.get("price", 0.0), 0.0, 0.0, 0.0
                )
            if full_mode:
                # AI 全权：绝对坐标（权益占比 0~1），不受单币 20% / 总仓位 70% 缩放。
                current_target_fraction = clamp(
                    position.quote_value / snapshot.equity if snapshot.equity > 0 else 0.0
                )
                rule_target_rel, rule_reason = self.engine.target_fraction(
                    result.score, current_target_fraction
                )
                # 规则目标转绝对权益占比（full 模式仅作参考/兜底，不封顶）
                rule_target = clamp(rule_target_rel * self.config.risk.max_symbol_allocation)
            else:
                current_target_fraction = 0.0
                if snapshot.equity > 0 and self.config.risk.max_symbol_allocation > 0:
                    current_target_fraction = (
                        position.quote_value / snapshot.equity / self.config.risk.max_symbol_allocation
                    )
                current_target_fraction = clamp(current_target_fraction)
                rule_target, rule_reason = self.engine.target_fraction(result.score, current_target_fraction)

            if symbol in selected:
                selection_context: Dict[str, Any] = {"selected": True}
                if daily_selection is not None:
                    ranked = sorted(
                        daily_selection.scores,
                        key=lambda item: (-daily_selection.scores[item], item),
                    )
                    selection_context.update({
                        "date": daily_selection.date,
                        "timeframe": daily_selection.timeframe,
                        "daily_score": round(daily_selection.scores.get(symbol, 0.0), 6),
                        "rank": ranked.index(symbol) + 1 if symbol in ranked else None,
                        "top_n": self.config.selection.top_n,
                        "minimum_score": self.config.selection.min_score,
                        "candidate_pool_size": (
                            len(daily_selection.candidates)
                            if daily_selection and daily_selection.candidates
                            else len(self.config.runtime.symbols)
                        ),
                    })
                ai_decision = self.ai.interpret(
                    symbol=symbol,
                    result=result,
                    rule_target=rule_target,
                    current_fraction=current_target_fraction,
                    rule_reason=rule_reason,
                    position=position,
                    account_equity=snapshot.equity,
                    selection_context=selection_context,
                    market_context={
                        "allowed_markets": list(self.config.exchange.allowed_markets),
                        "max_leverage": self.config.risk.max_leverage,
                        "max_futures_notional_pct": self.config.risk.max_futures_notional_pct,
                        "futures_allow_short": self.config.risk.futures_allow_short,
                    },
                )
                target, ai_bound_reason = self._apply_ai_bounds(
                    ai_decision, rule_target, current_target_fraction
                )
                reason = f"{rule_reason}; {ai_bound_reason}; AI summary={ai_decision.summary}"
                market = self._resolve_market(ai_decision, position.market)
            else:
                # 落选标的只可能因已有仓位进入精筛范围；跳过远程 AI，且永远不得加仓。
                target = min(rule_target, current_target_fraction)
                gate_reason = (
                    "not selected by daily ancient-method ranking; remote AI skipped; "
                    f"no increase; rule_cap={rule_target:.2f}; current={current_target_fraction:.2f}; "
                    f"applied={target:.2f}"
                )
                ai_decision = self.ai.rule_fallback(
                    result,
                    target,
                    current_target_fraction,
                    gate_reason,
                )
                ai_decision.enabled = False
                ai_decision.fallback = False
                ai_decision.summary = "未通过当日古法初选；不调用远程 AI，只允许持有、减仓或保护性退出。"
                ai_decision.risk_notes = ["每日初选门控禁止该标的新增风险。"]
                reason = f"{rule_reason}; {gate_reason}"
                market = position.market

            if symbol in protective:
                target, reason = 0.0, protective[symbol] + "; hard risk overrides AI/selection"
            elif not risk_status.allowed:
                # 熔断时不新增风险；AI 与每日初选都不能绕过账户级硬风控。
                target = min(target, current_target_fraction)
                reason = f"risk halt, no increase: {risk_status.reason}; {reason}"
            raw[symbol] = (target, reason, ai_decision)

        if full_mode:
            # AI 全权：target 已是绝对权益占比，直接作为目标分配，不做单币/总仓位缩放。
            raw_allocations = {symbol: target for symbol, (target, _, _) in raw.items()}
            scale = 1.0
        else:
            raw_allocations = {
                symbol: target * self.config.risk.max_symbol_allocation
                for symbol, (target, _, _) in raw.items()
            }
            total = sum(raw_allocations.values())
            scale = min(1.0, self.config.risk.max_total_allocation / max(total, EPSILON))
        decisions: Dict[str, SymbolDecision] = {}
        for symbol, (target, reason, ai_decision) in raw.items():
            allocation = raw_allocations[symbol] * scale
            decisions[symbol] = SymbolDecision(
                symbol=symbol,
                score=results[symbol].score,
                target_fraction=target,
                target_allocation=allocation,
                reason=reason,
                signal_result=results[symbol],
                ai_decision=ai_decision,
                market=market,
                leverage=ai_decision.leverage if ai_decision.enabled else 1,
            )
        return decisions

    def _resolve_market(self, ai_decision: AIDecision, current_market: str) -> str:
        """把 AI 选择的市场收敛到配置白名单 + 交易所实际支持范围内。

        - AI 要求的市场不在白名单：优先回退到当前持仓市场，再回退到默认市场。
        - 合约市场对个别币不存在（如 OKB 无永续）：回退现货。
        - AI 未表态（默认 spot）：直接取白名单里的默认市场，保持老行为。
        """
        allowed = self.config.exchange.allowed_markets
        if not allowed:
            return "spot"

        def market_supported(market: str, symbol: str) -> bool:
            if market == MARKET_SPOT:
                return True
            if market != MARKET_SWAP:
                return False
            if MARKET_SWAP not in allowed:
                return False
            try:
                info = self.gateway.markets.get(
                    self.gateway.exchange_symbol(MARKET_SWAP, symbol)
                ) or {}
            except Exception:
                return False
            return bool(info.get("swap"))

        symbol = ai_decision.symbol if hasattr(ai_decision, "symbol") else None
        if ai_decision.market in allowed and (
            symbol is None or market_supported(ai_decision.market, symbol)
        ):
            return ai_decision.market
        if symbol is not None and current_market in allowed and market_supported(current_market, symbol):
            return current_market
        return allowed[0]

    def _next_review_seconds(
        self,
        decisions: Mapping[str, SymbolDecision],
        risk_allowed: bool,
        protective: Mapping[str, str],
        paused: bool,
    ) -> int:
        """AI 决定下一次查看 K 线的时机。

        取所有 AI 建议中最紧的（最短间隔）以保证任何被选中标的都不被错过，
        并钳制在 [poll_interval_seconds, 6 小时] 内。熔断/保护性退出/暂停期间
        不使用 AI 建议，强制保守轮询间隔——异常状态必须频繁检查，不能拉长。
        无 AI 建议（AI 未启用、落选持仓、解析失败回退）时用默认轮询间隔。
        """
        base = self.config.runtime.poll_interval_seconds
        if not risk_allowed or protective or paused:
            return base
        minutes = [
            decision.ai_decision.next_review_minutes
            for decision in decisions.values()
            if decision.ai_decision.next_review_minutes is not None
        ]
        if not minutes:
            return base
        tightest = min(minutes) * 60
        return int(min(max(tightest, base), 6 * 3600))

    def _notify(self, event: str, payload: Dict[str, Any]) -> None:
        """可选的 webhook 事件通知（runtime.webhook_url 为空时静默跳过）。

        仅用于事件通知（成交/熔断等），best-effort：失败只记日志，不影响交易主流程。
        """
        url = self.config.runtime.webhook_url
        if not url:
            return
        body = {
            "event": event,
            "ts": iso_now(),
            "app": APP_NAME,
            "version": APP_VERSION,
            "sandbox": self.config.exchange.sandbox,
            **payload,
        }
        if _send_webhook(url, body):
            self.log.info("webhook %s 已发送", event)
        else:
            self.log.warning("webhook %s 发送失败", event)

    # ================= 信号触发模式（trigger_mode="signal"） =================

    def _auspicious_first_hour(self, symbol: str, now_dt) -> str:
        """古法择时：扫描当天剩余 12 时辰（每 2 小时一档），对每档做时空盘打分，
        取信号最强且晚于当前时刻的档位作为首次触发时刻。

        排盘打分成本高，12 档全跑会慢；实际折中：从当前时辰起每 2 小时一档，
        最多评估 6 档，选最高分档。失败时返回当前时刻（立即开始监听）。
        """
        try:
            if self.paipan_service is None:
                return iso_now()
            base = now_dt.replace(minute=0, second=0, microsecond=0)
            best_ts: Optional[float] = None
            best_score = -1.0
            for slot in range(6):
                probe = base + timedelta(hours=slot * 2)
                if probe <= now_dt:
                    continue
                try:
                    chart = self.paipan_service.paipan(
                        symbol, now_dt=probe,
                        listing_ts=self._listing_ts(symbol),
                    )
                    # PaipanResult 无统一评分字段：用时空盘成功生成的方法数
                    # 作为该时辰"盘面可用性"打分，至少让档位选择有区分度。
                    current = getattr(chart, "current", None)
                    if isinstance(current, dict):
                        score = float(sum(
                            1 for v in current.values()
                            if isinstance(v, dict) and "error" not in v
                        ))
                    else:
                        score = 0.0
                except Exception:  # noqa: BLE001 单档失败跳过
                    continue
                if score > best_score:
                    best_score = score
                    best_ts = probe.timestamp()
            if best_ts is None:
                return iso_now()
            return datetime.fromtimestamp(best_ts, tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001 择时失败不阻塞，立即监听
            return iso_now()

    def _listing_ts(self, symbol: str) -> Optional[float]:
        """标的上市时间（本命盘基准）；manual 优先，其次 ohlcv 探测结果。"""
        try:
            lt = self.config.paipan.listing_times or {}
            manual = str(lt.get(symbol, "") or "").strip()
            if manual:
                dt = parse_iso(manual)
                return dt.timestamp()
        except Exception:
            pass
        return None

    def _evaluate_condition(
        self,
        cond: TriggerCondition,
        price: float,
        rsi: Optional[float],
        volume_ratio: Optional[float],
        now_iso: str,
    ) -> bool:
        """评估单条触发条件是否命中。price 为最新价；ref_price 为条件基准。"""
        kind = cond.kind
        if kind in {"price_above", "price_below"}:
            if price <= 0 or cond.value <= 0:
                return False
            return price >= cond.value if kind == "price_above" else price <= cond.value
        if kind in {"change_pct_above", "change_pct_below"}:
            base = cond.ref_price if cond.ref_price > 0 else price
            if base <= 0:
                return False
            pct = (price - base) / base
            return pct >= cond.value if kind == "change_pct_above" else pct <= cond.value
        if kind in {"rsi_above", "rsi_below"}:
            if rsi is None:
                return False
            return rsi >= cond.value if kind == "rsi_above" else rsi <= cond.value
        if kind == "volume_surge":
            if volume_ratio is None:
                return False
            return volume_ratio >= cond.value
        if kind == "time_after":
            # 用 datetime 比较，杜绝字符串比较的误判（如空串恒真、纯数字串）；
            # 无法解析的 time_after 一律不触发，等待下次 AI 重新设定。
            target = parse_iso(str(cond.value))
            now = parse_iso(now_iso)
            if target is None or now is None:
                return False
            return now >= target
        return False

    def _trigger_poll_loop(self) -> None:
        """信号触发主循环：每 trigger_poll_seconds 拉全量 ticker，评估触发条件。

        入场命中 -> 执行买入（记录成交均价为基准价）-> 唤醒 AI-2 设定出场条件。
        出场命中 -> 执行卖出 -> 唤醒 AI-1 重新评估入场。
        """
        self.log.warning(
            "信号触发模式启动 | poll=%ds max_wait=%.1fh symbols=%d",
            self.config.runtime.trigger_poll_seconds,
            self.config.runtime.trigger_max_wait_hours,
            len(self.config.runtime.symbols),
        )
        # 启动时对账一次挂单，防止带未确认订单进入监听。
        try:
            self.gateway.reconcile_pending_orders()
        except OrderUncertainError:
            raise
        except Exception as exc:  # noqa: BLE001 对账失败不阻塞监听，下次 tick 再试
            self.log.warning("启动挂单对账失败，继续监听: %s", exc)
        consecutive_errors = 0
        while not self.stop_event.is_set():
            try:
                self._trigger_tick()
                consecutive_errors = 0
            except OrderUncertainError:
                raise
            except Exception as exc:  # noqa: BLE001 单轮失败不退出，退避重试
                consecutive_errors += 1
                retry = min(30 * (2 ** (consecutive_errors - 1)), 600)
                self.log.error(
                    "触发监听失败 (%d 次，%ds 后重试): %s",
                    consecutive_errors, retry, exc, exc_info=True,
                )
                atomic_write_json(self.state_dir / self.config.runtime.health_file, {
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "status": "degraded",
                    "timestamp": iso_now(),
                    "mode": "signal",
                    "consecutive_errors": consecutive_errors,
                    "retry_after_seconds": retry,
                    "error": str(exc),
                }, mode=0o644)
                time.sleep(retry)
                continue
            time.sleep(self.config.runtime.trigger_poll_seconds)

    def _sync_positions(self, snapshot: AccountSnapshot) -> None:
        """信号模式：以交易所实时快照为权威源，同步 state.positions。

        只保留快照中仍有余额的持仓（amount>0），并尽量延续历史 avg_entry /
        high_water / opened_at；仅在发生变化时落盘，避免每 2 秒重复写文件。
        """
        state = self.store.state
        new_positions: Dict[str, PositionState] = {}
        for key, pos in snapshot.positions.items():
            if pos.amount <= 0 or pos.quote_value < self.config.risk.dust_quote:
                continue  # 空仓 / dust 残留都不算持仓，避免误触发补设出场条件
            old = state.positions.get(key)
            new_positions[key] = PositionState(
                amount=pos.amount,
                avg_entry=old.avg_entry if old else pos.avg_entry,
                high_water=old.high_water if old and old.high_water > 0 else pos.high_water,
                opened_at=old.opened_at if old else iso_now(),
                updated_at=iso_now(),
                market=pos.market,
                side=pos.side,
                leverage=pos.leverage,
            )
        if new_positions != state.positions:
            state.positions = new_positions
            try:
                self.store.save()
            except Exception as exc:  # noqa: BLE001 落盘失败不阻断监听，下次 tick 重试
                self.log.warning("持仓状态同步落盘失败（下次 tick 重试）: %s", exc)

    def _trigger_tick(self) -> None:
        """单轮触发检查：刷新行情 -> 评估入场/出场 -> 执行。"""
        now_dt = datetime.now(timezone.utc)
        now_iso = iso_now()
        # 深度不足缓存按 UTC 日切：先清理昨日记录，当日新拒单再入缓存。
        self._prune_depth_blocked()
        # 挂单对账：信号模式同样需要核对 write-ahead 残留的 pending 订单
        # （进程崩溃/状态落盘失败后重启，必须确认订单是否已成交，禁止盲目重下）。
        self.gateway.reconcile_pending_orders()
        # 行情：全量 ticker 一次拉取（OKX 批量接口限频 20req/2s，2 秒轮询安全）。
        # 目标 = 白名单现货 + 状态内持仓键（spot 裸名 / swap:SYMBOL）。
        try:
            target_keys = list(self.config.runtime.symbols)
            for key in self.store.state.positions:
                if key not in target_keys:
                    target_keys.append(key)
            prices, changes = self.gateway.fetch_all_tickers_full(target_keys)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"批量行情拉取失败: {exc}") from exc
        snapshot = self.gateway.account_snapshot(
            {sym: prices[sym] for sym in self.config.runtime.symbols if prices.get(sym, 0.0) > 0}
        )
        # 信号模式以交易所实时快照为权威源：同步 state.positions，
        # 防止状态落盘失败/进程崩溃后本地持仓记录与交易所脱节。
        self._sync_positions(snapshot)
        # 已持仓基准名（spot 裸名 / swap 前缀都归一到裸名，避免重复入场）。
        # dust 残留（低于 dust_quote）不算持仓。
        managed_bases = {
            split_position_key(key)[1]
            for key, pos in snapshot.positions.items()
            if pos.amount > 0 and pos.quote_value >= self.config.risk.dust_quote
        }
        triggers = self.store.state.triggers
        # ---- 出场检查（先卖，释放资金） ----
        for key, pos in snapshot.positions.items():
            if pos.amount <= 0 or pos.quote_value < self.config.risk.dust_quote:
                continue  # 空仓 / dust 不参与出场评估
            base = split_position_key(key)[1]
            if base not in triggers:
                if (
                    self.store.state.trigger_skip_until.get(base)
                    and now_iso < self.store.state.trigger_skip_until[base]
                ):
                    continue  # AI-2 失败退避期内不重复唤醒
                # 重启恢复：已有持仓但尚无出场条件（例如程序升级前买入），
                # 以持仓成本（买入价基准）唤醒 AI-2 补出场条件；缺成本时用现价。
                self.log.warning(
                    "%s 已有持仓但无出场条件，以买入价 %.6f 唤醒 AI-2 补设",
                    key, pos.avg_entry or pos.price,
                )
                self._arm_exit_trigger(base, pos.price, pos.avg_entry or pos.price)
                continue
            # 已有出场条件但缺十项古法读数（如重启后 last_readings 清空）：
            # 主动调一次 AI-2 补 readings，让大屏雷达有数据可画。
            if base not in self.store.state.last_readings:
                if not (
                    self.store.state.trigger_skip_until.get(base)
                    and now_iso < self.store.state.trigger_skip_until[base]
                ):
                    self.log.info("%s 持仓但缺古法读数，唤醒 AI-2 补 readings", base)
                    self._arm_exit_trigger(base, pos.price, pos.avg_entry or pos.price)
        for symbol in list(triggers):
            ts = TriggerSet.from_dict(triggers[symbol])
            if symbol not in managed_bases:
                continue
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            rsi, vol_ratio = self._trigger_indicators(symbol)
            for cond in ts.exit:
                if self._evaluate_condition(cond, price, rsi, vol_ratio, now_iso):
                    self._execute_trigger_exit(symbol, price, cond)
                    break
        # ---- 入场检查 ----
        paused = (self.state_dir / "pause").exists()
        if paused:
            self.log.warning("检测到暂停标记 %s：本 tick 不开新仓，仅管理存量仓位",
                             self.state_dir / "pause")
        skip_until = self.store.state.trigger_skip_until
        for symbol in self.config.runtime.symbols:
            if symbol in managed_bases:
                continue  # 已持仓标的只管理出场
            if paused:
                continue
            if self._depth_blocked_today(symbol):
                continue  # 当日深度不足缓存
            # AI-1 冷却：古法判定暂不交易/调用失败，未到时间不重复唤醒。
            if skip_until.get(symbol) and now_iso < skip_until[symbol]:
                continue
            ts = triggers.get(symbol)
            if ts is None:
                # 无触发条件：AI-1 首次评估（带古法择时）。
                ts = self._arm_entry_trigger(symbol, prices.get(symbol, 0.0), now_dt, now_iso)
                if ts is None:
                    continue
                triggers[symbol] = ts.to_dict()
                self.store.save()
                continue
            ts = TriggerSet.from_dict(ts)
            # 古法择时：未到首次触发时刻不监听。
            if ts.first_trigger_at and now_iso < ts.first_trigger_at:
                continue
            # 超时：超过最长等待唤醒 AI-1 重新评估。
            created = ts.created_at
            if created:
                try:
                    created_dt = parse_iso(created)
                    if (now_dt - created_dt).total_seconds() > (
                        self.config.runtime.trigger_max_wait_hours * 3600
                    ):
                        self.log.warning("%s 触发条件超时，唤醒 AI-1 重新评估", symbol)
                        refreshed = self._arm_entry_trigger(
                            symbol, prices.get(symbol, 0.0), now_dt, now_iso
                        )
                        if refreshed is not None:
                            triggers[symbol] = refreshed.to_dict()
                            self.store.save()
                        continue
                except Exception:
                    pass
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            rsi, vol_ratio = self._trigger_indicators(symbol)
            for cond in ts.entry:
                if self._evaluate_condition(cond, price, rsi, vol_ratio, now_iso):
                    self._execute_trigger_entry(symbol, price, ts, cond)
                    break
        # 触发状态写盘 + 大屏健康报告
        self.store.save()
        self._write_signal_health(snapshot, prices, changes)

    def _trigger_indicators(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """轻量指标：RSI(14) 与成交量突增比（最新 1h 量 / 前 20 根均值）。

        缓存 60 秒：2 秒轮询下若每 tick 都拉 K 线，54 个标的会打爆 OKX
        K 线限频（40req/2s）；指标对秒级行情不敏感，60s 刷新足够。
        """
        cached = self._indicator_cache.get(symbol)
        if cached is not None and time.monotonic() - cached[0] < 60.0:
            return cached[1], cached[2]
        try:
            df = self.gateway.fetch_ohlcv(symbol, timeframe="1h", limit=30)
            if df is None or len(df) < 22:
                return None, None
            close = df["close"].astype(float)
            vol = df["volume"].astype(float)
            delta = close.diff()
            gain = delta.clip(lower=0.0).ewm(alpha=1 / 14, adjust=False).mean()
            loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / 14, adjust=False).mean()
            rsi = float(100.0 - 100.0 / (1.0 + gain.iloc[-1] / max(loss.iloc[-1], 1e-12)))
            avg_vol = float(vol.iloc[-21:-1].mean())
            vol_ratio = float(vol.iloc[-1] / avg_vol) if avg_vol > 0 else None
            self._indicator_cache[symbol] = (time.monotonic(), rsi, vol_ratio)
            return rsi, vol_ratio
        except Exception:  # noqa: BLE001 指标失败不阻断价格触发
            return None, None

    def _arm_entry_trigger(
        self,
        symbol: str,
        price: float,
        now_dt,
        now_iso: str,
    ) -> Optional[TriggerSet]:
        """AI-1 入场评估 + 古法择时。返回触发条件集；暂不交易/失败返回 None 并写冷却。"""
        try:
            paipan_payload = self._paipan_payload(symbol, now_dt) if self.paipan_service else None
        except Exception:  # noqa: BLE001
            paipan_payload = None
        position = self.store.state.positions.get(symbol)
        position_quote = float(position.amount * price) if position else 0.0
        conditions, summary, decision, target_level, readings = self.ai.decide_entry(
            symbol, price, paipan_payload,
            self._last_equity(), position_quote, now_iso,
        )
        # 缓存十项古法读数供大屏雷达使用（按币种存储）。
        if readings:
            self.store.state.last_readings[symbol] = readings
            self.store.save()
        skip = self.store.state.trigger_skip_until
        if decision == "error":
            # AI 调用/响应失败：退避 10 分钟再问，避免每 2 秒轮询打爆限频。
            skip[symbol] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            self.store.save()
            return None
        if decision == "no_trade":
            # 古法判定今日不宜交易：冷却到次日 UTC 零点，当天不再问 AI-1。
            next_day = datetime.combine(
                utc_now().date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
            )
            skip[symbol] = next_day.isoformat()
            self.log.warning("AI-1 %s 今日不宜交易，次日 %s 再评估（%s）", symbol, next_day.date(), summary)
            self.store.save()
            return None
        if decision == "wait" and not conditions:
            # 继续等待且无条件：30 分钟后再问。
            skip[symbol] = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            self.log.warning("AI-1 %s 继续等待（%s）", symbol, summary or decision)
            self.store.save()
            return None
        first_at = self._auspicious_first_hour(symbol, now_dt)
        ts = TriggerSet(
            symbol=symbol,
            entry=conditions,
            entry_target=1.0 if target_level == "FULL" else 0.5,
            first_trigger_at=first_at,
            created_at=now_iso,
            updated_at=now_iso,
        )
        self.store.state.trigger_skip_until.pop(symbol, None)
        return ts

    @staticmethod
    def _chart_summary(chart: Mapping[str, Any]) -> Dict[str, Any]:
        """把单法盘面 dict 精简为传给 AI 的摘要（控制 token 与响应体量）。"""
        out: Dict[str, Any] = {}
        for key, value in chart.items():
            if key in {"method", "chart_type", "error"}:
                out[key] = value
            elif isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, list) and len(value) <= 8:
                out[key] = [
                    v for v in value if isinstance(v, (str, int, float, bool))
                ][:8]
        return out

    def _paipan_payload(self, symbol: str, now_dt) -> Optional[Dict[str, Any]]:
        """构建传给 AI 的盘面摘要（十项 method -> 核心字段，本命盘+时空盘）。"""
        if self.paipan_service is None:
            return None
        try:
            chart = self.paipan_service.paipan(
                symbol, now_dt=now_dt, listing_ts=self._listing_ts(symbol)
            )
            data = chart.to_dict() if hasattr(chart, "to_dict") else {}
            if not isinstance(data, dict):
                return None
            current = data.get("current") if isinstance(data.get("current"), dict) else {}
            natal = data.get("natal") if isinstance(data.get("natal"), dict) else {}
            payload: Dict[str, Any] = {
                "time_label": data.get("time_label", ""),
                "diagnostics": data.get("diagnostics", {}),
            }
            for name, item in current.items():
                if isinstance(item, dict):
                    payload[f"current_{name}"] = self._chart_summary(item)
            for name, item in natal.items():
                if isinstance(item, dict):
                    payload[f"natal_{name}"] = self._chart_summary(item)
            return payload
        except Exception:  # noqa: BLE001 排盘失败不阻断 AI 决策（无盘面也问）
            return None

    def _last_equity(self) -> float:
        try:
            return float(self.store.state.last_equity or 0.0)
        except Exception:
            return 0.0

    def _execute_trigger_entry(
        self, symbol: str, price: float, ts: TriggerSet, cond: TriggerCondition
    ) -> None:
        """入场触发命中：按目标仓位买入，成交均价回填为 ref_price，唤醒 AI-2 设出场。"""
        self.log.warning(
            "入场触发 %s | cond=%s value=%s price=%.4f target=%.0f%%",
            symbol, cond.kind, cond.value, price, ts.entry_target * 100,
        )
        self._prune_depth_blocked()
        snapshot = self.gateway.account_snapshot(
            {sym: p for sym, p in self.gateway.fetch_all_tickers(
                self.config.runtime.symbols).items() if p > 0}
        )
        plan: Optional[OrderPlan] = None
        try:
            plan = self.gateway.plan_rebalance(
                snapshot, symbol, ts.entry_target,
                f"信号触发入场: {cond.kind}={cond.value} {cond.note or ''}",
                market=ts.entry_market, leverage=ts.entry_leverage,
            )
            if plan is None:
                return
            fill = self.gateway.execute(plan)
        except OrderUncertainError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 深度不足/滑点超限：反馈 AI 仲裁缩量买入或放弃（只缩一次，仍失败则当日屏蔽）。
            if self._retry_downsize_entry(symbol, plan, price, ts, cond, exc):
                return
            # 非深度/滑点类失败（交易所限额、No market data 等）：当日屏蔽避免刷屏重试。
            self.log.error("触发入场下单失败 %s: %s", symbol, exc)
            self._block_depth_today(symbol, str(exc))
            append_jsonl(self.audit_path, {
                "ts": iso_now(), "event": "trigger_entry_skip",
                "symbol": symbol, "reason": "下单失败",
                "error": repr(exc),
            })
            return
        avg = float(fill.average_price or 0.0) or price
        append_jsonl(self.audit_path, {
            "ts": iso_now(), "event": "trigger_entry",
            "symbol": symbol, "condition": cond.to_dict(),
            "plan": asdict(plan), "fill": asdict(fill),
        })
        self._notify("trigger_entry", {"symbol": symbol, "side": "buy", "avg": avg})
        # 唤醒 AI-2 设定出场条件（买入价为基准）。
        self._arm_exit_trigger(symbol, price, avg)
        self._update_equity_from_fill(fill)

    def _retry_downsize_entry(
        self,
        symbol: str,
        plan: Optional[OrderPlan],
        price: float,
        ts: TriggerSet,
        cond: TriggerCondition,
        exc: BaseException,
    ) -> bool:
        """深度不足/滑点超限时，向 AI 仲裁缩量买入或放弃。

        返回 True 表示异常已按 AI 决策处理（缩量成交 / 判定放弃），调用方不再打 ERROR；
        返回 False 表示非深度/滑点类异常，由调用方走原失败路径。
        """
        message = str(exc)
        is_depth = "订单簿深度不足以成交" in message
        is_slippage = "预计滑点" in message
        if plan is None or not (is_depth or is_slippage):
            return False
        reason = "深度不足" if is_depth else "滑点超限"
        info = self.gateway.order_book_fill_info(
            plan.symbol, plan.side, plan.amount, plan.reference_price
        )
        if info is None:
            self.log.warning("%s 订单簿评估失败，保守放弃（%s）", symbol, reason)
            self._block_depth_today(symbol, message)
            return True
        action, amount_pct = self.ai.decide_downsize(
            symbol,
            reason,
            plan.amount,
            plan.estimated_quote,
            info["fillable_amount"],
            info["fillable_quote"],
            info["slippage_bps"],
            self.config.risk.max_slippage_bps,
            iso_now(),
        )
        if action != "buy" or amount_pct <= 0:
            self.log.warning("AI 判定放弃 %s（%s）：当日本日不再重试", symbol, reason)
            self._block_depth_today(symbol, message)
            append_jsonl(self.audit_path, {
                "ts": iso_now(), "event": "trigger_entry_skip",
                "symbol": symbol, "reason": reason,
                "plan": asdict(plan), "ai_action": "skip",
            })
            return True
        # AI 判定缩量买入：构造缩量计划重试一次（数量×amount_pct）。
        new_amount = plan.amount * amount_pct
        new_plan = OrderPlan(
            symbol=plan.symbol,
            side=plan.side,
            amount=new_amount,
            reference_price=plan.reference_price,
            estimated_quote=plan.estimated_quote * amount_pct,
            target_allocation=plan.target_allocation * amount_pct,
            current_allocation=plan.current_allocation,
            reason=f"{plan.reason}（AI 缩量 {amount_pct:.0%}）",
            market=plan.market,
            leverage=plan.leverage,
            contracts=0.0,
        )
        try:
            fill = self.gateway.execute(new_plan)
        except Exception as exc2:  # noqa: BLE001 缩量后仍失败，当日屏蔽不再重试
            self.log.error("AI 缩量后仍失败 %s: %s", symbol, exc2)
            self._block_depth_today(symbol, message)
            append_jsonl(self.audit_path, {
                "ts": iso_now(), "event": "trigger_entry_skip",
                "symbol": symbol, "reason": reason,
                "plan": asdict(plan), "ai_action": "buy", "amount_pct": amount_pct,
                "error": repr(exc2),
            })
            return True
        avg = float(fill.average_price or 0.0) or price
        append_jsonl(self.audit_path, {
            "ts": iso_now(), "event": "trigger_entry",
            "symbol": symbol, "condition": cond.to_dict(),
            "plan": asdict(new_plan), "fill": asdict(fill),
            "downsize": {"from_amount": plan.amount, "amount_pct": amount_pct},
        })
        self._notify("trigger_entry", {"symbol": symbol, "side": "buy", "avg": avg})
        self._arm_exit_trigger(symbol, price, avg)
        self._update_equity_from_fill(fill)
        return True

    def _block_depth_today(self, symbol: str, message: str) -> None:
        """薄盘/滑点拦截：当日记入 depth_blocked，本日不再重试买入（次日自动恢复）。"""
        self.store.state.depth_blocked[symbol] = utc_now().date().isoformat()
        self.store.save()
        append_jsonl(self.audit_path, {
            "ts": iso_now(), "event": "depth_blocked", "symbol": symbol, "error": message,
        })

    def _execute_trigger_exit(
        self, symbol: str, price: float, cond: TriggerCondition
    ) -> None:
        """出场触发命中：平仓该标的，移除触发状态，唤醒 AI-1 重新评估。"""
        self.log.warning(
            "出场触发 %s | cond=%s value=%s price=%.4f",
            symbol, cond.kind, cond.value, price,
        )
        snapshot = self.gateway.account_snapshot(
            {sym: p for sym, p in self.gateway.fetch_all_tickers(
                self.config.runtime.symbols).items() if p > 0}
        )
        try:
            market = (
                MARKET_SWAP
                if position_key(MARKET_SWAP, symbol) in snapshot.positions
                else MARKET_SPOT
            )
            plan = self.gateway.plan_rebalance(
                snapshot, symbol, 0.0,
                f"信号触发出场: {cond.kind}={cond.value} {cond.note or ''}",
                market=market,
            )
            if plan is None:
                return
            fill = self.gateway.execute(plan)
        except OrderUncertainError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.error("触发出场下单失败 %s: %s", symbol, exc)
            return
        append_jsonl(self.audit_path, {
            "ts": iso_now(), "event": "trigger_exit",
            "symbol": symbol, "condition": cond.to_dict(),
            "plan": asdict(plan), "fill": asdict(fill),
        })
        self._notify("trigger_exit", {"symbol": symbol, "side": "sell"})
        # 出场后清触发状态，下次 tick 由 AI-1 重新评估入场。
        # 注意：此时订单已成交，状态落盘失败只记警告，绝不误报"下单失败"。
        self.store.state.triggers.pop(symbol, None)
        try:
            self.store.save()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("出场状态落盘失败 %s（订单已成交，下次 tick 自动校正）: %s", symbol, exc)
        self._update_equity_from_fill(fill)

    def _arm_exit_trigger(
        self, symbol: str, current_price: float, ref_price: float
    ) -> None:
        """AI-2 出场决策：以买入价为基准设定出场条件。失败则保留现有触发并退避。"""
        try:
            now_dt = datetime.now(timezone.utc)
            paipan_payload = self._paipan_payload(symbol, now_dt) if self.paipan_service else None
        except Exception:  # noqa: BLE001
            paipan_payload = None
        position = self.store.state.positions.get(symbol)
        position_quote = float(position.amount * current_price) if position else 0.0
        exit_conds, summary, status, readings = self.ai.decide_exit(
            symbol, current_price, ref_price, paipan_payload,
            self._last_equity(), position_quote, iso_now(),
        )
        # 缓存十项古法读数（按持仓币种），供大屏雷达展示。
        if readings:
            self.store.state.last_readings[symbol] = readings
            self.store.save()
        if status == "error":
            # AI-2 调用/响应失败：保留现有出场条件不覆盖，10 分钟后重试。
            self.store.state.trigger_skip_until[symbol] = (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat()
            self.store.save()
            return
        ts = self.store.state.triggers.get(symbol)
        ts_obj = TriggerSet.from_dict(ts) if ts else TriggerSet(symbol=symbol)
        ts_obj.exit = exit_conds
        ts_obj.ref_price = ref_price
        ts_obj.updated_at = iso_now()
        self.store.state.triggers[symbol] = ts_obj.to_dict()
        self.store.save()

    def _update_equity_from_fill(self, fill) -> None:
        """成交后刷新账户权益快照（供 health 与后续决策参考）。"""
        try:
            snapshot = self.gateway.account_snapshot(
                {sym: p for sym, p in self.gateway.fetch_all_tickers(
                    self.config.runtime.symbols).items() if p > 0}
            )
            self.store.state.last_equity = snapshot.equity
            self.store.state.last_cycle_at = iso_now()
            self.store.save()
        except Exception:  # noqa: BLE001
            pass

    def _write_signal_health(self, snapshot: AccountSnapshot, prices: Mapping[str, float],
                             changes: Optional[Mapping[str, float]] = None) -> None:
        """信号模式健康报告（大屏/监控）：权益、持仓、触发条件概览。"""
        state = self.store.state
        triggers_view: Dict[str, Any] = {}
        for symbol, ts_dict in state.triggers.items():
            try:
                ts_obj = TriggerSet.from_dict(ts_dict)
            except Exception:  # noqa: BLE001 单条损坏不影响整体
                continue
            triggers_view[symbol] = {
                "entry_conditions": len(ts_obj.entry),
                "exit_conditions": len(ts_obj.exit),
                "entry_target": ts_obj.entry_target,
                "first_trigger_at": ts_obj.first_trigger_at,
                "ref_price": ts_obj.ref_price,
            }
        report = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "status": "halted" if state.halted_reason else "ok",
            "timestamp": iso_now(),
            "mode": "signal",
            "exchange": self.config.exchange.id,
            "sandbox": self.config.exchange.sandbox,
            "equity": round(snapshot.equity, 6),
            "quote_free": round(snapshot.quote_free, 6),
            "quotes": {
                sym: {
                    "price": round(pos.price, 10),
                    "value": round(pos.quote_value, 6),
                    "change_pct": round(changes.get(sym, 0.0), 4) if changes else 0.0,
                }
                for sym, pos in snapshot.positions.items()
            },
            "triggers": triggers_view,
            "fills": len(state.positions),
            "decisions": {
                sym: {"readings": rd} for sym, rd in state.last_readings.items()
            } if state.last_readings else {},
            "live_updated_at": iso_now(),
        }
        try:
            atomic_write_json(
                self.state_dir / self.config.runtime.health_file, report, mode=0o644
            )
        except Exception as exc:  # noqa: BLE001 健康报告失败不影响交易
            self.log.debug("信号模式健康报告写入失败（忽略）: %s", exc)
            return
        try:
            # 信号模式同样追加实时权益点，保证大屏权益曲线持续跳动。
            append_jsonl(self.state_dir / "equity.jsonl", {
                "ts": iso_now(),
                "equity": round(snapshot.equity, 6),
                "live": True,
            })
        except Exception as exc:  # noqa: BLE001
            self.log.debug("信号模式权益点追加失败（忽略）: %s", exc)

    def run_cycle(self) -> Dict[str, Any]:
        cycle_started = time.monotonic()
        # 深度不足缓存按 UTC 日切：先清理昨日记录，当日新拒单再入缓存。
        self._prune_depth_blocked()
        self.gateway.reconcile_pending_orders()

        # 第一阶段：每天用闭合日线扫描完整人工白名单。失败时 selected_symbols 为空，
        # 但下方仍会继续管理已有仓位，因此初选故障不会绕过保护性退出。
        daily_selection = self._daily_selection()

        # 账户估值与未管理余额检查始终覆盖完整白名单，不能只看当日入选标的。
        account_prices = self._account_prices()
        snapshot = self.gateway.account_snapshot(account_prices)
        self.gateway.update_high_water(snapshot)
        prev_halted = self.store.state.halted_reason
        risk_status = self.risk.evaluate(snapshot)
        if self.config.ai.decision_mode == "full":
            # AI 全权模式：账户级熔断（日内亏损/峰值回撤/成交次数上限）不阻止 AI 决策，
            # 仅保留评估原因供展示；ORDER_UNCERTAIN（订单状态未知）硬停仍保留。
            if risk_status.reason:
                self.log.warning(
                    "AI 全权模式：账户熔断评估已忽略（仅记录，不限制交易）: %s",
                    risk_status.reason,
                )
            if self.store.state.halted_reason and not self.store.state.halted_reason.startswith(
                "ORDER_UNCERTAIN"
            ):
                self.store.state.halted_reason = ""
            risk_status = RiskStatus(True, risk_status.reason)
        if self.store.state.halted_reason and self.store.state.halted_reason != prev_halted:
            self._notify("halted", {"reason": self.store.state.halted_reason})
        if self.config.ai.decision_mode == "full":
            # AI 全权模式：不启用保护性止损/止盈/移动止损，仓位完全交由 AI 决策。
            protective: Dict[str, str] = {}
        else:
            protective = self.risk.protective_exits(snapshot)

        # 暂停开关：state_dir/pause 存在时本周期不开新仓，仅继续管理存量与保护性退出。
        pause_file = self.state_dir / "pause"
        paused = pause_file.exists()
        if paused:
            self.log.warning("检测到暂停标记 %s：本周期不开新仓，仅管理存量仓位", pause_file)

        managed_positions = {
            symbol
            for symbol, position in snapshot.positions.items()
            if position.amount > 0
            and (
                position.quote_value > self.config.risk.dust_quote
                or symbol in self.store.state.positions
            )
        }
        selected = set() if paused else set(daily_selection.selected_symbols)
        fine_screen_symbols = selected | managed_positions

        # 第二阶段：只对“当日入选 + 已有受管仓位”使用原交易周期精筛。
        # _build_decisions 内只会对当日入选标的调用远程 AI；落选持仓不得加仓。
        _, results = self._prices_and_signals(fine_screen_symbols)
        decisions = self._build_decisions(
            snapshot,
            results,
            protective,
            risk_status,
            selected,
            daily_selection,
        )

        plans: List[OrderPlan] = []
        for symbol, decision in decisions.items():
            cooldown_ok, cooldown_reason = self.risk.cooldown_ok(symbol)
            if not cooldown_ok and symbol not in protective:
                self.log.info("%s 跳过调仓: %s", symbol, cooldown_reason)
                continue
            try:
                plan = self.gateway.plan_rebalance(
                    snapshot,
                    symbol,
                    decision.target_allocation,
                    decision.reason,
                    market=decision.market,
                    leverage=decision.leverage,
                )
                if plan:
                    if plan.side == "buy" and self._depth_blocked_today(symbol):
                        self.log.info(
                            "%s 当日订单簿深度不足，跳过买入（次日自动重试）: %s",
                            symbol,
                            self.store.state.depth_blocked.get(symbol, ""),
                        )
                        continue
                    plans.append(plan)
            except SafetyError as exc:
                self.log.warning("%s 无法生成订单: %s", symbol, exc)

        # 先卖后买，减少由于可用计价币不足导致的失败。
        plans.sort(key=lambda p: 0 if p.side == "sell" else 1)
        fills: List[FillResult] = []
        for plan in plans:
            if self.stop_event.is_set():
                break
            try:
                fill = self.gateway.execute(plan)
                fills.append(fill)
                audit = {
                    "ts": iso_now(),
                    "event": "order_fill",
                    "version": APP_VERSION,
                    "plan": asdict(plan),
                    "fill": asdict(fill),
                }
                append_jsonl(self.audit_path, audit)
                self._notify("order_fill", {
                    "symbol": plan.symbol,
                    "side": plan.side,
                    "filled_amount": fill.filled_amount,
                    "average_price": fill.average_price,
                    "status": fill.status,
                    "reason": plan.reason,
                })
                self.log.warning(
                    "%s %s filled=%s avg=%s status=%s sandbox=%s reason=%s",
                    plan.symbol, plan.side.upper(), fill.filled_amount,
                    fill.average_price, fill.status, self.config.exchange.sandbox, plan.reason,
                )
            except OrderUncertainError as exc:
                append_jsonl(self.audit_path, {
                    "ts": iso_now(),
                    "event": "order_uncertain",
                    "plan": asdict(plan),
                    "error": repr(exc),
                    "action": "automatic trading stopped; manual reconciliation required",
                })
                self.stop_event.set()
                self._notify("order_uncertain", {"symbol": plan.symbol, "error": repr(exc)})
                self.log.critical("订单状态不确定，立即停止自动交易: %s", exc, exc_info=True)
                raise
            except Exception as exc:
                append_jsonl(self.audit_path, {
                    "ts": iso_now(),
                    "event": "order_error",
                    "plan": asdict(plan),
                    "error": repr(exc),
                })
                # 薄盘深度不足：当日记入 depth_blocked，本日不再重试买入（次日自动恢复）。
                if plan.side == "buy" and self._is_depth_error(str(exc)):
                    self.store.state.depth_blocked[plan.symbol] = utc_now().date().isoformat()
                self.log.error("订单执行失败 %s %s: %s", plan.symbol, plan.side, exc, exc_info=True)

        state = self.store.state
        state.last_cycle_at = iso_now()
        state.last_scores = {symbol: result.score for symbol, result in results.items()}
        self.store.save()
        duration = time.monotonic() - cycle_started
        next_review_seconds = self._next_review_seconds(
            decisions, risk_status.allowed, protective, paused
        )
        report = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "status": (
                "halted"
                if state.halted_reason
                else ("degraded" if not daily_selection.complete else "ok")
            ),
            "timestamp": iso_now(),
            "mode": "exchange-sandbox" if self.config.exchange.sandbox else "exchange-production",
            "exchange": self.config.exchange.id,
            "sandbox": self.config.exchange.sandbox,
            "paused": paused,
            "equity": snapshot.equity,
            "quote_free": snapshot.quote_free,
            "risk_allowed": risk_status.allowed,
            "risk_reason": risk_status.reason,
            "daily_selection": daily_selection.to_dict(),
            "new_entry_symbols": [
                symbol for symbol in self.config.runtime.symbols if symbol in selected
            ],
            "managed_position_symbols": [
                symbol for symbol in self.config.runtime.symbols if symbol in managed_positions
            ],
            "fine_screen_symbols": [
                symbol for symbol in self.config.runtime.symbols if symbol in fine_screen_symbols
            ],
            "scores": {s: round(r.score, 6) for s, r in results.items()},
            "decisions": {
                s: {
                    "target_fraction": d.target_fraction,
                    "target_allocation": d.target_allocation,
                    "reason": d.reason,
                    "ai": d.ai_decision.to_dict(),
                } for s, d in decisions.items()
            },
            "protective_exits": protective,
            "fills": len(fills),
            "next_review_seconds": next_review_seconds,
            "cycle_seconds": round(duration, 3),
        }
        # 周期报告写盘时保留等待期的实时行情字段（quotes/live_updated_at），
        # 避免周期完成瞬间把大屏的实时价清空。
        try:
            prev_health = json.loads(
                (self.state_dir / self.config.runtime.health_file).read_text(encoding="utf-8")
            )
            if isinstance(prev_health, dict):
                if prev_health.get("quotes"):
                    report["quotes"] = prev_health["quotes"]
                if prev_health.get("live_updated_at"):
                    report["live_updated_at"] = prev_health["live_updated_at"]
        except Exception:
            pass  # 读不到旧 health 就不合并，实时字段由等待期下一次刷新补上
        atomic_write_json(self.state_dir / self.config.runtime.health_file, report, mode=0o644)
        # 权益曲线历史（追加式，供 stats 命令与外部监控使用）
        append_jsonl(self.state_dir / "equity.jsonl", {
            "ts": iso_now(),
            "equity": snapshot.equity,
            "quote_free": snapshot.quote_free,
            "risk_allowed": risk_status.allowed,
            "paused": paused,
            "status": report["status"],
            "fills": len(fills),
        })
        self.log.info(
            "周期完成 | equity=%.4f %s | fills=%d | risk=%s | selected=%s | scores=%s | next_review=%ds | %.2fs",
            snapshot.equity, self.config.runtime.quote_currency, len(fills),
            risk_status.reason or "OK",
            daily_selection.selected_symbols,
            {s: round(r.score, 3) for s, r in results.items()}, next_review_seconds, duration,
        )
        return report

    def run_forever(self) -> None:
        self.log.warning(
            "%s %s 启动 | exchange_mode=%s sandbox=%s symbols=%s selection=%s/%s/top%d/min%.3f fine=%s",
            APP_NAME, APP_VERSION,
            "sandbox" if self.config.exchange.sandbox else "production",
            self.config.exchange.sandbox,
            self.config.runtime.symbols,
            "on" if self.config.selection.enabled else "off",
            self.config.selection.timeframe,
            self.config.selection.top_n,
            self.config.selection.min_score,
            self.config.runtime.timeframe,
        )
        # 8.8.0：信号触发模式（默认）——不再按固定周期全量重算，
        # 由 AI-1（古法入场）+ AI-2（古法出场）预设触发条件，监听循环按最小间隔轮询。
        if self.config.runtime.trigger_mode == "signal":
            self._trigger_poll_loop()
            return
        consecutive_errors = 0
        retry_delay = self.config.runtime.poll_interval_seconds
        first = True
        while not self.stop_event.is_set():
            report: Optional[Dict[str, Any]] = None
            if first and not self.config.runtime.once_on_start:
                first = False
            else:
                try:
                    report = self.run_cycle()
                    consecutive_errors = 0
                    retry_delay = self.config.runtime.poll_interval_seconds
                except OrderUncertainError as exc:
                    atomic_write_json(self.state_dir / self.config.runtime.health_file, {
                        "app": APP_NAME,
                        "version": APP_VERSION,
                        "status": "order_uncertain",
                        "timestamp": iso_now(),
                        "error": str(exc),
                        "action": "manual reconciliation required before restart",
                    }, mode=0o644)
                    self.log.critical("订单状态不确定，服务不会自动重试: %s", exc)
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    # 8.3.0 起自动恢复：一般错误（网络抖动、限流、行情缺失等）
                    # 不再退出进程，改为指数退避后继续探测，恢复后自动继续交易。
                    # OrderUncertainError 仍强制退出（订单状态必须人工核对）。
                    retry_delay = min(30 * (2 ** (consecutive_errors - 1)), 600)
                    self.log.error(
                        "运行周期失败 (%d 次，%ds 后自动重试): %s",
                        consecutive_errors, retry_delay, exc,
                        exc_info=True,
                    )
                    atomic_write_json(self.state_dir / self.config.runtime.health_file, {
                        "app": APP_NAME,
                        "version": APP_VERSION,
                        "status": "degraded",
                        "timestamp": iso_now(),
                        "consecutive_errors": consecutive_errors,
                        "retry_after_seconds": retry_delay,
                        "error": str(exc),
                    }, mode=0o644)
            first = False
            wait_seconds = retry_delay
            if isinstance(report, dict):
                wait_seconds = int(
                    report.get("next_review_seconds") or self.config.runtime.poll_interval_seconds
                )
            # 等待期间每 poll_interval 轻量刷新行情/权益（不调用 AI、不排盘），
            # 让大屏/监控数据实时更新，避免复查间隔内数据冻结。
            self._wait_until_review(wait_seconds)
        self.log.warning("服务已安全停止")

    def _wait_until_review(self, wait_seconds: int) -> None:
        """周期间等待：每 poll_interval_seconds 轻量刷新一次行情/权益。

        只更新 health.json 的 equity/quote_free/quotes/live_updated_at 与
        equity.jsonl 实时点；不调用 AI、不排盘、不决策、不改变复查节奏。
        """
        deadline = time.monotonic() + max(0, wait_seconds)
        interval = max(10, self.config.runtime.poll_interval_seconds)
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            if now >= next_tick:
                next_tick = now + interval
                try:
                    self._refresh_live_quotes()
                except Exception as exc:  # noqa: BLE001 轻量刷新失败不影响主流程
                    self.log.debug("轻量行情刷新失败（忽略，下次再试）: %s", exc)
            self.stop_event.wait(min(interval, max(0.1, deadline - now)))

    def _refresh_live_quotes(self) -> None:
        """拉取持仓/选中标的实时价格，更新 health.json 实时字段并追加实时权益点。

        只写 equity/quote_free/quotes/live_updated_at，保留周期报告其余字段；
        任一步失败都放弃本次刷新，绝不让实时数据覆盖或破坏周期报告。
        """
        state = self.store.state
        health_path = self.state_dir / self.config.runtime.health_file
        targets: List[str] = []
        for sym in state.positions:
            if sym not in targets:
                targets.append(sym)
        for sym in (state.daily_selected_symbols or [])[:5]:
            if sym not in targets:
                targets.append(sym)
        if not targets:
            return
        health: Dict[str, Any] = {}
        try:
            if health_path.exists():
                health = json.loads(health_path.read_text(encoding="utf-8"))
        except Exception:
            return  # health 不可读则不动，避免覆盖周期报告
        if not isinstance(health, dict):
            return
        try:
            # 状态键可能是 swap:XXX/USDT（合约持仓）；转成交易所符号拉价，
            # 再把价格映射回状态键（spot 裸符号、swap 带前缀）。
            exchange_targets: Dict[str, str] = {}
            for sym in targets:
                market, base = split_position_key(sym)
                exchange_targets[self.gateway.exchange_symbol(market, base)] = sym
            tickers = self.gateway.client.fetch_tickers(list(exchange_targets))
            prices: Dict[str, float] = {}
            changes: Dict[str, float] = {}
            for ex_sym, ticker in (tickers or {}).items():
                state_key = exchange_targets.get(str(ex_sym), str(ex_sym))
                price = finite(ticker.get("last") or ticker.get("close"))
                if price > 0:
                    prices[state_key] = price
                    pct = finite(ticker.get("percentage"))
                    if -100 <= pct <= 1000:
                        changes[state_key] = pct
            if not prices:
                return
            snap = self.gateway.account_snapshot(prices)
        except Exception as exc:
            self.log.warning("实时行情刷新失败: %s", exc)
            return
        health["equity"] = round(snap.equity, 6)
        health["quote_free"] = round(snap.quote_free, 6)
        health["quotes"] = {
            sym: {
                "price": round(pos.price, 10),
                "value": round(pos.quote_value, 6),
                "change_pct": round(changes.get(sym, 0.0), 4),
            }
            for sym, pos in snap.positions.items()
        }
        health["live_updated_at"] = iso_now()
        try:
            atomic_write_json(health_path, health, mode=0o644)
            append_jsonl(self.state_dir / "equity.jsonl", {
                "ts": iso_now(),
                "equity": round(snap.equity, 6),
                "live": True,
            })
        except Exception as exc:
            self.log.warning("实时行情写入失败: %s", exc)


# =============================================================================
# CLI
# =============================================================================


def default_config_dict() -> Dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "exchange": asdict(ExchangeConfig()),
        "runtime": asdict(RuntimeConfig()),
        "selection": asdict(DailySelectionConfig()),
        "strategy": asdict(StrategyConfig()),
        "risk": asdict(RiskConfig()),
        "ai": asdict(AIConfig()),
        "paipan": asdict(PaipanConfig()),
    }


def terminal_is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _prompt_text(
    label: str,
    default: str = "",
    required: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
) -> str:
    reader = input_fn or input
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = reader(f"{label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigError("配置向导已取消") from exc
        value = raw or default
        if value or not required:
            return value
        print(f"{label} 不能为空。", file=sys.stderr)


def _prompt_bool(
    label: str,
    default: bool,
    input_fn: Optional[Callable[[str], str]] = None,
) -> bool:
    reader = input_fn or input
    suffix = "Y/n" if default else "y/N"
    while True:
        try:
            raw = reader(f"{label} [{suffix}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigError("配置向导已取消") from exc
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true", "是"}:
            return True
        if raw in {"n", "no", "0", "false", "否"}:
            return False
        print("请输入 y 或 n。", file=sys.stderr)


def _prompt_secret(
    label: str,
    name: str,
    store: CredentialStore,
    required: bool,
    secret_input_fn: Optional[Callable[[str], str]] = None,
) -> None:
    reader = secret_input_fn or getpass.getpass
    env_value = os.getenv(name, "").strip()
    if env_value and not store.stored(name):
        store.set(name, env_value)
    while True:
        source = store.source(name)
        hint = "已配置，回车保留；输入 - 清除" if source != "missing" else "输入时不会回显"
        try:
            value = reader(f"{label} ({name}，{hint}): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigError("配置向导已取消") from exc
        if value == "-":
            if required and not env_value:
                print(f"{label} 是必填项，不能清除。", file=sys.stderr)
                continue
            store.set(name, "")
            return
        if value:
            store.set(name, value)
            return
        if store.source(name) != "missing":
            return
        if not required:
            return
        print(f"{label} 是必填项。", file=sys.stderr)


def required_credential_names(config: AppConfig) -> List[Tuple[str, str]]:
    required = [
        (config.exchange.api_key_env, "交易所 API Key"),
        (config.exchange.secret_env, "交易所 API Secret"),
    ]
    if config.ai.enabled:
        required.append((config.ai.api_key_env, "AI API Key"))
    return required


def missing_credentials(config: AppConfig, store: CredentialStore) -> List[Tuple[str, str]]:
    return [
        (name, label)
        for name, label in required_credential_names(config)
        if store.source(name) == "missing"
    ]


def run_setup_wizard(
    config_path: Path,
    *,
    require_tty: bool = True,
    input_fn: Optional[Callable[[str], str]] = None,
    secret_input_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[AppConfig, CredentialStore]:
    if require_tty and not terminal_is_interactive():
        raise ConfigError("setup 必须在交互终端运行，以免密钥被回显或卡住服务进程")

    existing = config_path.exists()
    payload = load_json(config_path) if existing else default_config_dict()
    if existing:
        AppConfig.from_dict(payload)
    exchange = require_json_object(payload["exchange"], "exchange")
    ai = require_json_object(payload["ai"], "ai")
    store = CredentialStore(default_credentials_path(config_path))

    print("\n=== GuFaQuant-Pro 首次配置向导 ===")
    print("密钥将保存到独立的 0600 凭据文件，不会写入 config.json 或日志。")
    print("注意：这是本地明文权限保护，不是操作系统加密保险库。")

    exchange["id"] = _prompt_text(
        "交易所 CCXT ID", str(exchange.get("id", "okx")), True, input_fn
    ).lower()
    if bool(exchange.get("sandbox", True)):
        print("交易环境：Sandbox/Testnet/Demo 模拟盘（安全默认）")
    else:
        print("警告：现有配置是正式盘；向导不会替你更改正式盘确认。", file=sys.stderr)

    _prompt_secret(
        "交易所 API Key", str(exchange["api_key_env"]), store, True, secret_input_fn
    )
    _prompt_secret(
        "交易所 API Secret", str(exchange["secret_env"]), store, True, secret_input_fn
    )
    _prompt_secret(
        "交易所 Passphrase（不需要可留空）",
        str(exchange["password_env"]), store, False, secret_input_fn,
    )

    ai_enabled = _prompt_bool("启用 AI 十项古法解读", bool(ai.get("enabled", False)), input_fn)
    ai["enabled"] = ai_enabled
    if ai_enabled:
        ai["base_url"] = _prompt_text(
            "OpenAI 兼容中转站 Base URL（官方 OpenAI 可留空）",
            str(ai.get("base_url", "")), False, input_fn,
        ).rstrip("/")
        ai["model"] = _prompt_text(
            "模型 ID", str(ai.get("model", "gpt-4.1-mini")), True, input_fn
        )
        _prompt_secret(
            "AI / 中转站 API Key", str(ai["api_key_env"]), store, True, secret_input_fn
        )

    config = AppConfig.from_dict(payload)
    store.save()
    atomic_write_json(config_path, payload, mode=0o600)
    print(f"\n配置已保存: {config_path}")
    print(f"凭据已保存: {store.path}")
    print(f"AI: {'启用' if config.ai.enabled else '禁用'} | model={config.ai.model}")
    print("后续运行无需重复设置环境变量；如设置同名环境变量，它会临时覆盖已保存值。")
    return config, store


def fetch_ai_model_ids(config: AIConfig, store: CredentialStore) -> List[str]:
    if not config.base_url:
        raise ConfigError("未配置 AI 中转站 base_url；请直接使用 model set MODEL_ID")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ConfigError("查询模型需要安装 openai") from exc
    key = store.resolve(config.api_key_env, True, "AI API")
    client = OpenAI(api_key=key, base_url=config.base_url, timeout=config.timeout_seconds)
    try:
        response = client.models.list()
    except Exception as exc:
        raise ConfigError(
            "中转站模型列表查询失败；可直接执行 model set MODEL_ID。"
            f"原始错误: {exc}"
        ) from exc
    ids = sorted({
        str(getattr(item, "id", "")).strip()
        for item in getattr(response, "data", [])
        if str(getattr(item, "id", "")).strip()
    })
    if not ids:
        raise ConfigError("中转站返回的模型列表为空；请直接使用 model set MODEL_ID")
    return ids


def set_ai_model(config_path: Path, model_id: str) -> AppConfig:
    model = model_id.strip()
    if not model:
        raise ConfigError("模型 ID 不能为空")
    payload = load_json(config_path)
    ai = require_json_object(payload.get("ai", {}), "ai")
    ai["model"] = model
    config = AppConfig.from_dict(payload)
    atomic_write_json(config_path, payload, mode=0o600)
    return config


def select_model_interactively(
    models: Sequence[str],
    current: str,
    input_fn: Optional[Callable[[str], str]] = None,
) -> str:
    reader = input_fn or input
    print(f"当前模型: {current}")
    for index, model in enumerate(models, 1):
        marker = " *" if model == current else ""
        print(f"{index:3d}. {model}{marker}")
    while True:
        try:
            raw = reader("输入模型编号（直接回车取消）: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigError("模型切换已取消") from exc
        if not raw:
            raise ConfigError("模型切换已取消，配置未修改")
        try:
            index = int(raw)
        except ValueError:
            print("请输入有效编号。", file=sys.stderr)
            continue
        if 1 <= index <= len(models):
            return models[index - 1]
        print("模型编号超出范围。", file=sys.stderr)


def parse_entry_pairs(values: Sequence[str]) -> Dict[str, float]:
    entries: Dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError(f"--entry 格式必须为 SYMBOL=PRICE: {value}")
        symbol, price = value.rsplit("=", 1)
        entries[symbol.strip()] = float(price)
    return entries


# =============================================================================
# 8.1 排盘报告与古法信号回测（只读：不连接私密 API、不下单）
# =============================================================================


def _split_symbols(text: str, fallback: Sequence[str]) -> List[str]:
    symbols = [s.strip() for s in text.split(",") if s.strip()]
    return symbols or list(fallback)


def cmd_export_ohlcv(
    config: AppConfig,
    symbols: Sequence[str],
    timeframe: str,
    bars: int,
    output: Optional[str],
) -> int:
    """导出公开 K 线到 CSV（列与 backtest-paipan --ohlcv-file 兼容）。只读公开行情，无需凭据。"""
    if not hasattr(ccxt, config.exchange.id):
        raise ConfigError(f"CCXT 不支持交易所: {config.exchange.id}")
    exchange = getattr(ccxt, config.exchange.id)({
        "enableRateLimit": True,
        "timeout": config.exchange.timeout_ms,
        "options": {"defaultType": "spot"},
        **({"proxies": {"http": config.exchange.effective_proxies()[0], "https": config.exchange.effective_proxies()[0]}}
           if config.exchange.effective_proxies() else {}),
    })
    rows: List[List[Any]] = []
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=bars)
        except Exception as exc:  # noqa: BLE001 网络/交易所错误统一转为友好提示
            raise ConfigError(f"拉取 {symbol} {timeframe} 行情失败: {exc}") from exc
        if len(ohlcv) < 2:
            raise ConfigError(f"{symbol} {timeframe} 有效 K 线不足: {len(ohlcv)}")
        for candle in ohlcv:
            ts = datetime.fromtimestamp(candle[0] / 1000.0, timezone.utc).isoformat()
            rows.append([symbol, ts] + [float(x) for x in candle[1:6]])
    if output:
        path = Path(output).expanduser().resolve()
    else:
        state_dir = Path(config.runtime.state_dir).expanduser().resolve()
        path = state_dir / f"ohlcv_{timeframe}_{utc_now().strftime('%Y%m%d_%H%M%S')}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "date", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
    print(f"已导出 {len(rows)} 根 {timeframe} K 线（{', '.join(symbols)}）→ {path}")
    return 0


def cmd_pause_resume(state_dir: Path, resume: bool) -> int:
    """暂停/恢复：创建或移除 state_dir/pause 标记文件。"""
    pause_file = state_dir / "pause"
    if resume:
        if pause_file.exists():
            pause_file.unlink()
            print(f"已恢复自动交易（移除 {pause_file}）")
        else:
            print("当前未暂停")
    else:
        pause_file.parent.mkdir(parents=True, exist_ok=True)
        pause_file.touch()
        print(f"已暂停新开仓（标记 {pause_file}）；存量仓位与保护性退出继续管理。用 resume 恢复。")
    return 0


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读取 JSONL 文件；容忍空行与损坏行（进程中断时末行可能不完整）。"""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def cmd_stats(state_dir: Path, health_file: str) -> int:
    """汇总权益曲线与成交审计（只读，供运维与回测观察）。"""
    equity_rows = _read_jsonl(state_dir / "equity.jsonl")
    audit_rows = _read_jsonl(state_dir / "orders.audit.jsonl")
    # 8.6.3 起 equity.jsonl 含周期点与实时点（live=true），
    # 统计语义保持"周期级"：收益/回撤只看周期点。
    cycle_rows = [row for row in equity_rows if not row.get("live")]
    summary: Dict[str, Any] = {"cycles": len(cycle_rows)}
    live_count = len(equity_rows) - len(cycle_rows)
    if live_count:
        summary["live_quote_points"] = live_count
    if cycle_rows:
        first, last = cycle_rows[0], cycle_rows[-1]
        summary["period"] = {"first": first.get("ts"), "last": last.get("ts")}
        eq_first = first.get("equity")
        eq_last = last.get("equity")
        summary["equity"] = {"first": eq_first, "last": eq_last}
        if isinstance(eq_first, (int, float)) and isinstance(eq_last, (int, float)) and eq_first > 0:
            summary["return_pct"] = round((eq_last / eq_first - 1) * 100, 4)
        peak = float("-inf")
        max_dd = 0.0
        for row in cycle_rows:
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_paipan_report(
    config: AppConfig,
    symbols: Sequence[str],
    now_iso: Optional[str],
    fmt: str,
    output: Optional[str],
) -> int:
    """离线生成完整盘面 + 信号 + 断卦要点报告，供人工审计与排盘校验。"""
    if not config.paipan.enabled and not PAIPAN_AVAILABLE:
        raise ConfigError("paipan 模块不可用")
    svc = build_paipan_service(config.paipan)
    now_dt = parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "generated_at": utc_now().isoformat(),
        "report_time": now_dt.isoformat(),
        "ai_model": config.ai.model or "",
        "paipan": {
            "true_solar_time": config.paipan.true_solar_time,
            "longitude": config.paipan.longitude,
            "latitude": config.paipan.latitude,
            "listing_time_source": config.paipan.listing_time_source,
        },
        "symbols": [],
    }
    for symbol in symbols:
        result = svc.paipan(symbol, now_dt=now_dt)
        entry = result.to_dict()
        entry["signals"] = paipan_signals(entry)
        entry["verdicts"] = paipan_verdicts(entry)
        if not entry.get("natal"):
            entry["diagnostics"]["natal_missing"] = "需 ohlcv 行情或 paipan.listing_times 手工指定"
        payload["symbols"].append(entry)
    text = _report_markdown(payload) if fmt == "markdown" else json.dumps(payload, ensure_ascii=False, indent=2)
    return _emit(text, output)


def _report_markdown(payload: Dict[str, Any]) -> str:
    lines = [
        f"# {APP_NAME} 排盘报告",
        f"- 版本: {payload['version']}  生成: {payload['generated_at']}",
        f"- 报告基准时间: {payload['report_time']}",
        f"- 真太阳时修正: {'开' if payload['paipan']['true_solar_time'] else '关'} "
        f"（经度 {payload['paipan']['longitude']}，纬度 {payload['paipan']['latitude']}）",
        f"- 本命盘来源: {payload['paipan']['listing_time_source']}",
        "",
    ]
    for entry in payload["symbols"]:
        symbol = entry["symbol"]
        lines.append(f"## {symbol} —— 时空盘 {entry['time_label']}")
        lines.append("")
        lines.append("| 古法 | 置信度 | 断卦要点 |")
        lines.append("|---|---|---|")
        for method in STRATEGY_NAMES:
            score = entry["signals"].get(method, 0.5)
            verdict = entry["verdicts"].get(method, "")
            lines.append(f"| {method} | {score:.2f} | {verdict} |")
        lines.append("")
        lines.append(f"### 本命盘（{symbol}）")
        natal = entry.get("natal") or {}
        if not natal:
            lines.append(f"未生成：{entry.get('diagnostics', {}).get('natal_missing', '无上市时间')}")
        else:
            for method in STRATEGY_NAMES:
                chart = natal.get(method) or {}
                key = {
                    "奇门": ("dun", "ju"), "六壬": ("yuejiang",), "太乙": ("taiyi_gong",),
                    "易经": ("ben_gua",), "风水": ("year_star",), "八字": ("day_master", "strength"),
                    "梅花": ("ti_yong_relation",), "紫微": ("palaces",), "八卦": ("gua_gong",),
                    "四柱": ("ganzhi",),
                }.get(method, ())
                summary = "、".join(str(chart.get(k)) for k in key if chart.get(k))
                lines.append(f"- {method}: {summary or '（见完整 JSON）'}")
        lines.append("")
    lines.append(
        f"> 排盘过程真实可复现；断卦解读由大模型生成（model={payload.get('ai_model') or '未配置'}）；"
        "预测准确性不保证，不构成投资建议。"
    )
    return "\n".join(lines)


def cmd_backtest_paipan(
    config: AppConfig,
    symbols: Sequence[str],
    bars: int,
    days: int,
    min_score: float,
    fmt: str,
    output: Optional[str],
    ohlcv_file: Optional[str] = None,
) -> int:
    """逐日排盘回测：十项古法信号 vs 未来 N 日收益（公开历史行情，不下单）。

    统计指标仅供观察古法信号与历史走势的统计关联，不构成预测保证。
    """
    if bars < 30:
        raise ConfigError("--bars 至少 30")
    if days < 1:
        raise ConfigError("--days 至少 1")
    if not 0.0 < min_score < 1.0:
        raise ConfigError("--min-score 必须在 (0,1)")
    if not PAIPAN_AVAILABLE:
        raise ConfigError("排盘模块不可用: " + str(_PAIPAN_IMPORT_ERROR))
    svc = build_paipan_service(config.paipan)
    rows: List[Dict[str, Any]] = []
    if ohlcv_file:
        ohlcv_map = _load_ohlcv_file(ohlcv_file)
        for symbol in symbols:
            if symbol not in ohlcv_map:
                raise ConfigError(f"CSV 中无 {symbol} 数据（可用列: {sorted(ohlcv_map)}）")
            ohlcv = ohlcv_map[symbol]
            print(f"[backtest] {symbol}: {len(ohlcv)} 根日线（CSV）", file=sys.stderr)
            rows.extend(_ohlcv_to_rows(svc, symbol, ohlcv, days))
    else:
        exchange_id = config.exchange.id
        if not hasattr(ccxt, exchange_id):
            raise ConfigError(f"CCXT 不支持交易所: {exchange_id}")
        # 公开行情回测：不设 sandbox（用真实历史数据），无需 API 凭据。
        exchange = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "timeout": config.exchange.timeout_ms,
            "options": {"defaultType": "spot"},
            **({"proxies": {"http": config.exchange.effective_proxies()[0], "https": config.exchange.effective_proxies()[0]}}
               if config.exchange.effective_proxies() else {}),
        })
        limit = min(bars, 300)  # OKX 单次日线上限 300
        for symbol in symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, "1d", limit=limit)
            except Exception as exc:  # noqa: BLE001 - 网络/交易所异常统一转为清晰报错
                raise ConfigError(
                    f"拉取 {symbol} 历史行情失败（请检查网络/交易所可达性）："
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not ohlcv or len(ohlcv) < 30:
                raise ConfigError(f"{symbol} 历史日线不足: {len(ohlcv) if ohlcv else 0} 根")
            print(f"[backtest] {symbol}: {len(ohlcv)} 根日线（公开行情）", file=sys.stderr)
            rows.extend(_ohlcv_to_rows(svc, symbol, ohlcv, days))
    payload: Dict[str, Any] = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "generated_at": utc_now().isoformat(),
        "exchange": "csv" if ohlcv_file else config.exchange.id,
        "timeframe": "1d",
        "fwd_days": days,
        "min_score": min_score,
        "samples": len(rows),
        "per_method": _backtest_stats(rows, config.strategy.weights, days, min_score),
        "disclaimer": "统计相关性仅供参考，不构成预测保证（README 免责声明）",
    }
    text = _backtest_markdown(payload) if fmt == "markdown" else json.dumps(payload, ensure_ascii=False, indent=2)
    return _emit(text, output)


def _ohlcv_to_rows(svc: PaipanService, symbol: str, ohlcv: Sequence[Sequence[float]], days: int) -> List[Dict[str, Any]]:
    """把 [[ts_ms, o, h, l, c, v], ...] 逐根排盘并计算未来收益。"""
    rows: List[Dict[str, Any]] = []
    closes = [float(c[4]) for c in ohlcv]
    for i in range(len(ohlcv) - days):
        ts = int(ohlcv[i][0])
        now_dt = datetime.fromtimestamp(ts / 1000, timezone.utc)
        result = svc.paipan(symbol, now_dt=now_dt)
        sig = paipan_signals(result.to_dict())
        rows.append({
            "symbol": symbol,
            "date": datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d"),
            "signals": sig,
            "fwd_return": round(closes[i + days] / closes[i] - 1.0, 6),
        })
    return rows


def _load_ohlcv_file(path: str) -> Dict[str, List[List[float]]]:
    """读取回测 CSV（列: symbol,date,open,high,low,close,volume 或 时间戳/OHLCV）。

    时间列支持 ISO 字符串或毫秒时间戳；按 symbol 分组为 [[ts,o,h,l,c,v], ...]。
    """
    csv_path = Path(path).expanduser().resolve()
    if not csv_path.exists():
        raise ConfigError(f"回测 CSV 不存在: {csv_path}")
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"读取 CSV 失败: {exc}") from exc
    if len(df.columns) < 6:
        raise ConfigError("CSV 至少需要 6 列: symbol,date,open,high,low,close[,volume]")
    df.columns = [str(c).strip().lower() for c in df.columns]
    mapping: Dict[str, List[List[float]]] = {}
    for name, group in df.groupby(df.columns[0], sort=False):
        g = group.sort_values(df.columns[1])
        ts_values = g.iloc[:, 1]
        first = str(ts_values.iloc[0])
        if first.replace("-", "").replace(".", "").isdigit() and len(first) >= 10:
            times = ts_values.astype("int64").tolist()  # 毫秒时间戳
        else:
            times = [int(pd.Timestamp(v).timestamp() * 1000) for v in ts_values]
        rows = []
        for idx, ts in enumerate(times):
            o, high, low, close = (float(g.iloc[idx, j]) for j in (2, 3, 4, 5))
            v = float(g.iloc[idx, 6]) if len(g.columns) > 6 else 0.0
            rows.append([ts, o, high, low, close, v])
        mapping[name] = rows
    return mapping


def _backtest_stats(
    rows: Sequence[Dict[str, Any]],
    weights: Mapping[str, float],
    days: int,
    min_score: float,
) -> Dict[str, Any]:
    n = len(rows)
    total_w = sum(weights.values()) or 1.0

    def agg(sig: Mapping[str, float]) -> float:
        return sum(sig.get(m, 0.5) * weights.get(m, 0.0) for m in STRATEGY_NAMES) / total_w

    def stats_for(xs: Sequence[float]) -> Dict[str, Any]:
        ys = [r["fwd_return"] for r in rows]
        pearson: Optional[float] = None
        if n >= 3:
            corr = float(pd.Series(xs).corr(pd.Series(ys)))
            if corr == corr:  # NaN 判定
                pearson = round(corr, 4)
        bull = [y for x, y in zip(xs, ys) if x >= min_score]
        bear = [y for x, y in zip(xs, ys) if x <= 1.0 - min_score]
        return {
            "n": n,
            "pearson": pearson,
            "bull_n": len(bull),
            "bull_hit_rate": round(sum(1 for y in bull if y > 0) / len(bull), 4) if bull else None,
            "bull_mean_return": round(sum(bull) / len(bull), 6) if bull else None,
            "bear_n": len(bear),
            "bear_hit_rate": round(sum(1 for y in bear if y < 0) / len(bear), 4) if bear else None,
            "bear_mean_return": round(sum(bear) / len(bear), 6) if bear else None,
        }

    stats: Dict[str, Any] = {m: stats_for([r["signals"][m] for r in rows]) for m in STRATEGY_NAMES}
    stats["综合"] = stats_for([agg(r["signals"]) for r in rows])
    return stats


def _backtest_markdown(payload: Dict[str, Any]) -> str:
    lines = [
        f"# {APP_NAME} 古法信号回测",
        f"- 版本: {payload['version']}  生成: {payload['generated_at']}",
        f"- 交易所: {payload['exchange']}  周期: {payload['timeframe']}  样本: {payload['samples']}",
        f"- 未来 {payload['fwd_days']} 日收益；多头阈值 ≥ {payload['min_score']}，空头阈值 ≤ {1.0 - payload['min_score']}",
        "",
        "| 古法 | 样本 | Pearson | 多头n | 多头命中 | 多头均值 | 空头n | 空头命中 | 空头均值 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for method, st in payload["per_method"].items():
        def pct(v):
            return f"{v * 100:.1f}%" if v is not None else "-"

        def pct2(v):
            return f"{v * 100:.2f}%" if v is not None else "-"

        lines.append(
            f"| {method} | {st['n']} | {st['pearson'] if st['pearson'] is not None else '-'} "
            f"| {st['bull_n']} | {pct(st['bull_hit_rate'])} | {pct2(st['bull_mean_return'])} "
            f"| {st['bear_n']} | {pct(st['bear_hit_rate'])} | {pct2(st['bear_mean_return'])} |"
        )
    lines.append("")
    lines.append(f"> {payload['disclaimer']}")
    return "\n".join(lines)


def _emit(text: str, output: Optional[str]) -> int:
    if output:
        path = Path(output).expanduser().resolve()
        path.write_text(text, encoding="utf-8")
        print(f"已写入: {path}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--config", default="config.json", help="JSON 配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="打印版本号")
    sub.add_parser("init-config", help="生成安全的默认配置，不覆盖已有文件")
    sub.add_parser("setup", help="首次配置或更新交易所/AI凭据与中转站设置")
    model = sub.add_parser("model", help="查看、查询或切换 AI 模型")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("show", help="显示当前模型和中转站地址")
    model_sub.add_parser("list", help="从中转站查询可用模型")
    model_set = model_sub.add_parser("set", help="切换模型；省略 ID 时交互选择")
    model_set.add_argument("model_id", nargs="?", help="中转站支持的模型 ID")
    sub.add_parser("ai-check", help="仅测试 AI 中转站与严格 JSON Schema；不连接交易所、不下单")
    report = sub.add_parser("paipan-report", help="离线生成完整排盘报告：盘面+信号+断卦要点（不下单）")
    report.add_argument("--symbols", default="", help="逗号分隔标的；默认用 runtime.symbols")
    report.add_argument("--now", default="", help="ISO 时间（默认当前 UTC 时间）")
    report.add_argument("--format", choices=["json", "markdown"], default="json")
    report.add_argument("--output", default="", help="输出文件路径（默认打印 stdout）")
    backtest = sub.add_parser("backtest-paipan", help="历史回测：逐日排盘信号 vs 未来收益（公开行情，不下单）")
    backtest.add_argument("--symbols", default="", help="逗号分隔标的；默认用 runtime.symbols")
    backtest.add_argument("--bars", type=int, default=240, help="历史日线根数（最多 300）")
    backtest.add_argument("--days", type=int, default=1, help="未来 N 日收益")
    backtest.add_argument("--min-score", type=float, default=0.6, help="多头阈值（空头=1-该值）")
    backtest.add_argument("--ohlcv-file", default="", help="本地回测 CSV（列: symbol,date,open,high,low,close[,volume]；离线可用）")
    backtest.add_argument("--format", choices=["json", "markdown"], default="json")
    backtest.add_argument("--output", default="", help="输出文件路径（默认打印 stdout）")
    export = sub.add_parser("export-ohlcv", help="导出公开 K 线为 CSV（列与 backtest --ohlcv-file 兼容；只读公开行情，无需凭据）")
    export.add_argument("--symbols", default="", help="逗号分隔标的；默认用 runtime.symbols")
    export.add_argument("--timeframe", default="", help="K 线周期（默认 runtime.timeframe）")
    export.add_argument("--bars", type=int, default=250, help="每标的 K 线根数")
    export.add_argument("--output", default="", help="输出 CSV 路径（默认 state_dir/ohlcv_*.csv）")
    sub.add_parser("pause", help="暂停新开仓（创建 state_dir/pause 标记；存量仓位仍受管理）")
    sub.add_parser("resume", help="恢复自动交易（移除暂停标记）")
    console = sub.add_parser("console", help="启动 Web 控制台（小白/手机端；一键配置、启动/停止）")
    console.add_argument("--host", default="127.0.0.1", help="监听地址；手机访问用 0.0.0.0")
    console.add_argument("--port", type=int, default=8600, help="监听端口（默认 8600）")
    console.add_argument("--token", default="", help="访问令牌（默认随机生成并打印）")
    sub.add_parser("validate", help="校验配置、交易所市场和公开行情")
    sub.add_parser("once", help="执行一个完整周期")
    sub.add_parser("run", help="持续运行")
    sub.add_parser("status", help="读取本地健康状态")
    sub.add_parser("stats", help="汇总权益曲线与成交审计（只读）")
    adopt = sub.add_parser("adopt-positions", help="显式接管交易所账户已有现货仓位")
    adopt.add_argument(
        "--entry", action="append", default=[], metavar="SYMBOL=PRICE",
        help="每个待接管标的的人工确认成本价，可重复传入",
    )
    sub.add_parser("export-weights", help="导出当前策略权重")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    if args.command == "version":
        print(APP_VERSION)
        return 0

    if args.command == "init-config":
        if config_path.exists():
            raise ConfigError(f"拒绝覆盖已有配置: {config_path}")
        atomic_write_json(config_path, default_config_dict())
        print(f"已生成: {config_path}")
        print("默认使用交易所模拟盘 API。下一步运行 setup，一次填写并安全保存凭据。")
        return 0

    if args.command == "setup":
        run_setup_wizard(config_path)
        return 0

    if args.command == "console":
        from gufa_console import run_console
        return run_console(config_path, host=args.host, port=args.port, token=args.token)

    config = AppConfig.load(config_path)
    credentials = CredentialStore(default_credentials_path(config_path))

    if args.command == "model":
        if args.model_command == "show":
            print(json.dumps({
                "enabled": config.ai.enabled,
                "model": config.ai.model,
                "base_url": config.ai.base_url or "https://api.openai.com/v1",
                "api_key_source": credentials.source(config.ai.api_key_env),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.model_command == "list":
            models = fetch_ai_model_ids(config.ai, credentials)
            print("\n".join(models))
            return 0
        if args.model_command == "set":
            model_id = args.model_id
            if not model_id:
                if not terminal_is_interactive():
                    raise ConfigError("无交互终端时必须使用 model set MODEL_ID")
                model_id = select_model_interactively(
                    fetch_ai_model_ids(config.ai, credentials), config.ai.model
                )
            updated = set_ai_model(config_path, model_id)
            print(f"AI 模型已切换: {updated.ai.model}")
            return 0

    if args.command == "ai-check":
        if not config.ai.enabled:
            raise ConfigError("ai-check 要求 ai.enabled=true；请先运行 setup 启用 AI")
        if credentials.source(config.ai.api_key_env) == "missing":
            raise ConfigError(
                f"缺少 AI 凭据: {config.ai.api_key_env}。请先在交互终端运行 setup"
            )
        advisor = AIAdvisor(config.ai, logging.getLogger(f"{APP_NAME}.ai-check"), credentials)
        print(json.dumps(advisor.schema_check(), ensure_ascii=False, indent=2))
        print("AI_SCHEMA_TEST=PASS")
        return 0

    if args.command in {"validate", "once", "run", "adopt-positions"}:
        missing = missing_credentials(config, credentials)
        if missing:
            if terminal_is_interactive():
                print("检测到首次运行所需凭据尚未保存，自动进入 setup 向导。")
                config, credentials = run_setup_wizard(config_path)
            else:
                names = ", ".join(name for name, _ in missing)
                raise ConfigError(
                    f"缺少凭据: {names}。请先在交互终端运行 "
                    f"python gufa_quant_pro.py --config {config_path} setup"
                )

    logger = setup_logging(config.runtime)
    state_dir = Path(config.runtime.state_dir).expanduser().resolve()

    if args.command == "status":
        health = state_dir / config.runtime.health_file
        if not health.exists():
            print("尚无健康状态文件")
            return 1
        print(json.dumps(load_json(health), ensure_ascii=False, indent=2))
        return 0

    if args.command == "stats":
        return cmd_stats(state_dir, config.runtime.health_file)

    if args.command == "export-weights":
        path = state_dir / f"GuFa_Weights_{utc_now().strftime('%Y%m%d_%H%M%S')}.json"
        atomic_write_json(path, asdict(config.strategy))
        print(path)
        return 0

    if args.command == "paipan-report":
        return cmd_paipan_report(
            config,
            _split_symbols(args.symbols, config.runtime.symbols),
            args.now or None,
            args.format,
            args.output or None,
        )

    if args.command == "backtest-paipan":
        return cmd_backtest_paipan(
            config,
            _split_symbols(args.symbols, config.runtime.symbols),
            args.bars,
            args.days,
            args.min_score,
            args.format,
            args.output or None,
            args.ohlcv_file or None,
        )

    if args.command == "export-ohlcv":
        return cmd_export_ohlcv(
            config,
            _split_symbols(args.symbols, config.runtime.symbols),
            args.timeframe or config.runtime.timeframe,
            args.bars,
            args.output or None,
        )

    if args.command in {"pause", "resume"}:
        return cmd_pause_resume(state_dir, resume=(args.command == "resume"))

    lock_path = state_dir / "gufa_quant.lock"
    with InstanceLock(lock_path):
        app = GuFaQuantPro(config, logger, credentials)
        if args.command == "validate":
            daily_selection = app._daily_selection()
            if not daily_selection.complete:
                raise SafetyError(
                    "每日古法初选行情校验失败: "
                    + json.dumps(daily_selection.errors, ensure_ascii=False, sort_keys=True)
                )
            prices, results = app._prices_and_signals()
            report = {
                "valid": True,
                "version": APP_VERSION,
                "exchange": config.exchange.id,
                "mode": "exchange-sandbox" if config.exchange.sandbox else "exchange-production",
                "sandbox": config.exchange.sandbox,
                "daily_selection": daily_selection.to_dict(),
                "prices": prices,
                "scores": {s: r.score for s, r in results.items()},
                "orders_possible": False,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "once":
            print(json.dumps(app.run_cycle(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "adopt-positions":
            entries = parse_entry_pairs(args.entry)
            prices = {s: app.gateway.fetch_last_price(s) for s in config.runtime.symbols}
            adopted = app.gateway.adopt_positions(prices, entries)
            print(json.dumps({"adopted": adopted}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            app.run_forever()
            return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, SafetyError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"UNHANDLED: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1)
