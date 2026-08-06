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
    assert cfg.selection.min_score == 0.55


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
    payload["version"] = g.STATE_VERSION - 1
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
    gateway.has_open_order = lambda symbol: False
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
        def __init__(self, config, logger, credentials):
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
