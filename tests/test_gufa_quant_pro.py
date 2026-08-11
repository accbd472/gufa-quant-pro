import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gufa_quant_pro as g  # noqa: E402


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_default(tmp_path: Path, mutate=None) -> g.AppConfig:
    payload = g.default_config_dict()
    payload["runtime"]["state_dir"] = str(tmp_path / "runtime")
    if mutate:
        mutate(payload)
    return g.AppConfig.load(write_json(tmp_path / "config.json", payload))


def test_default_config_round_trip_and_profile_id(tmp_path: Path) -> None:
    cfg = load_default(tmp_path)
    assert cfg.exchange.sandbox is True
    assert cfg.exchange.market_type == "spot"
    assert cfg.selection.enabled is True
    assert cfg.selection.timeframe == "1d"
    assert g.default_config_dict()["version"] == g.CONFIG_VERSION
    assert g.default_config_dict()["selection"]["top_n"] == 3
    assert "dry_run" not in g.default_config_dict()["risk"]
    assert not any(key.startswith("paper_") for key in g.default_config_dict()["risk"])
    assert len(g.build_profile_id(cfg)) == 64


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["risk"].__setitem__("reject_unmanaged_positions", "false"), "risk.reject_unmanaged_positions 必须是 JSON boolean"),
        (lambda p: p["exchange"].__setitem__("timeout_ms", "15000"), "exchange.timeout_ms 必须是 JSON integer"),
        (lambda p: p["runtime"].__setitem__("symbols", "BTC/USDT"), "runtime.symbols 必须是 JSON array"),
        (lambda p: p["selection"].__setitem__("top_n", "3"), "selection.top_n 必须是 JSON integer"),
        (lambda p: p["selection"].__setitem__("enabled", 1), "selection.enabled 必须是 JSON boolean"),
        (lambda p: p["strategy"].__setitem__("entry_half", "0.64"), "strategy.entry_half 必须是有限 JSON number"),
        (lambda p: p.__setitem__("version", True), "version 必须是 JSON integer"),
        (lambda p: p.__setitem__("exchange", []), "exchange 必须是 JSON object"),
    ],
)
def test_strict_config_types(tmp_path: Path, mutate, message: str) -> None:
    with pytest.raises(g.ConfigError, match=message):
        load_default(tmp_path, mutate)


def test_unknown_config_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(g.ConfigError, match="未知字段"):
        load_default(tmp_path, lambda p: p["runtime"].__setitem__("typo", 1))


def test_exchange_proxy_url_validation(tmp_path: Path) -> None:
    cfg = load_default(tmp_path)
    assert cfg.exchange.proxy_url == ""
    with pytest.raises(g.ConfigError, match="http:// 或 https://"):
        load_default(tmp_path, lambda p: p["exchange"].__setitem__("proxy_url", "socks5://127.0.0.1:1080"))
    cfg2 = load_default(
        tmp_path, lambda p: p["exchange"].__setitem__("proxy_url", "http://127.0.0.1:7890")
    )
    assert cfg2.exchange.proxy_url == "http://127.0.0.1:7890"


def test_exchange_proxy_url_wired_to_ccxt(tmp_path: Path, monkeypatch) -> None:
    payload = g.default_config_dict()
    payload["exchange"]["proxy_url"] = "http://127.0.0.1:7890"
    cfg = g.AppConfig.load(write_json(tmp_path / "config.json", payload))
    state = g.StateStore(tmp_path / "state.json", "profile")
    captured: dict = {}

    class FakeClient:
        timeframes = {"1h": "1H", "1d": "1D"}

        def __init__(self, params: dict) -> None:
            captured["params"] = params

        def set_sandbox_mode(self, enabled: bool) -> None:
            captured["sandbox"] = enabled

        def load_markets(self) -> dict:
            return {
                "BTC/USDT": {
                    "id": "BTC-USDT",
                    "type": "spot",
                    "spot": True,
                    "active": True,
                    "quote": "USDT",
                }
            }

    monkeypatch.setattr(g, "ccxt", SimpleNamespace(okx=FakeClient))
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setenv("GUFA_CREDENTIALS_FILE", str(credentials_path))
    store = g.CredentialStore(credentials_path)
    store.set(cfg.exchange.api_key_env, "k")
    store.set(cfg.exchange.secret_env, "s")
    store.set(cfg.exchange.password_env, "p")
    store.save()

    g.ExchangeGateway(cfg, state, logging.getLogger("test.proxy"), store)
    assert captured["params"]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert captured["sandbox"] is True


def test_exchange_proxy_list_parsing_and_merge(tmp_path: Path) -> None:
    """proxy_list 支持逗号分隔/数组，去重保序；effective_proxies 合并 proxy_url 优先。"""
    cfg = load_default(
        tmp_path,
        lambda p: p["exchange"].__setitem__(
            "proxy_list", "http://127.0.0.1:7891, http://127.0.0.1:7892,http://127.0.0.1:7891"
        ),
    )
    assert cfg.exchange.proxy_list == (
        "http://127.0.0.1:7891", "http://127.0.0.1:7892",
    )
    # 数组形式
    cfg2 = load_default(
        tmp_path,
        lambda p: p["exchange"].__setitem__(
            "proxy_list", ["http://127.0.0.1:7891", "http://127.0.0.1:7892"]
        ),
    )
    assert cfg2.exchange.proxy_list == ("http://127.0.0.1:7891", "http://127.0.0.1:7892")
    # 非法格式报错
    with pytest.raises(g.ConfigError, match="proxy_list"):
        load_default(
            tmp_path,
            lambda p: p["exchange"].__setitem__("proxy_list", "socks5://127.0.0.1:1080"),
        )
    # effective_proxies：proxy_url 优先 + proxy_list 去重追加
    cfg3 = load_default(
        tmp_path,
        lambda p: (
            p["exchange"].__setitem__("proxy_url", "http://127.0.0.1:7890"),
            p["exchange"].__setitem__("proxy_list", ["http://127.0.0.1:7890", "http://127.0.0.1:7891"]),
        ),
    )
    assert cfg3.exchange.effective_proxies() == (
        "http://127.0.0.1:7890", "http://127.0.0.1:7891",
    )


def test_gateway_proxy_auto_switch_on_network_error(monkeypatch) -> None:
    """多代理时：当前代理网络错误重试耗尽后自动切换下一个可用代理，并继续成功。"""
    import ccxt as ccxt_mod

    gw = object.__new__(g.ExchangeGateway)
    gw.exchange_cfg = SimpleNamespace(
        id="okx", timeout_ms=15000, max_retries=1, retry_base_seconds=0.01,
        sandbox=True, api_key_env="K", secret_env="S", password_env="P",
    )
    gw.risk = None
    gw.runtime = SimpleNamespace(symbols=["BTC/USDT"])
    gw.state_store = None
    gw.log = logging.getLogger("test.proxy.switch")
    gw._proxies = ("http://127.0.0.1:1", "http://127.0.0.1:2")
    gw._proxy_index = 0
    gw._proxy_cooldown = {}
    gw._proxy_switches = 0
    gw.markets = {}
    gw.credentials = None

    class FakeClient:
        def __init__(self, proxy: str) -> None:
            self.proxy = proxy

        def load_markets(self) -> dict:
            if self.proxy == "http://127.0.0.1:1":
                raise ccxt_mod.NetworkError("bad proxy")
            return {"BTC/USDT": {"spot": True, "active": True, "quote": "USDT"}}

    gw._build_client = lambda proxy: FakeClient(proxy)  # type: ignore[method-assign]
    gw._validate_markets = lambda: None  # type: ignore[method-assign]
    gw.client = gw._build_client("http://127.0.0.1:1")

    result = gw._safe_call("probe", lambda: gw.client.load_markets())
    assert result["BTC/USDT"]["spot"] is True
    assert gw._proxy_switches == 1
    assert gw._current_proxy() == "http://127.0.0.1:2"
    # 坏代理已进冷却
    assert gw._proxy_cooldown.get("http://127.0.0.1:1", 0.0) > 0.0


def test_gateway_proxy_all_fail_raises(monkeypatch) -> None:
    """所有代理都失败时抛最后一个网络错误，不做无意义轮换。"""
    import ccxt as ccxt_mod

    gw = object.__new__(g.ExchangeGateway)
    gw.exchange_cfg = SimpleNamespace(
        id="okx", timeout_ms=15000, max_retries=1, retry_base_seconds=0.01,
        sandbox=True, api_key_env="K", secret_env="S", password_env="P",
    )
    gw.risk = None
    gw.runtime = SimpleNamespace(symbols=["BTC/USDT"])
    gw.state_store = None
    gw.log = logging.getLogger("test.proxy.allfail")
    gw._proxies = ("http://127.0.0.1:1", "http://127.0.0.1:2")
    gw._proxy_index = 0
    gw._proxy_cooldown = {}
    gw._proxy_switches = 0
    gw.markets = {}
    gw.credentials = None

    class FakeClient:
        def load_markets(self) -> dict:
            raise ccxt_mod.NetworkError("all down")

    gw._build_client = lambda proxy: FakeClient()  # type: ignore[method-assign]
    gw._validate_markets = lambda: None  # type: ignore[method-assign]
    gw.client = FakeClient()

    with pytest.raises(ccxt_mod.NetworkError):
        gw._safe_call("probe", lambda: gw.client.load_markets())
    # 两个代理都试过并冷却
    assert set(gw._proxy_cooldown.keys()) == {"http://127.0.0.1:1", "http://127.0.0.1:2"}


def test_config_json_tolerates_utf8_bom(tmp_path: Path) -> None:
    payload = g.default_config_dict()
    payload["runtime"]["state_dir"] = str(tmp_path / "runtime")
    path = tmp_path / "config-bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    cfg = g.AppConfig.load(path)
    assert cfg.runtime.state_dir.endswith("runtime")


def test_old_config_without_selection_uses_safe_defaults(tmp_path: Path) -> None:
    payload = g.default_config_dict()
    payload.pop("selection")
    payload["runtime"]["state_dir"] = str(tmp_path / "runtime")
    cfg = g.AppConfig.load(write_json(tmp_path / "config.json", payload))
    assert cfg.selection.enabled is True
    assert cfg.selection.timeframe == "1d"
    assert cfg.selection.top_n == 3
    assert cfg.selection.min_score == 0.45


def test_selection_config_bounds_are_validated(tmp_path: Path) -> None:
    with pytest.raises(g.ConfigError, match="selection.ohlcv_limit 至少为 120"):
        load_default(tmp_path, lambda p: p["selection"].__setitem__("ohlcv_limit", 99))
    with pytest.raises(g.ConfigError, match="selection.top_n 至少为 1"):
        load_default(tmp_path, lambda p: p["selection"].__setitem__("top_n", 0))
    with pytest.raises(g.ConfigError, match="selection.min_score 必须在 0..1"):
        load_default(tmp_path, lambda p: p["selection"].__setitem__("min_score", 1.1))


def test_production_ack_is_required(tmp_path: Path) -> None:
    with pytest.raises(g.ConfigError, match="正式盘被拒绝"):
        load_default(tmp_path, lambda p: p["exchange"].__setitem__("sandbox", False))


def test_state_version_and_profile_are_bound(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = g.StateStore(state_path, "profile-a")
    assert store.state.version == g.STATE_VERSION
    assert store.state.profile_id == "profile-a"

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    # v4 -> v5 合法自动迁移（新增 triggers/trigger_skip_until 字段）
    payload["version"] = 4
    write_json(state_path, payload)
    migrated = g.StateStore(state_path, "profile-a")
    assert migrated.state.version == g.STATE_VERSION
    assert migrated.state.triggers == {}
    assert migrated.state.trigger_skip_until == {}

    # 低于 v4 的旧版本仍拒绝（禁止跳过版本迁移）
    payload["version"] = 3
    write_json(state_path, payload)
    with pytest.raises(g.SafetyError, match="状态文件版本不兼容"):
        g.StateStore(state_path, "profile-a")

    payload["version"] = g.STATE_VERSION
    payload["profile_id"] = "profile-b"
    write_json(state_path, payload)
    with pytest.raises(g.SafetyError, match="其他运行配置"):
        g.StateStore(state_path, "profile-a")


def make_gateway_stub(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = g.StateStore(state_path, "profile")
    gateway = object.__new__(g.ExchangeGateway)
    gateway.state_store = store
    gateway.risk = SimpleNamespace()
    gateway.log = logging.getLogger("test.gateway")
    gateway.client = SimpleNamespace()
    gateway.exchange_cfg = SimpleNamespace(
        id="fake", client_order_id_param="clientOrderId", max_retries=0
    )
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    return gateway, store


def test_pending_without_order_id_halts_reconciliation(tmp_path: Path) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    store.state.pending_orders["BTC/USDT"] = {
        "id": "",
        "client_id": "gufatest",
        "created_at": g.iso_now(),
    }
    store.save()

    with pytest.raises(g.OrderUncertainError, match="无法证明订单未成交"):
        gateway.reconcile_pending_orders()
    assert store.state.halted_reason.startswith("ORDER_UNCERTAIN:")
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted["pending_orders"]["BTC/USDT"]["id"] == ""


def test_account_snapshot_skips_zombie_symbol_without_balance(tmp_path: Path) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(dust_quote=1.0, reject_unmanaged_positions=False)
    gateway.runtime = SimpleNamespace(quote_currency="USDT", symbols=["BTC/USDT", "CC/USDT"])
    gateway.client = SimpleNamespace(
        fetch_balance=lambda: {
            "free": {"USDT": 100.0, "BTC": 0.0},
            "total": {"USDT": 100.0, "BTC": 0.0},
        }
    )
    snapshot = gateway.account_snapshot(prices={"BTC/USDT": 50000.0})
    assert snapshot.equity == 100.0
    assert "BTC/USDT" in snapshot.positions
    assert "CC/USDT" not in snapshot.positions


def test_account_snapshot_requires_price_when_balance_exists(tmp_path: Path) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(dust_quote=1.0, reject_unmanaged_positions=False)
    gateway.runtime = SimpleNamespace(quote_currency="USDT", symbols=["CC/USDT"])
    gateway.client = SimpleNamespace(
        fetch_balance=lambda: {
            "free": {"USDT": 100.0, "CC": 5.0},
            "total": {"USDT": 100.0, "CC": 5.0},
        }
    )
    with pytest.raises(g.SafetyError, match="无有效价格"):
        gateway.account_snapshot(prices={})


def test_create_order_network_failure_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(max_slippage_bps=30.0)
    gateway.has_open_order = lambda symbol, market="spot": False
    gateway.estimate_vwap = lambda symbol, side, amount, price: (price, 0.0)

    calls = {"count": 0}

    class FakeNetworkError(Exception):
        pass

    def create_order(*args, **kwargs):
        calls["count"] += 1
        raise FakeNetworkError("timeout")

    gateway.client = SimpleNamespace(create_order=create_order)
    monkeypatch.setattr(g.ExchangeGateway, "_network_error_types", staticmethod(lambda: (FakeNetworkError,)))
    plan = g.OrderPlan("BTC/USDT", "buy", 0.01, 100.0, 1.0, 0.1, 0.0, "test")

    with pytest.raises(g.OrderUncertainError, match="禁止自动重试"):
        gateway.execute(plan)
    assert calls["count"] == 1
    pending = store.state.pending_orders["BTC/USDT"]
    assert pending["stage"] == "submitting"
    assert pending["uncertain"] is True
    assert store.state.halted_reason.startswith("ORDER_UNCERTAIN:")


def test_market_data_validator_accepts_sane_data_and_rejects_bad_ohlc() -> None:
    rows = 120
    close = [100.0 + i * 0.1 for i in range(rows)]
    frame = pd.DataFrame({
        "timestamp": [i * 60_000 for i in range(rows)],
        "open": close,
        "high": [x + 1.0 for x in close],
        "low": [x - 1.0 for x in close],
        "close": close,
        "volume": [10.0] * rows,
    })
    validated = g.MarketDataValidator.validate(frame)
    assert len(validated) == rows

    broken = frame.copy()
    broken.loc[5, "high"] = broken.loc[5, "low"] - 1
    with pytest.raises(g.SafetyError, match="OHLC"):
        g.MarketDataValidator.validate(broken)


def test_strategy_engine_output_is_bounded() -> None:
    rows = 250
    close = pd.Series([100.0 + i * 0.15 + (i % 7) * 0.02 for i in range(rows)])
    frame = pd.DataFrame({
        "timestamp": [i * 3_600_000 for i in range(rows)],
        "open": close - 0.1,
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": [1000.0 + i for i in range(rows)],
    })
    result = g.StrategyEngine(g.StrategyConfig()).calculate(frame)
    assert 0.0 <= result.score <= 1.0
    assert set(result.signals) == set(g.STRATEGY_NAMES)
    assert all(0.0 <= value <= 1.0 for value in result.signals.values())


def test_wait_for_order_uses_real_order_not_found_class(tmp_path: Path, monkeypatch) -> None:
    gateway, _ = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(order_fill_timeout_seconds=1)

    class FakeOrderNotFound(Exception):
        pass

    monkeypatch.setattr(g.ccxt, "OrderNotFound", FakeOrderNotFound)
    gateway._safe_call = lambda name, function: (_ for _ in ()).throw(FakeOrderNotFound())
    order = gateway._wait_for_order("BTC/USDT", "123", {"status": "open"})
    assert order["status"] == "open"


def test_reconcile_non_network_error_halts(tmp_path: Path, monkeypatch) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    store.state.pending_orders["BTC/USDT"] = {
        "id": "123",
        "created_at": g.iso_now(),
    }
    store.save()

    class FakeOrderNotFound(Exception):
        pass

    monkeypatch.setattr(g.ccxt, "OrderNotFound", FakeOrderNotFound)
    monkeypatch.setattr(g.ExchangeGateway, "_network_error_types", staticmethod(lambda: (TimeoutError,)))
    gateway._safe_call = lambda name, function: (_ for _ in ()).throw(ValueError("bad response"))

    with pytest.raises(g.OrderUncertainError, match="非网络异常"):
        gateway.reconcile_pending_orders()
    assert store.state.halted_reason.startswith("ORDER_UNCERTAIN:")


def make_signal_result(score: float = 0.7) -> g.SignalResult:
    signals = {name: 0.7 for name in g.STRATEGY_NAMES}
    return g.SignalResult(
        score=score,
        signals=signals,
        diagnostics={"price": 100.0, "atr": 2.0},
        candle_time="2026-01-01T00:00:00+00:00",
    )


def make_selection_controller(tmp_path: Path, scores, fail_symbol=None, dead_symbols=()):
    controller = object.__new__(g.GuFaQuantPro)
    controller.config = SimpleNamespace(
        runtime=SimpleNamespace(symbols=list(scores)),
        selection=g.DailySelectionConfig(
            enabled=True,
            timeframe="1d",
            ohlcv_limit=180,
            top_n=2,
            min_score=0.55,
        ),
        strategy=g.StrategyConfig(),
    )
    controller.store = g.StateStore(tmp_path / "selection-state.json", "selection-profile")
    controller.log = logging.getLogger("test.selection")
    calls = []

    def fetch_ohlcv(symbol, timeframe=None, ohlcv_limit=None):
        calls.append((symbol, timeframe, ohlcv_limit))
        if symbol == fail_symbol:
            raise g.SafetyError("daily data unavailable")
        if symbol in dead_symbols:
            raise g.SafetyError(f"{symbol} 无有效 K 线，行情为空")
        return symbol

    controller.gateway = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    controller.engine = SimpleNamespace(
        calculate=lambda frame, symbol=None: make_signal_result(scores[symbol])
    )
    return controller, calls


def test_daily_selection_dead_symbols_excluded_not_fail_closed(tmp_path: Path) -> None:
    """死币（无行情数据）只被当日剔除，不触发 fail-closed；其余标的正常入选。"""
    scores = {
        "BTC/USDT": 0.61,
        "ETH/USDT": 0.82,
        "CC/USDT": 0.99,   # 死币：即使古法分数最高也不入选，且不拖垮当日初选
    }
    controller, calls = make_selection_controller(
        tmp_path, scores, dead_symbols=("CC/USDT",)
    )

    first = controller._daily_selection()
    assert first.complete is True          # 死币不 fail-closed
    assert first.cached is False
    assert "CC/USDT" in first.dead         # 死币被记录
    assert first.selected_symbols == ["ETH/USDT", "BTC/USDT"]
    assert "CC/USDT" not in first.candidates
    assert "无有效" in controller.store.state.daily_selection_dead.get("CC/USDT", "")

    # 当日缓存命中时死币状态保持一致，且不重复拉 K 线
    second = controller._daily_selection()
    assert second.cached is True
    assert second.dead == first.dead
    assert len(calls) == 3


def test_daily_selection_bad_data_excluded_not_fail_closed(tmp_path: Path) -> None:
    """行情数据异常（如单根 K 线涨跌超 50%）视为死币当日剔除，不 fail-closed 全盘。"""
    scores = {
        "BTC/USDT": 0.61,
        "ETH/USDT": 0.82,
        "ACE/USDT": 0.99,  # 数据异常：即使古法分数最高也不入选，且不拖垮当日初选
    }
    controller, _ = make_selection_controller(
        tmp_path, scores, dead_symbols=("ACE/USDT",)
    )
    # 把死币报错模拟成真实场景的「涨跌超过 50%」
    controller.gateway.fetch_ohlcv = lambda symbol, timeframe=None, ohlcv_limit=None: (
        (_ for _ in ()).throw(
            g.SafetyError("ACE/USDT 最新闭合 K 线涨跌超过 50%，疑似数据异常，拒绝交易")
        )
        if symbol == "ACE/USDT"
        else symbol
    )

    result = controller._daily_selection()
    assert result.complete is True               # 数据异常不 fail-closed
    assert "ACE/USDT" in result.dead
    assert "疑似数据异常" in result.dead["ACE/USDT"]
    assert result.selected_symbols == ["ETH/USDT", "BTC/USDT"]
    assert "ACE/USDT" not in result.candidates


def test_daily_selection_dead_symbols_reprobe_next_day(tmp_path: Path) -> None:
    """次日自动重新探测死币：即使仍无行情，也会重新探测而非永久缓存。"""
    scores = {"BTC/USDT": 0.61, "CC/USDT": 0.99}
    controller, calls = make_selection_controller(tmp_path, scores, dead_symbols=("CC/USDT",))

    first = controller._daily_selection()
    assert "CC/USDT" in first.dead
    first_calls = len(calls)

    # 模拟次日：死币被重新探测（calls 增加），而非从缓存拿
    controller.store.state.daily_selection_date = "2000-01-01"
    controller.store.state.daily_selection_dead = {}
    controller.store.save()
    second = controller._daily_selection()
    assert len(calls) > first_calls      # 重新探测了
    assert "CC/USDT" in second.dead      # 仍死，但已重新探测


def test_account_prices_skips_dead_symbols_without_position(tmp_path: Path) -> None:
    """账户估值跳过当日死币（无持仓），避免每周期重复拉价刷屏。"""
    controller, _ = make_selection_controller(tmp_path, {"BTC/USDT": 0.61, "CC/USDT": 0.0})
    controller.store.state.daily_selection_dead = {"CC/USDT": "无有效价格"}
    fetched = []

    def fetch_last_price(symbol):
        fetched.append(symbol)
        if symbol == "CC/USDT":
            raise g.SafetyError("CC/USDT ticker 无有效价格")
        return 100.0

    controller.gateway.fetch_last_price = fetch_last_price
    prices = controller._account_prices()
    assert "CC/USDT" not in fetched
    assert prices == {"BTC/USDT": 100.0}

    # 有持仓的死币仍硬校验：估值失败必须保守停止
    controller.store.state.positions["CC/USDT"] = g.PositionState(
        amount=1.0, avg_entry=10.0
    )
    with pytest.raises(g.SafetyError, match="无有效价格"):
        controller._account_prices()


def test_daily_selection_ranks_thresholds_and_caches(tmp_path: Path) -> None:
    scores = {
        "BTC/USDT": 0.61,
        "ETH/USDT": 0.82,
        "SOL/USDT": 0.73,
        "DOGE/USDT": 0.54,
    }
    controller, calls = make_selection_controller(tmp_path, scores)

    first = controller._daily_selection()
    assert first.complete is True
    assert first.cached is False
    assert first.selected_symbols == ["ETH/USDT", "SOL/USDT"]
    assert len(calls) == 4
    assert all(timeframe == "1d" and limit == 180 for _, timeframe, limit in calls)

    second = controller._daily_selection()
    assert second.complete is True
    assert second.cached is True
    assert second.selected_symbols == first.selected_symbols
    assert len(calls) == 4


def test_daily_selection_failure_is_fail_closed_and_not_cached(tmp_path: Path) -> None:
    scores = {"BTC/USDT": 0.8, "ETH/USDT": 0.7}
    controller, calls = make_selection_controller(tmp_path, scores, fail_symbol="ETH/USDT")

    result = controller._daily_selection()
    assert result.complete is False
    assert result.selected_symbols == []
    assert "ETH/USDT" in result.errors
    assert controller.store.state.daily_selection_date == ""

    controller._daily_selection()
    assert len(calls) == 4


def make_prefilter_controller(prefilter=True, preferred=(), exclude=()):
    controller = object.__new__(g.GuFaQuantPro)
    controller.config = SimpleNamespace(
        runtime=SimpleNamespace(symbols=[]),
        selection=g.DailySelectionConfig(
            enabled=True,
            timeframe="1d",
            ohlcv_limit=180,
            top_n=2,
            min_score=0.55,
            prefilter=prefilter,
            preferred=tuple(preferred),
            exclude_patterns=tuple(exclude),
        ),
        strategy=g.StrategyConfig(),
    )
    controller.store = g.StateStore(Path("unused-state.json"), "prefilter-profile")
    controller.log = logging.getLogger("test.prefilter")
    return controller


def test_name_prefilter_keeps_preferred_and_drops_numeric() -> None:
    controller = make_prefilter_controller()
    symbols = ["BTC/USDT", "ETH/USDT", "2Z/USDT", "1INCH/USDT", "BABYDOGE/USDT"]
    out = controller._prefilter_symbols(symbols)
    assert "BTC/USDT" in out
    assert "ETH/USDT" in out
    assert "2Z/USDT" not in out
    assert "1INCH/USDT" not in out  # 数字开头且不在 preferred
    assert "BABYDOGE/USDT" in out   # 非数字开头，保留进古法扫描层


def test_name_prefilter_exclude_patterns_and_preferred_override() -> None:
    controller = make_prefilter_controller(
        preferred=["BTC"], exclude=["^BABY", "DOGE$"]
    )
    symbols = ["BTC/USDT", "BABYDOGE/USDT", "DOGE/USDT", "SHIB/USDT"]
    out = controller._prefilter_symbols(symbols)
    assert out == ["BTC/USDT", "SHIB/USDT"]


def test_name_prefilter_ignores_liquidity_keeps_all_valid_names() -> None:
    # 不使用流动性：即使成交额很低/非主流，只要名字合规就全部进入古法扫描
    controller = make_prefilter_controller()
    symbols = ["BTC/USDT", "A/USDT", "CAT/USDT", "BONK/USDT", "PUMP/USDT"]
    out = controller._prefilter_symbols(symbols)
    assert out == symbols  # 无成交额排序、无截断、无剔除


def test_name_prefilter_disabled_returns_all() -> None:
    controller = make_prefilter_controller(prefilter=False)
    symbols = ["BTC/USDT", "2Z/USDT"]
    assert controller._prefilter_symbols(symbols) == symbols


def test_daily_selection_uses_prefiltered_candidates_and_caches(tmp_path: Path) -> None:
    scores = {
        "BTC/USDT": 0.61,
        "ETH/USDT": 0.82,
        "2Z/USDT": 0.99,   # 名字初筛直接排除，即使古法得分最高也不入选
    }
    controller, calls = make_selection_controller(tmp_path, scores)

    first = controller._daily_selection()
    assert first.complete is True
    assert "2Z/USDT" not in first.candidates
    assert first.selected_symbols == ["ETH/USDT", "BTC/USDT"]
    assert controller.store.state.daily_selection_candidates == ["BTC/USDT", "ETH/USDT"]
    assert len(calls) == 2  # 只对初筛后的候选拉 K 线

    second = controller._daily_selection()
    assert second.cached is True
    assert second.candidates == first.candidates


def test_fine_screen_rejects_symbols_outside_manual_pool() -> None:
    controller = object.__new__(g.GuFaQuantPro)
    controller.config = SimpleNamespace(runtime=SimpleNamespace(symbols=["BTC/USDT"]))
    with pytest.raises(g.SafetyError, match="候选池外"):
        controller._prices_and_signals(["AAPL"])


def test_unselected_position_skips_remote_ai_and_cannot_increase() -> None:
    controller = object.__new__(g.GuFaQuantPro)
    ai_config = g.AIConfig(enabled=False)
    controller.config = SimpleNamespace(
        runtime=SimpleNamespace(symbols=["ETH/USDT"]),
        selection=g.DailySelectionConfig(),
        strategy=g.StrategyConfig(),
        risk=SimpleNamespace(max_symbol_allocation=0.2, max_total_allocation=0.7),
        ai=ai_config,
    )
    controller.engine = g.StrategyEngine(controller.config.strategy)
    controller.ai = g.AIAdvisor(ai_config, logging.getLogger("test.selection.ai"))
    controller.ai.interpret = lambda *args, **kwargs: pytest.fail(
        "未入选持仓不得调用远程 AI 解读路径"
    )
    position = g.AccountPosition("ETH/USDT", 1.0, 100.0, 100.0, 90.0, 110.0)
    snapshot = g.AccountSnapshot(
        equity=1000.0,
        quote_free=900.0,
        quote_total=900.0,
        positions={"ETH/USDT": position},
        timestamp=g.iso_now(),
    )

    decisions = controller._build_decisions(
        snapshot,
        {"ETH/USDT": make_signal_result(0.95)},
        {},
        g.RiskStatus(True, ""),
        selected_symbols=[],
    )
    decision = decisions["ETH/USDT"]
    current_fraction = position.quote_value / snapshot.equity / 0.2
    assert decision.target_fraction <= current_fraction
    assert decision.target_allocation <= position.quote_value / snapshot.equity
    assert decision.ai_decision.enabled is False
    assert "未通过当日古法初选" in decision.ai_decision.summary


def make_ai_payload(action="BUY", target_level="FULL", confidence=0.9):
    return {
        "action": action,
        "target_level": target_level,
        "confidence": confidence,
        "summary": "十项中多数趋势与动量偏多，但仍需遵守仓位上限。",
        "readings": {
            name: {
                "bias": "bullish",
                "confidence": 0.8,
                "reading": f"{name} 指标偏多。",
            }
            for name in g.STRATEGY_NAMES
        },
        "conflicts": ["摆动指标与中期趋势可能不同步"],
        "risk_notes": ["不保证未来收益"],
    }


def test_ai_decision_requires_all_ten_readings() -> None:
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    payload = make_ai_payload()
    payload["readings"].pop("四柱")
    with pytest.raises(g.ConfigError, match="缺少古法项"):
        advisor._parse_decision(json.dumps(payload, ensure_ascii=False))


def test_ai_decision_requires_all_top_level_fields() -> None:
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    payload = make_ai_payload()
    payload.pop("risk_notes")
    with pytest.raises(g.ConfigError, match="缺少顶层字段"):
        advisor._parse_decision(json.dumps(payload, ensure_ascii=False))


def test_ai_decision_strict_parser_accepts_complete_payload() -> None:
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    decision = advisor._parse_decision(json.dumps(make_ai_payload(), ensure_ascii=False))
    assert decision.action == "BUY"
    assert decision.target_level == "FULL"
    assert set(decision.readings) == set(g.STRATEGY_NAMES)
    assert decision.readings["奇门"].bias == "bullish"


def test_ai_invalid_structure_is_repaired_once() -> None:
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True)
    advisor.log = logging.getLogger("test.ai.repair")
    calls = []
    repaired = make_ai_payload(action="HOLD", target_level="UNCHANGED", confidence=0.2)

    def completion(messages):
        calls.append(messages)
        return json.dumps(repaired, ensure_ascii=False)

    advisor._completion_content = completion
    decision, repair_used = advisor._parse_with_one_format_repair(
        json.dumps({"action": {"value": "BUY"}}, ensure_ascii=False)
    )
    assert repair_used is True
    assert len(calls) == 1
    assert decision.action == "HOLD"
    assert decision.target_level == "UNCHANGED"
    repair_request = json.loads(calls[0][1]["content"])
    assert "validation_error" in repair_request
    assert "invalid_response" in repair_request


def test_ai_valid_structure_does_not_trigger_repair() -> None:
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True)
    advisor.log = logging.getLogger("test.ai.no-repair")
    advisor._completion_content = lambda messages: pytest.fail("不应调用格式修复请求")
    decision, repair_used = advisor._parse_with_one_format_repair(
        json.dumps(make_ai_payload(action="HOLD", target_level="UNCHANGED"), ensure_ascii=False)
    )
    assert repair_used is False
    assert decision.action == "HOLD"


def test_ai_empty_content_does_not_trigger_repair() -> None:
    # 余额不足/中转站异常常表现为空正文：必须直接报错，不得再发一次修复请求烧钱
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True)
    advisor.log = logging.getLogger("test.ai.empty")
    advisor._completion_content = lambda messages: pytest.fail("空正文不应触发格式修复请求")
    for bad in ("", "   ", None):
        with pytest.raises(g.ConfigError, match="非空 JSON string"):
            advisor._parse_with_one_format_repair(bad)


def test_ai_relay_error_skips_format_repair() -> None:
    # 修复请求遇到中转站错误（如余额不足）时直接上抛，不再尝试解析
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True)
    advisor.log = logging.getLogger("test.ai.relay")
    calls = []

    def completion(messages):
        calls.append(messages)
        raise g.AIRelayError("AI 中转站错误（HTTP 402）: INSUFFICIENT_BALANCE: 余额不足",
                             status=402, detail="INSUFFICIENT_BALANCE: 余额不足")

    advisor._completion_content = completion
    with pytest.raises(g.AIRelayError, match="余额不足"):
        advisor._parse_with_one_format_repair(
            json.dumps({"action": {"value": "BUY"}}, ensure_ascii=False)
        )
    assert len(calls) == 1


def test_ai_interpret_relay_error_fallback_and_last_error() -> None:
    # 真实交易周期中：中转站 402 余额不足 → 单行日志 + fail_closed 回退 + last_error 可读
    cfg = g.AIConfig(enabled=True, fail_closed=True)
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = cfg
    advisor.log = logging.getLogger("test.ai.relay.interpret")
    advisor.bind_strategy_weights(g.StrategyConfig().weights)

    class RelayDown(Exception):
        status_code = 402
        body = {"error": {"message": "INSUFFICIENT_BALANCE: 余额不足"}}

    advisor.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RelayDown()))
        )
    )
    position = g.AccountPosition("BTC/USDT", 1.0, 100.0, 100.0, 90.0, 110.0)
    decision = advisor.interpret(
        "BTC/USDT", make_signal_result(), 1.0, 0.5, "rule full", position, 1000.0
    )
    assert decision.fallback is True
    assert decision.action == "HOLD"
    assert decision.target_level == "UNCHANGED"
    assert "中转站错误" in advisor.last_error
    assert "HTTP 402" in advisor.last_error
    assert "余额不足" in advisor.last_error


def test_ai_check_uses_synthetic_data_and_never_connects_exchange() -> None:
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True, model="schema-model", base_url="https://relay.example/v1")
    advisor.log = logging.getLogger("test.ai.schema")
    payload = make_ai_payload(action="HOLD", target_level="UNCHANGED", confidence=0.1)
    captured = {}

    def completion(messages):
        captured["messages"] = messages
        return json.dumps(payload, ensure_ascii=False)

    advisor._completion_content = completion
    report = advisor.schema_check()
    request = json.loads(captured["messages"][1]["content"])
    assert request["test_only"] is True
    assert request["symbol"] == "SCHEMA/TEST"
    assert report["ok"] is True
    assert report["exchange_connected"] is False
    assert report["orders_possible"] is False
    assert report["decision"]["action"] == "HOLD"


def test_ai_parse_accepts_next_review_minutes() -> None:
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    payload = make_ai_payload()
    payload["next_review_minutes"] = 30
    decision = advisor._parse_decision(json.dumps(payload, ensure_ascii=False))
    assert decision.next_review_minutes == 30


def test_ai_parse_accepts_missing_next_review_minutes() -> None:
    # 旧响应没有该字段时兼容，视为未提供节奏建议
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    decision = advisor._parse_decision(json.dumps(make_ai_payload(), ensure_ascii=False))
    assert decision.next_review_minutes is None


def test_ai_parse_rejects_invalid_next_review_minutes() -> None:
    advisor = g.AIAdvisor(g.AIConfig(), logging.getLogger("test.ai"))
    for bad in (0, 361, -5, 60.5, "60"):
        payload = make_ai_payload()
        payload["next_review_minutes"] = bad
        with pytest.raises(g.ConfigError, match="next_review_minutes"):
            advisor._parse_decision(json.dumps(payload, ensure_ascii=False))


def make_symbol_decision(symbol: str, ai_decision: g.AIDecision) -> g.SymbolDecision:
    return g.SymbolDecision(
        symbol=symbol,
        score=0.7,
        target_fraction=0.5,
        target_allocation=0.1,
        reason="test",
        signal_result=g.SignalResult(0.7, {}, {}, "2026-08-06T00:00:00+00:00"),
        ai_decision=ai_decision,
    )


def make_review_controller(poll_interval_seconds: int = 60) -> g.GuFaQuantPro:
    controller = object.__new__(g.GuFaQuantPro)
    controller.config = SimpleNamespace(
        runtime=SimpleNamespace(poll_interval_seconds=poll_interval_seconds)
    )
    return controller


def review_decision(next_review_minutes=None) -> g.AIDecision:
    return g.AIDecision(
        "HOLD", "UNCHANGED", 0.5, "s", {},
        next_review_minutes=next_review_minutes,
    )


def test_next_review_takes_tightest_ai_suggestion() -> None:
    controller = make_review_controller()
    decisions = {
        "BTC/USDT": make_symbol_decision("BTC/USDT", review_decision(30)),
        "ETH/USDT": make_symbol_decision("ETH/USDT", review_decision(120)),
    }
    assert controller._next_review_seconds(decisions, True, {}, False) == 1800


def test_next_review_clamps_to_six_hours() -> None:
    controller = make_review_controller()
    decisions = {"BTC/USDT": make_symbol_decision("BTC/USDT", review_decision(720))}
    assert controller._next_review_seconds(decisions, True, {}, False) == 6 * 3600


def test_next_review_floor_is_poll_interval() -> None:
    controller = make_review_controller(poll_interval_seconds=300)
    decisions = {"BTC/USDT": make_symbol_decision("BTC/USDT", review_decision(1))}
    assert controller._next_review_seconds(decisions, True, {}, False) == 300


def test_next_review_conservative_when_halted_paused_or_protective() -> None:
    controller = make_review_controller(poll_interval_seconds=90)
    decisions = {"BTC/USDT": make_symbol_decision("BTC/USDT", review_decision(300))}
    assert controller._next_review_seconds(decisions, False, {}, False) == 90
    assert controller._next_review_seconds(decisions, True, {"BTC/USDT": "stop_loss"}, False) == 90
    assert controller._next_review_seconds(decisions, True, {}, True) == 90


def test_next_review_defaults_when_no_ai_suggestion() -> None:
    controller = make_review_controller(poll_interval_seconds=90)
    decisions = {"BTC/USDT": make_symbol_decision("BTC/USDT", review_decision(None))}
    assert controller._next_review_seconds(decisions, True, {}, False) == 90


def test_ai_check_cli_never_constructs_trading_controller(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    payload = g.default_config_dict()
    payload["ai"]["enabled"] = True
    payload["ai"]["api_key_env"] = "TEST_AI_KEY"
    config_path = write_json(tmp_path / "config.json", payload)
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setenv("GUFA_CREDENTIALS_FILE", str(credentials_path))
    store = g.CredentialStore(credentials_path)
    store.set("TEST_AI_KEY", "not-a-real-key")
    store.save()

    class FakeAdvisor:
        def __init__(self, config, logger, credentials, method_weights=None):
            assert config.enabled is True
            assert credentials.source("TEST_AI_KEY") == "credential-store"

        def schema_check(self):
            return {
                "ok": True,
                "exchange_connected": False,
                "orders_possible": False,
            }

    monkeypatch.setattr(g, "AIAdvisor", FakeAdvisor)
    monkeypatch.setattr(
        g,
        "GuFaQuantPro",
        lambda *args, **kwargs: pytest.fail("ai-check 不得构造交易控制器"),
    )
    assert g.main(["--config", str(config_path), "ai-check"]) == 0
    assert "AI_SCHEMA_TEST=PASS" in capsys.readouterr().out


def make_controller_for_bounds(mode="bounded", minimum=0.6):
    controller = object.__new__(g.GuFaQuantPro)
    controller.config = SimpleNamespace(
        ai=SimpleNamespace(decision_mode=mode, minimum_allow_confidence=minimum)
    )
    controller.ai = SimpleNamespace(_weighted_method_ratio=lambda readings: (0.0, 0))
    return controller


def test_ai_buy_cannot_exceed_rule_cap() -> None:
    controller = make_controller_for_bounds()
    decision = g.AIDecision(
        action="BUY",
        target_level="FULL",
        confidence=0.9,
        summary="buy",
        readings={},
    )
    target, reason = controller._apply_ai_bounds(decision, rule_target=0.5, current_fraction=0.0)
    assert target == 0.5
    assert "rule_cap=0.50" in reason


def test_ai_hold_with_empty_position_allows_rule_target() -> None:
    """空仓 + AI HOLD + 规则目标 > 0：规则目标应生效（AI 未明确看空不阻止建仓）。"""
    controller = make_controller_for_bounds()
    decision = g.AIDecision(
        action="HOLD",
        target_level="UNCHANGED",
        confidence=0.8,
        summary="hold",
        readings={},
    )
    target, _ = controller._apply_ai_bounds(decision, rule_target=0.5, current_fraction=0.0)
    assert target == 0.5
    # 有持仓时 HOLD 仍维持当前仓位
    decision2 = g.AIDecision(
        action="HOLD",
        target_level="UNCHANGED",
        confidence=0.8,
        summary="hold",
        readings={},
    )
    target2, _ = controller._apply_ai_bounds(decision2, rule_target=0.5, current_fraction=0.4)
    assert target2 == 0.4


def test_ai_sell_can_reduce_below_rule_target() -> None:
    controller = make_controller_for_bounds()
    decision = g.AIDecision(
        action="SELL",
        target_level="FLAT",
        confidence=0.9,
        summary="sell",
        readings={},
    )
    target, _ = controller._apply_ai_bounds(decision, rule_target=1.0, current_fraction=1.0)
    assert target == 0.0


def test_ai_fallback_empty_position_allows_rule_target() -> None:
    """AI 失败兜底 + 空仓 + 规则目标 > 0：遵循规则目标建仓（rule_fallback 已判 BUY）。"""
    controller = make_controller_for_bounds()
    decision = g.AIDecision(
        action="BUY",
        target_level="HALF",
        confidence=0.5,
        summary="fallback",
        readings={},
        fallback=True,
    )
    target, _ = controller._apply_ai_bounds(decision, rule_target=0.5, current_fraction=0.0)
    assert target == 0.5
    # 有持仓时 fallback 仍只减不增（fail-closed）
    decision2 = g.AIDecision(
        action="BUY",
        target_level="FULL",
        confidence=0.5,
        summary="fallback",
        readings={},
        fallback=True,
    )
    target2, _ = controller._apply_ai_bounds(decision2, rule_target=1.0, current_fraction=0.4)
    assert target2 == 0.4


def test_low_confidence_ai_holds_current_but_respects_rule_cap() -> None:
    controller = make_controller_for_bounds(minimum=0.7)
    decision = g.AIDecision(
        action="BUY",
        target_level="FULL",
        confidence=0.4,
        summary="uncertain",
        readings={},
    )
    target, reason = controller._apply_ai_bounds(decision, rule_target=0.5, current_fraction=0.25)
    assert target == 0.25
    assert "HOLD" in reason


def _make_split_advisor(**ai_kwargs):
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = g.AIConfig(enabled=True, split_readings=True, **ai_kwargs)
    advisor.log = logging.getLogger("test.ai.split")
    advisor.bind_strategy_weights(g.StrategyConfig().weights)
    return advisor


def _split_aggregate_payload(**overrides):
    payload = {
        "action": "BUY",
        "target_level": "FULL",
        "confidence": 0.8,
        "summary": "十项综合看多",
        "conflicts": ["奇门与六壬冲突"],
        "risk_notes": ["拆分模式测试"],
        "next_review_minutes": 30,
    }
    payload.update(overrides)
    return payload


def test_ai_split_mode_full_success() -> None:
    advisor = _make_split_advisor()
    calls = []

    def completion(messages):
        calls.append(messages)
        system = messages[0]["content"]
        if "汇总决策师" in system:
            return json.dumps(_split_aggregate_payload(), ensure_ascii=False)
        return json.dumps(
            {"bias": "bullish", "confidence": 0.7, "reading": "盘面看多"}, ensure_ascii=False
        )

    advisor._completion_content = completion
    position = g.AccountPosition("BTC/USDT", 0.0, 0.0, 0.0, 0.0, 0.0)
    decision = advisor.interpret("BTC/USDT", make_signal_result(), 1.0, 0.0, "rule full", position, 1000.0)
    assert decision.fallback is False
    assert decision.action == "BUY"
    assert decision.target_level == "FULL"
    assert decision.next_review_minutes == 30
    assert set(decision.readings) == set(g.STRATEGY_NAMES)
    assert all(r.reading == "盘面看多" for r in decision.readings.values())
    assert len(calls) == 11  # 10 个单项 + 1 个聚合


def test_ai_split_single_method_failure_uses_rule_fallback() -> None:
    advisor = _make_split_advisor()
    failed = {"奇门"}

    def completion(messages):
        system = messages[0]["content"]
        if "汇总决策师" in system:
            return json.dumps(_split_aggregate_payload(), ensure_ascii=False)
        user = json.loads(messages[1]["content"])
        if user.get("method") in failed:
            raise g.ConfigError("AI 响应正文必须是非空 JSON string（奇门）")
        return json.dumps(
            {"bias": "bearish", "confidence": 0.6, "reading": "单项解读"}, ensure_ascii=False
        )

    advisor._completion_content = completion
    position = g.AccountPosition("BTC/USDT", 0.0, 0.0, 0.0, 0.0, 0.0)
    decision = advisor.interpret("BTC/USDT", make_signal_result(), 0.5, 0.0, "rule half", position, 1000.0)
    assert decision.fallback is False  # 整体仍成功
    assert "规则兜底" in decision.readings["奇门"].reading
    assert decision.readings["六壬"].reading == "单项解读"
    assert len(decision.readings) == 10


def test_ai_split_relay_error_aborts_and_falls_back() -> None:
    advisor = _make_split_advisor(fail_closed=True)

    def completion(messages):
        raise g.AIRelayError("AI 中转站错误（HTTP 402）: INSUFFICIENT_BALANCE 余额不足", status=402)

    advisor._completion_content = completion
    position = g.AccountPosition("BTC/USDT", 0.0, 0.0, 0.0, 0.0, 0.0)
    decision = advisor.interpret("BTC/USDT", make_signal_result(), 1.0, 0.0, "rule full", position, 1000.0)
    assert decision.fallback is True
    assert decision.action == "HOLD"
    assert "402" in advisor.last_error


def test_ai_split_aggregate_failure_keeps_readings_with_rule_action() -> None:
    advisor = _make_split_advisor()

    def completion(messages):
        system = messages[0]["content"]
        if "汇总决策师" in system:
            raise g.ConfigError("AI 响应正文必须是非空 JSON string")
        return json.dumps(
            {"bias": "bullish", "confidence": 0.7, "reading": "单项解读"}, ensure_ascii=False
        )

    advisor._completion_content = completion
    position = g.AccountPosition("BTC/USDT", 0.0, 0.0, 0.0, 0.0, 0.0)
    decision = advisor.interpret("BTC/USDT", make_signal_result(), 1.0, 0.0, "rule full", position, 1000.0)
    assert decision.fallback is True  # 规则兜底，仓位只降不增
    assert len(decision.readings) == 10  # 十项 AI 解读仍保留
    assert all(r.reading == "单项解读" for r in decision.readings.values())


def test_ai_split_aggregate_payload_truncates_readings() -> None:
    advisor = _make_split_advisor()
    seen: dict = {}

    def completion(messages):
        system = messages[0]["content"]
        if "汇总决策师" in system:
            seen["aggregate_user"] = json.loads(messages[1]["content"])
            return json.dumps(_split_aggregate_payload(), ensure_ascii=False)
        return json.dumps(
            {"bias": "bullish", "confidence": 0.7, "reading": "x" * 300}, ensure_ascii=False
        )

    advisor._completion_content = completion
    position = g.AccountPosition("BTC/USDT", 0.0, 0.0, 0.0, 0.0, 0.0)
    advisor.interpret("BTC/USDT", make_signal_result(), 1.0, 0.0, "rule full", position, 1000.0)
    agg = seen["aggregate_user"]
    assert len(agg["readings"]) == 10
    assert len(agg["readings"]["奇门"]["reading"]) <= 60


def test_ai_split_config_roundtrip() -> None:
    cfg = g.AIConfig.from_dict({"enabled": True, "split_readings": True})
    assert cfg.split_readings is True
    cfg2 = g.AIConfig.from_dict({"enabled": True})
    assert cfg2.split_readings is False


def test_ai_reasoning_effort_config() -> None:
    # 合法值通过
    for value in ("", "low", "medium", "high", "xhigh", "max"):
        cfg = g.AIConfig.from_dict({"enabled": True, "reasoning_effort": value})
        cfg.validate()
        assert cfg.reasoning_effort == value
    # 大小写归一
    cfg = g.AIConfig.from_dict({"enabled": True, "reasoning_effort": "LOW"})
    cfg.validate()
    assert cfg.reasoning_effort == "low"
    # 非法值拒绝
    bad = g.AIConfig.from_dict({"enabled": True, "reasoning_effort": "minimal"})
    with pytest.raises(g.ConfigError):
        bad.validate()


def test_ai_reasoning_effort_passed_to_api() -> None:
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        content = json.dumps(
            {
                "action": "HOLD",
                "target_level": "UNCHANGED",
                "confidence": 0.5,
                "summary": "ok",
                "readings": {name: {"bias": "neutral", "confidence": 0.5, "reading": "x"}
                             for name in g.STRATEGY_NAMES},
                "conflicts": [],
                "risk_notes": [],
                "next_review_minutes": 60,
            },
            ensure_ascii=False,
        )
        return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])]

    # 显式 low
    cfg = g.AIConfig(enabled=True, model="deepseek-v4-flash-0731", reasoning_effort="low")
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = cfg
    advisor.log = logging.getLogger("test.ai")
    advisor.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    advisor._completion_content([{"role": "user", "content": "hi"}])
    assert captured.get("reasoning_effort") == "low"

    # 空 = 不传
    captured.clear()
    cfg2 = g.AIConfig(enabled=True, model="deepseek-v4-flash-0731", reasoning_effort="")
    advisor2 = object.__new__(g.AIAdvisor)
    advisor2.config = cfg2
    advisor2.log = logging.getLogger("test.ai")
    advisor2.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    advisor2._completion_content([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in captured


def test_ai_explain_only_retains_rule_target() -> None:
    controller = make_controller_for_bounds(mode="explain_only")
    decision = g.AIDecision(
        action="SELL",
        target_level="FLAT",
        confidence=1.0,
        summary="sell",
        readings={},
    )
    target, _ = controller._apply_ai_bounds(decision, rule_target=0.5, current_fraction=1.0)
    assert target == 0.5


def test_ai_fail_closed_fallback_never_increases_position() -> None:
    cfg = g.AIConfig(enabled=True, fail_closed=True)
    advisor = object.__new__(g.AIAdvisor)
    advisor.config = cfg
    advisor.log = logging.getLogger("test.ai")
    advisor.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        )
    )
    advisor.bind_strategy_weights(g.StrategyConfig().weights)
    position = g.AccountPosition("BTC/USDT", 1.0, 100.0, 100.0, 90.0, 110.0)
    decision = advisor.interpret(
        "BTC/USDT", make_signal_result(), 1.0, 0.5, "rule full", position, 1000.0
    )
    assert decision.fallback is True
    assert decision.action == "HOLD"
    assert decision.target_level == "UNCHANGED"


def test_ai_fallback_cannot_increase_even_with_high_rule_score() -> None:
    controller = make_controller_for_bounds()
    decision = g.AIDecision(
        action="HOLD",
        target_level="UNCHANGED",
        confidence=0.9,
        summary="fallback",
        readings={},
        enabled=True,
        fallback=True,
    )
    target, reason = controller._apply_ai_bounds(decision, rule_target=1.0, current_fraction=0.25)
    assert target == 0.25
    assert "AI fallback" in reason


def test_legacy_local_paper_fields_are_rejected(tmp_path: Path) -> None:
    for field, value in (
        ("dry_run", True),
        ("paper_starting_cash", 100000.0),
        ("paper_fee_bps", 8.0),
        ("paper_slippage_bps", 5.0),
    ):
        with pytest.raises(g.ConfigError, match="未知字段"):
            load_default(
                tmp_path,
                lambda payload, f=field, v=value: payload["risk"].__setitem__(f, v),
            )


def test_config_v1_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(g.ConfigError, match="当前要求 3"):
        load_default(tmp_path, lambda payload: payload.__setitem__("version", 1))


def test_production_ack_allows_explicit_opt_in(tmp_path: Path) -> None:
    def mutate(payload):
        payload["exchange"]["sandbox"] = False
        payload["risk"]["live_trading_ack"] = "I_UNDERSTAND_LIVE_TRADING_RISK"

    cfg = load_default(tmp_path, mutate)
    assert cfg.exchange.sandbox is False


def test_state_has_no_local_paper_cash(tmp_path: Path) -> None:
    store = g.StateStore(tmp_path / "state.json", "exchange-profile")
    assert "paper_cash" not in store.state.to_dict()


def test_account_snapshot_always_uses_exchange_balance(tmp_path: Path) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(dust_quote=5.0, reject_unmanaged_positions=False)
    gateway.runtime = SimpleNamespace(quote_currency="USDT", symbols=["BTC/USDT"])
    gateway.client = SimpleNamespace(
        fetch_balance=lambda: {
            "free": {"USDT": 900.0, "BTC": 1.0},
            "total": {"USDT": 1000.0, "BTC": 1.0},
        }
    )
    gateway._safe_call = lambda name, function: function()
    snapshot = gateway.account_snapshot({"BTC/USDT": 100.0})
    assert snapshot.quote_free == 900.0
    assert snapshot.equity == 1100.0
    assert snapshot.positions["BTC/USDT"].amount == 1.0
    assert "paper_cash" not in store.state.to_dict()


def test_fill_result_has_no_local_simulation_flag() -> None:
    assert "dry_run" not in g.FillResult.__dataclass_fields__


def test_webhook_url_config_round_trip(tmp_path: Path) -> None:
    cfg = load_default(tmp_path, lambda p: p["runtime"].__setitem__("webhook_url", "https://example.com/hook"))
    assert cfg.runtime.webhook_url == "https://example.com/hook"
    assert g.default_config_dict()["runtime"]["webhook_url"] == ""


def test_webhook_send_receives_json() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length).decode("utf-8")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # noqa: N802
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        ok = g._send_webhook(f"http://127.0.0.1:{port}/hook", {"event": "test", "msg": "你好"})
        assert ok is True
        body = json.loads(received["body"])
        assert body["event"] == "test" and body["msg"] == "你好"
        # 失败路径不抛异常
        assert g._send_webhook("http://127.0.0.1:1/none", {"event": "x"}) is False
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_pause_resume_marker(tmp_path: Path) -> None:
    assert g.cmd_pause_resume(tmp_path, resume=False) == 0
    assert (tmp_path / "pause").exists()
    # 再次 pause 幂等
    assert g.cmd_pause_resume(tmp_path, resume=False) == 0
    assert (tmp_path / "pause").exists()
    assert g.cmd_pause_resume(tmp_path, resume=True) == 0
    assert not (tmp_path / "pause").exists()
    # resume 无标记时幂等
    assert g.cmd_pause_resume(tmp_path, resume=True) == 0


def test_cmd_stats_summary(tmp_path: Path, capsys) -> None:
    equity = [
        {"ts": "2026-08-01T00:00:00+00:00", "equity": 1000.0, "quote_free": 200.0, "paused": False, "status": "ok", "fills": 0},
        {"ts": "2026-08-02T00:00:00+00:00", "equity": 1100.0, "quote_free": 300.0, "paused": False, "status": "ok", "fills": 1},
        {"ts": "2026-08-03T00:00:00+00:00", "equity": 990.0, "quote_free": 400.0, "paused": True, "status": "ok", "fills": 0},
        {"ts": "2026-08-04T00:00:00+00:00", "equity": 1039.5, "quote_free": 500.0, "paused": False, "status": "ok", "fills": 1},
    ]
    audit = [
        {"ts": "2026-08-02T00:00:00+00:00", "event": "order_fill", "plan": {"symbol": "BTC/USDT", "side": "buy"}, "fill": {"filled_amount": 0.1}},
        {"ts": "2026-08-02T00:00:01+00:00", "event": "order_fill", "plan": {"symbol": "ETH/USDT", "side": "buy"}, "fill": {}},
        {"ts": "2026-08-03T00:00:00+00:00", "event": "order_error", "plan": {"symbol": "ETH/USDT"}, "error": "boom"},
    ]
    for row in equity:
        g.append_jsonl(tmp_path / "equity.jsonl", row)
    for row in audit:
        g.append_jsonl(tmp_path / "orders.audit.jsonl", row)
    # 容忍损坏行
    with (tmp_path / "equity.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")

    assert g.cmd_stats(tmp_path, "health.json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cycles"] == 4
    assert out["fills"] == 2
    assert out["fills_by_symbol"] == {"BTC/USDT": 1, "ETH/USDT": 1}
    assert out["order_errors"] == 1
    assert out["order_uncertain"] == 0
    assert out["equity"]["first"] == 1000.0 and out["equity"]["last"] == 1039.5
    assert out["return_pct"] == 3.95
    # 峰值 1100 → 最低 990 回撤 10%
    assert out["max_drawdown_pct"] == 10.0
    assert out["health_exists"] is False


# ====== 8.5.0: 多 AI Key 档案 + api_key_name ======

def test_credentials_ai_keys_roundtrip(tmp_path: Path) -> None:
    """添加、解析、列出、删除命名 AI Key。"""
    store = g.CredentialStore(tmp_path / "creds.json")
    assert store.list_ai_key_names() == []
    store.set_ai_key("rb1", "sk-rb1-abc")
    store.set_ai_key("rb2", "sk-rb2-xyz")
    store.save()
    store2 = g.CredentialStore(tmp_path / "creds.json")
    assert store2.list_ai_key_names() == ["rb1", "rb2"]
    assert store2.resolve_ai_key("rb1", required=True) == "sk-rb1-abc"
    assert store2.resolve_ai_key("rb2", required=True) == "sk-rb2-xyz"
    store2.delete_ai_key("rb1")
    store2.save()
    store3 = g.CredentialStore(tmp_path / "creds.json")
    assert store3.list_ai_key_names() == ["rb2"]
    assert store3.resolve_ai_key("rb1", required=False) == ""


def test_credentials_ai_key_empty_name_raises(tmp_path: Path) -> None:
    store = g.CredentialStore(tmp_path / "creds.json")
    with pytest.raises(g.ConfigError, match="名称不能为空"):
        store.set_ai_key("  ", "value")


def test_credentials_ai_key_required_missing_raises(tmp_path: Path) -> None:
    store = g.CredentialStore(tmp_path / "creds.json")
    with pytest.raises(g.ConfigError, match="AI API Key"):
        store.resolve_ai_key("nonexistent", required=True)


def test_credentials_v1_to_v2_auto_upgrade(tmp_path: Path) -> None:
    """v1 凭据文件自动升级到 v2。"""
    v1 = {"version": 1, "secrets": {"GUFA_API_KEY": "old-key"}}
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(v1), encoding="utf-8")
    store = g.CredentialStore(p)
    assert store.secrets["GUFA_API_KEY"] == "old-key"
    assert store.ai_keys == {}
    raw = json.loads(p.read_text())
    assert raw["version"] == 2
    assert "ai_keys" in raw


def test_credentials_v2_loads_ai_keys(tmp_path: Path) -> None:
    v2 = {"version": 2, "secrets": {"X": "v"}, "ai_keys": {"k1": "v1", "k2": "v2"}}
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(v2), encoding="utf-8")
    store = g.CredentialStore(p)
    assert store.ai_keys == {"k1": "v1", "k2": "v2"}
    assert store.resolve_ai_key("k1", required=True) == "v1"


def test_ai_config_parses_api_key_name() -> None:
    cfg = g.AIConfig.from_dict({"enabled": True, "api_key_name": "rb1", "model": "m1",
                                 "base_url": "https://x/v1"})
    assert cfg.api_key_name == "rb1"


def test_ai_config_validate_strips_api_key_name() -> None:
    cfg = g.AIConfig.from_dict({"enabled": True, "api_key_name": "  rb1  ", "model": "m1",
                                 "base_url": "https://x/v1"})
    cfg.validate()
    assert cfg.api_key_name == "rb1"


def test_ai_config_default_api_key_name_empty() -> None:
    cfg = g.AIConfig()
    assert cfg.api_key_name == ""


def test_credential_store_ignores_empty_ai_keys_on_load(tmp_path: Path) -> None:
    """加载时跳过空 key 名或空 key 值。"""
    v2 = {"version": 2, "secrets": {}, "ai_keys": {"ok": "v", "  ": "x", "bad": "  "}}
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(v2), encoding="utf-8")
    store = g.CredentialStore(p)
    assert store.ai_keys == {"ok": "v"}
    assert store.list_ai_key_names() == ["ok"]


def test_credentials_ai_key_rejects_invalid_chars(tmp_path: Path) -> None:
    store = g.CredentialStore(tmp_path / "creds.json")
    for bad in ["key/name", "key@name", "key name", "<script>"]:
        with pytest.raises(g.ConfigError, match="只能包含"):
            store.set_ai_key(bad, "value")


# =============================================================================
# 多市场（现货/合约）支持
# =============================================================================


def _swap_snapshot(equity: float = 10000.0) -> g.AccountSnapshot:
    """含一个 swap 多头仓位的账户快照。"""
    pos = g.AccountPosition(
        "BTC/USDT",
        amount=0.5,
        price=50000.0,
        quote_value=25000.0,
        avg_entry=48000.0,
        high_water=51000.0,
        market=g.MARKET_SWAP,
        side="long",
        notional=25000.0,
        leverage=2.0,
        contracts=1,
    )
    return g.AccountSnapshot(
        equity=equity,
        quote_free=8000.0,
        quote_total=9000.0,
        positions={g.position_key(g.MARKET_SWAP, "BTC/USDT"): pos},
        timestamp=g.iso_now(),
    )


def _swap_gateway_stub(tmp_path: Path):
    """带合约市场信息的 gateway 桩。"""
    gateway, store = make_gateway_stub(tmp_path)
    gateway.config = SimpleNamespace(ai=SimpleNamespace(decision_mode="bounded"))
    gateway.markets = {
        "BTC/USDT:USDT": {"contractSize": 0.01, "swap": True, "spot": False},
    }
    gateway.risk.min_order_quote = 0.0  # 计划测试不卡最小金额
    return gateway, store


def test_swap_plan_rebalance_buys_contracts(tmp_path: Path) -> None:
    gateway, _ = _swap_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(
        max_symbol_allocation=0.2,
        max_futures_notional_pct=0.5,
        futures_margin_cap_pct=0.5,
        max_leverage=5.0,
        min_rebalance_quote=1.0,
        min_rebalance_pct=0.001,
        min_order_quote=0.0,
    )
    gateway.exchange_cfg = SimpleNamespace(id="okx")
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    gateway.client = SimpleNamespace(
        amount_to_precision=lambda sym, x: round(x, 1),
    )
    snapshot = _swap_snapshot()
    plan = gateway.plan_rebalance(
        snapshot,
        "BTC/USDT",
        target_allocation=0.15,  # 目标名义 1500 USDT（当前 25000，需卖出）
        reason="test",
        market=g.MARKET_SWAP,
        leverage=2.0,
    )
    assert plan is not None
    assert plan.side == "sell"
    assert plan.market == g.MARKET_SWAP
    assert plan.amount > 0
    assert plan.contracts == plan.amount


def test_swap_plan_rebalance_respects_notional_cap(tmp_path: Path) -> None:
    gateway, _ = _swap_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(
        max_symbol_allocation=0.2,
        max_futures_notional_pct=0.1,  # 名义顶 = 1000 USDT
        futures_margin_cap_pct=0.5,
        max_leverage=5.0,
        min_rebalance_quote=1.0,
        min_rebalance_pct=0.001,
        min_order_quote=0.0,
    )
    gateway.exchange_cfg = SimpleNamespace(id="okx")
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    gateway.client = SimpleNamespace(
        amount_to_precision=lambda sym, x: round(x, 1),
    )
    snapshot = _swap_snapshot()
    plan = gateway.plan_rebalance(
        snapshot,
        "BTC/USDT",
        target_allocation=0.15,
        reason="test",
        market=g.MARKET_SWAP,
        leverage=5.0,
    )
    assert plan is not None
    # 卖出方向：减少的名义被钳制在 notional_cap=1000 以内
    assert abs(plan.estimated_quote) <= 1000.0 + 1e-6


def test_swap_execute_sets_posside_and_margin_mode(tmp_path: Path, monkeypatch) -> None:
    gateway, store = make_gateway_stub(tmp_path)
    gateway.risk = SimpleNamespace(
        max_slippage_bps=30.0, order_fill_timeout_seconds=1,
    )
    gateway.exchange_cfg = SimpleNamespace(
        id="okx", client_order_id_param="clientOrderId", max_retries=0,
    )
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    gateway.has_open_order = lambda symbol, market="spot": False
    gateway.estimate_vwap = lambda symbol, side, amount, price: (price, 0.0)

    captured = {}
    created_order: dict = {}

    def fake_set_leverage(lev, symbol, params):
        captured["leverage"] = (lev, symbol, params)
        return {}

    def fake_create_order(symbol, order_type, side, amount, price, params):
        captured["order"] = (symbol, order_type, side, amount, price, params)
        created_order.update(
            {"id": "swap-1", "status": "closed", "filled": amount,
             "average": 50000.0, "fee": {"cost": 0.0}}
        )
        return dict(created_order)

    gateway.client = SimpleNamespace(
        set_leverage=fake_set_leverage,
        create_order=fake_create_order,
        fetch_order=lambda symbol, order_id: dict(created_order),
    )
    monkeypatch.setattr(g.ExchangeGateway, "_network_error_types", staticmethod(lambda: (TimeoutError,)))
    gateway._wait_for_order = lambda symbol, order_id, initial: {
        "id": order_id, "status": "closed", "filled": initial["filled"],
        "average": initial["average"], "fee": initial["fee"],
    }
    gateway._parse_fill = lambda plan, order: g.FillResult(
        order_id="swap-1", symbol=plan.symbol, side=plan.side,
        requested_amount=plan.amount, filled_amount=plan.amount,
        average_price=50000.0, fee_quote=0.0, status="closed",
        market=plan.market, leverage=plan.leverage,
    )
    gateway._apply_fill = lambda fill: None

    plan = g.OrderPlan(
        "BTC/USDT", "buy", 0.1, 50000.0, 5000.0, 0.15, 0.0, "test",
        market=g.MARKET_SWAP, leverage=3.0, contracts=0.1,
    )
    result = gateway.execute(plan)
    assert result.market == g.MARKET_SWAP
    assert captured["leverage"][0] == 3.0
    order_symbol, _, _, _, _, params = captured["order"]
    assert order_symbol == "BTC/USDT:USDT"
    assert params.get("marginMode") == "cross"
    assert params.get("posSide") == "long"
    assert store.state.pending_orders.get("swap:BTC/USDT") is None  # closed 后清理


def test_resolve_market_respects_whitelist_and_holding() -> None:
    bot = object.__new__(g.GuFaQuantPro)
    bot.config = SimpleNamespace(
        exchange=SimpleNamespace(allowed_markets=(g.MARKET_SPOT, g.MARKET_SWAP)),
    )
    bot.gateway = SimpleNamespace(
        markets={"BTC/USDT:USDT": {"swap": True}},
        exchange_symbol=lambda market, symbol: f"{symbol}:USDT",
    )

    class FakeAI:
        def __init__(self, market, symbol="BTC/USDT"):
            self.market = market
            self.symbol = symbol

    # AI 选合约且白名单允许 -> 合约
    assert bot._resolve_market(FakeAI(g.MARKET_SWAP), g.MARKET_SPOT) == g.MARKET_SWAP
    # AI 选合约但白名单只允许现货 -> 回退当前持仓市场
    bot.config.exchange.allowed_markets = (g.MARKET_SPOT,)
    assert bot._resolve_market(FakeAI(g.MARKET_SWAP), g.MARKET_SPOT) == g.MARKET_SPOT
    # AI 选合约但该币无 swap 市场 -> 回退现货
    bot.config.exchange.allowed_markets = (g.MARKET_SPOT, g.MARKET_SWAP)
    assert bot._resolve_market(FakeAI(g.MARKET_SWAP, "OKB/USDT"), g.MARKET_SPOT) == g.MARKET_SPOT
    # AI 未表态（默认 spot）-> spot
    assert bot._resolve_market(FakeAI(g.MARKET_SPOT), g.MARKET_SWAP) == g.MARKET_SPOT
    # 白名单为空 -> 强制现货
    bot.config.exchange.allowed_markets = ()
    assert bot._resolve_market(FakeAI(g.MARKET_SWAP), g.MARKET_SPOT) == g.MARKET_SPOT


def test_parse_fill_converts_swap_contracts_to_base(tmp_path: Path) -> None:
    """合约成交 filled 是张数，必须乘以 contractSize 换算为 base 数量。"""
    gateway, _ = _swap_gateway_stub(tmp_path)
    gateway.markets = {
        "BTC/USDT:USDT": {"contractSize": 0.01, "swap": True},
    }
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    plan = g.OrderPlan(
        "BTC/USDT", "buy", 1.23, 64800.0, 797.0, 0.01, 0.0, "test",
        market=g.MARKET_SWAP, leverage=3.0, contracts=1.23,
    )
    order = {
        "id": "swap-1", "status": "closed",
        "filled": 1.23,          # 张数
        "average": 64800.0,
        "cost": 797.04,          # 名义价值 = 1.23 张 × 0.01 × 64800
        "fee": {"currency": "USDT", "cost": 0.08},
    }
    fill = gateway._parse_fill(plan, order)
    assert fill.filled_amount == 1.23 * 0.01  # 0.0123 BTC
    assert fill.average_price == 64800.0
    assert fill.fee_quote == 0.08
    assert fill.market == g.MARKET_SWAP
    assert fill.leverage == 3.0


def test_parse_fill_spot_keeps_base_amount(tmp_path: Path) -> None:
    """现货成交 filled 就是 base 数量，不做换算。"""
    gateway, _ = make_gateway_stub(tmp_path)
    gateway.runtime = SimpleNamespace(quote_currency="USDT")
    plan = g.OrderPlan("BTC/USDT", "buy", 0.01, 64800.0, 648.0, 0.01, 0.0, "test")
    order = {
        "id": "spot-1", "status": "closed",
        "filled": 0.01,
        "average": 64800.0,
        "cost": 648.0,
        "fee": {"currency": "USDT", "cost": 0.1},
    }
    fill = gateway._parse_fill(plan, order)
    assert fill.filled_amount == 0.01
    assert fill.market == g.MARKET_SPOT


def test_exchange_config_allowed_markets_validation(tmp_path: Path) -> None:
    # 合约必须 OKX + 沙箱（直接用 ExchangeConfig 测 validate，避开 AppConfig.load）
    cfg = g.ExchangeConfig(id="binance", sandbox=True, market_type="swap",
                           allowed_markets=("spot", "swap"))
    with pytest.raises(g.ConfigError, match="仅适配 OKX"):
        cfg.validate()

    cfg = g.ExchangeConfig(id="okx", sandbox=False, market_type="spot",
                           allowed_markets=("spot", "swap"))
    with pytest.raises(g.ConfigError, match="sandbox"):
        cfg.validate()

    # market_type 必须在 allowed_markets 内
    cfg = g.ExchangeConfig(id="okx", sandbox=True, market_type="swap",
                           allowed_markets=("spot",))
    with pytest.raises(g.ConfigError, match="包含在 allowed_markets"):
        cfg.validate()

    # 合法：OKX 沙箱双市场
    cfg = g.ExchangeConfig(id="okx", sandbox=True, market_type="spot",
                           allowed_markets=("spot", "swap"))
    cfg.validate()
    assert cfg.allowed_markets == ("spot", "swap")


# =============================================================================
# 信号触发模式（8.8.0：AI-1 入场 / AI-2 出场 + 条件监听）
# =============================================================================


def test_trigger_condition_round_trip_with_time_after() -> None:
    """time_after 的 value 是 ISO 字符串，序列化往返不得丢失。"""
    cond = g.TriggerCondition(
        kind="time_after", value="2026-08-10T00:00:00+00:00", note="最迟复查"
    )
    again = g.TriggerCondition.from_dict(cond.to_dict())
    assert again.kind == "time_after"
    assert again.value == "2026-08-10T00:00:00+00:00"
    assert again.note == "最迟复查"


def test_trigger_set_round_trip() -> None:
    ts = g.TriggerSet(
        symbol="BTC/USDT",
        entry=[g.TriggerCondition("price_above", 60000.0, note="突破买入")],
        exit=[
            g.TriggerCondition("change_pct_above", 0.08, ref_price=55000.0),
            g.TriggerCondition("change_pct_below", -0.05, ref_price=55000.0),
        ],
        entry_target=1.0,
        entry_market=g.MARKET_SWAP,
        entry_leverage=3.0,
        first_trigger_at="2026-08-09T08:00:00+00:00",
        ref_price=55000.0,
        created_at="2026-08-09T07:00:00+00:00",
    )
    again = g.TriggerSet.from_dict(ts.to_dict())
    assert again.to_dict() == ts.to_dict()


def test_trigger_condition_rejects_unknown_kind() -> None:
    with pytest.raises(g.SafetyError, match="未知触发条件类型"):
        g.TriggerCondition.from_dict({"kind": "moon_align", "value": 1})


def test_evaluate_condition_all_kinds() -> None:
    ev = g.GuFaQuantPro._evaluate_condition
    now = "2026-08-09T10:00:00+00:00"
    cases = [
        (g.TriggerCondition("price_above", 100.0), 120.0, None, None, True),
        (g.TriggerCondition("price_above", 100.0), 80.0, None, None, False),
        (g.TriggerCondition("price_below", 100.0), 80.0, None, None, True),
        (g.TriggerCondition("change_pct_above", 0.08, ref_price=1.0), 1.09, None, None, True),
        (g.TriggerCondition("change_pct_above", 0.08, ref_price=1.0), 1.05, None, None, False),
        (g.TriggerCondition("change_pct_below", -0.05, ref_price=1.0), 0.94, None, None, True),
        (g.TriggerCondition("rsi_above", 70.0), 1.0, 75.0, None, True),
        (g.TriggerCondition("rsi_above", 70.0), 1.0, None, None, False),  # 指标缺失不命中
        (g.TriggerCondition("volume_surge", 2.0), 1.0, None, 3.5, True),
        (g.TriggerCondition("time_after", "2026-08-09T09:00:00+00:00"), 1.0, None, None, True),
        (g.TriggerCondition("time_after", "2026-08-09T11:00:00+00:00"), 1.0, None, None, False),
    ]
    for cond, price, rsi, vol, want in cases:
        assert ev(None, cond, price, rsi, vol, now) == want, cond.kind


def test_trigger_condition_time_after_normalization() -> None:
    """AI 输出 time_after 的兼容性：ISO 时间戳原样保留，纯数字 N 转 N 小时后。"""
    now = g.iso_now()
    # ISO 时间戳
    c1 = g.TriggerCondition.from_dict(
        {"kind": "time_after", "value": "2026-08-10T00:00:00+00:00", "note": "x"},
        now_iso=now,
    )
    assert c1.value == "2026-08-10T00:00:00+00:00"
    # 纯数字 N（24 = 24 小时后）
    c2 = g.TriggerCondition.from_dict(
        {"kind": "time_after", "value": "24", "note": "x"}, now_iso=now,
    )
    base = g.parse_iso(now)
    assert g.parse_iso(c2.value) is not None
    delta = (g.parse_iso(c2.value) - base).total_seconds()
    assert abs(delta - 24 * 3600) < 5
    # 非法值 -> 空串（调用方丢弃，绝不误触发）
    c3 = g.TriggerCondition.from_dict(
        {"kind": "time_after", "value": "??", "note": "x"}, now_iso=now,
    )
    assert c3.value == ""
    # 数值类条件不受影响
    c4 = g.TriggerCondition.from_dict(
        {"kind": "change_pct_above", "value": 0.08, "ref_price": 1.0},
    )
    assert c4.value == 0.08


def test_runtime_trigger_config_validation(tmp_path: Path) -> None:
    def load_default(mutator):
        payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        mutator(payload)
        return g.AppConfig.from_dict(payload)

    # JSON 往返：tuple 型默认值（如 allowed_markets）转成 list 再解析
    payload = json.loads(json.dumps(g.default_config_dict()))
    payload["runtime"]["trigger_mode"] = "signal"
    payload["runtime"]["trigger_poll_seconds"] = 2
    payload["runtime"]["trigger_max_wait_hours"] = 24.0
    write_json(tmp_path / "config.json", payload)
    cfg = g.AppConfig.from_dict(payload)
    assert cfg.runtime.trigger_mode == "signal"
    assert cfg.runtime.trigger_poll_seconds == 2
    assert cfg.runtime.trigger_max_wait_hours == 24.0

    with pytest.raises(g.ConfigError, match="trigger_mode 必须是 signal 或 cycle"):
        load_default(lambda p: p["runtime"].__setitem__("trigger_mode", "bogus"))
    with pytest.raises(g.ConfigError, match="trigger_poll_seconds 至少为 1"):
        load_default(lambda p: p["runtime"].__setitem__("trigger_poll_seconds", 0))
    with pytest.raises(g.ConfigError, match="trigger_max_wait_hours 必须在"):
        load_default(lambda p: p["runtime"].__setitem__("trigger_max_wait_hours", 200))


def test_trigger_mode_default_is_signal() -> None:
    cfg = g.RuntimeConfig()
    assert cfg.trigger_mode == "signal"
    assert cfg.trigger_poll_seconds == 2


def test_bot_state_trigger_fields_round_trip(tmp_path: Path) -> None:
    store = g.StateStore(tmp_path / "state.json", "profile-trigger")
    store.state.triggers["ETH/USDT"] = {
        "symbol": "ETH/USDT",
        "entry": [{"kind": "price_above", "value": 3500.0, "ref_price": 0.0, "note": ""}],
        "exit": [],
        "entry_target": 0.5,
        "entry_market": "spot",
        "entry_leverage": 1.0,
        "first_trigger_at": "",
        "ref_price": 0.0,
        "created_at": "2026-08-09T00:00:00+00:00",
        "updated_at": "2026-08-09T00:00:00+00:00",
    }
    store.state.trigger_skip_until["DOGE/USDT"] = "2026-08-09T12:00:00+00:00"
    store.save()
    again = g.StateStore(tmp_path / "state.json", "profile-trigger")
    assert again.state.triggers["ETH/USDT"]["entry"][0]["kind"] == "price_above"
    assert again.state.trigger_skip_until["DOGE/USDT"] == "2026-08-09T12:00:00+00:00"



