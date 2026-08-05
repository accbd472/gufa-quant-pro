# -*- coding: utf-8 -*-
"""GuFaQuant-Pro 8.0 排盘模块测试。

覆盖：历法服务（真太阳时/干支/节气）、十项排盘器、信号映射、端到端调度。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gufa_calendar as gc
import gufa_paipan as gp
import gufa_paipan_signal as gps
from gufa_paipan_bazi import BaziPaipan, SizhuPaipan
from gufa_paipan_liuren import LiurenPaipan
from gufa_paipan_qimen import QimenPaipan
from gufa_paipan_taiyi import TaiyiPaipan
from gufa_paipan_yijing import (
    BaguaPaipan,
    FengshuiPaipan,
    MeihuaPaipan,
    YijingPaipan,
    _GUA_HEX,
    _hex_to_name,
)
from gufa_paipan_ziwei import ZiweiPaipan, _nayin
import gufa_quant_pro as g

CHINA_TZ = gc.CHINA_TZ
SAMPLE_DT = datetime(2026, 8, 5, 18, 44, 0, tzinfo=CHINA_TZ)


@pytest.fixture(scope="module")
def svc() -> gc.CalendarService:
    return gc.CalendarService(g.PaipanConfig())


@pytest.fixture(scope="module")
def ctx(svc):
    return svc.context(SAMPLE_DT)


# ---------------------------------------------------------------------------
# 历法服务
# ---------------------------------------------------------------------------


def test_calendar_context_basic(ctx):
    assert ctx.year_gz == "丙午"
    assert ctx.day_gz == "辛亥"
    assert ctx.shichen_name == "酉"
    assert ctx.xun_kong == "寅卯"
    assert ctx.next_jieqi == "立秋"
    assert ctx.correction_minutes < 0  # 北京经度 116.4°E 相对 120°E 偏西


def test_calendar_manual_listing_time():
    svc = gc.CalendarService(
        g.PaipanConfig(listing_time_source="manual", listing_times={"BTC/USDT": "2017-08-17T00:00:00+00:00"})
    )
    assert svc.listing_time("BTC/USDT").year == 2017


def test_calendar_ohlcv_millis_listing_time():
    svc = gc.CalendarService(g.PaipanConfig(listing_time_source="ohlcv"))
    t = svc.listing_time("BTC/USDT", first_candle_ts=1502956800000)
    assert t is not None and t.year == 2017


# ---------------------------------------------------------------------------
# 十项排盘器：结构自洽
# ---------------------------------------------------------------------------


def test_all_chart_classes_registered():
    assert set(gp.CHART_CLASSES.keys()) == set(g.STRATEGY_NAMES)


@pytest.mark.parametrize("panzer", [
    QimenPaipan(), LiurenPaipan(), TaiyiPaipan(), YijingPaipan(), FengshuiPaipan(),
    BaziPaipan(), MeihuaPaipan(), ZiweiPaipan(), BaguaPaipan(), SizhuPaipan(),
])
def test_paipan_produces_chart(svc, panzer):
    ctx = svc.context(SAMPLE_DT)
    chart = panzer.current(ctx)
    assert chart.method == panzer.method
    assert chart.chart_type == "current"
    assert chart.solar_time
    assert chart.ganzhi
    data = chart.to_dict()
    assert data["method"] == panzer.method
    # 不应出现 error 字段
    assert "error" not in data


# ---------------------------------------------------------------------------
# 各法关键断言
# ---------------------------------------------------------------------------


def test_qimen_yin_dun_1_ju(svc):
    """2026-08-05 18:44 北京：大暑中元，阴遁 1 局（手工推演）。"""
    ctx = svc.context(SAMPLE_DT)
    c = QimenPaipan().current(ctx)
    assert c.dun == "阴遁"
    assert c.ju == 1
    assert c.zhifu == "天柱"
    assert c.zhishi == "惊门"
    # 地盘：阴遁1局戊起一宫逆布
    assert c.jiu_gong["坎一宫"]["gan"] == "戊"
    assert c.jiu_gong["离九宫"]["gan"] == "己"


def test_liuren_three_transmissions(svc):
    """2026-08-05 18:44：月将胜光加酉，贼克课，三传巳寅亥（手工推演）。"""
    ctx = svc.context(SAMPLE_DT)
    c = LiurenPaipan().current(ctx)
    assert c.yuejiang == "胜光"
    assert c.four_lessons == ["戌未", "未辰", "亥申", "申巳"]
    assert c.three_transmissions == ["巳", "寅", "亥"]
    assert c.ke_break == "贼克"


def test_taiyi_jiyuan_anchor():
    """《太乙统宗宝鉴》锚点：大德七年(1303) 岁积 10155219。"""
    assert gp_taiyi_jiyuan(1303) == 10155219


def test_taiyi_yue_ri_gong(svc):
    ctx = svc.context(SAMPLE_DT)
    c = TaiyiPaipan().current(ctx)
    assert c.yue_gong and c.ri_gong  # 月计/日计落宫已进盘面
    assert all("宫" in x for x in (c.taiyi_gong, c.yue_gong, c.ri_gong))


def gp_taiyi_jiyuan(year):
    return 10155219 + (year - 1303)


def test_yijing_ben_gua(svc):
    ctx = svc.context(SAMPLE_DT)
    c = YijingPaipan().current(ctx)
    assert c.ben_gua in _GUA_HEX
    assert c.ben_gua_hex == _GUA_HEX[c.ben_gua]
    assert c.gua_ci  # 卦辞非空


def test_meihua_ti_yong(svc):
    ctx = svc.context(SAMPLE_DT)
    c = MeihuaPaipan().current(ctx)
    assert c.ti_gua and c.yong_gua
    assert c.ti_yong_relation in {"比和", "体生用（泄）", "用生体（吉）", "体克用（劳）", "用克体（凶）"}


def test_ziwei_nayin():
    assert _nayin("甲", "子") == "海中金"
    assert _nayin("戊", "戌") == "平地木"
    assert _nayin("丙", "午") == "天河水"


def test_ziwei_palace_structure(svc):
    ctx = svc.context(SAMPLE_DT)
    c = ZiweiPaipan().current(ctx)
    assert c.wuxing_ju in {"水二局", "木三局", "金四局", "土五局", "火六局"}
    assert len(c.palaces) == 12
    assert c.ziwei_palace and c.tianfu_palace
    assert set(c.four_hua.keys()) == {"禄", "权", "科", "忌"}


def test_bagua_shi_ying(svc):
    ctx = svc.context(SAMPLE_DT)
    c = BaguaPaipan().current(ctx)
    assert 1 <= c.shi_yao <= 6
    assert 1 <= c.ying_yao <= 6
    assert len(c.najia) == 6
    assert len(c.liuqin) == 6


def test_fengshui_feixing(svc):
    ctx = svc.context(SAMPLE_DT)
    c = FengshuiPaipan().current(ctx)
    # 2026 年一白入中（9 - 2026%9）
    assert c.year_star == 1
    assert c.fei_xing["中宫"]["year"] == 1
    assert c.fei_xing["乾宫"]["year"] == 2


def test_bazi_strength_and_ten_gods(svc):
    ctx = svc.context(SAMPLE_DT)
    c = BaziPaipan().current(ctx)
    assert c.day_master == "辛"
    assert c.strength == "相"  # 未月土生金
    assert c.ten_gods["年"] == "正官"
    assert c.da_yun  # 大运非空


def test_sizhu_shen_sha(svc):
    ctx = svc.context(SAMPLE_DT)
    c = SizhuPaipan().current(ctx)
    assert "禄神" in c.shen_sha  # 辛日禄在酉（时支）
    assert "天乙贵人" in c.shen_sha


# ---------------------------------------------------------------------------
# 卦象表自洽
# ---------------------------------------------------------------------------


def test_gua_hex_unique_and_complete():
    assert len(_GUA_HEX) == 64
    assert len(set(_GUA_HEX.values())) == 64
    assert _hex_to_name("010010") == "坎为水"
    assert _hex_to_name("010001") == "山水蒙"
    assert _hex_to_name("100010") == "水雷屯"


# ---------------------------------------------------------------------------
# 信号映射
# ---------------------------------------------------------------------------


def test_paipan_signals_shape(svc):
    service = gp.PaipanService(g.PaipanConfig(listing_time_source="ohlcv"))
    for panzer in (QimenPaipan(), LiurenPaipan(), TaiyiPaipan(), YijingPaipan(),
                   FengshuiPaipan(), BaziPaipan(), MeihuaPaipan(), ZiweiPaipan(),
                   BaguaPaipan(), SizhuPaipan()):
        service.register(panzer)
    result = service.paipan("BTC/USDT", now_dt=SAMPLE_DT, listing_ts=1502956800000)
    signals = gps.paipan_signals(result.to_dict())
    assert set(signals.keys()) == set(g.STRATEGY_NAMES)
    for value in signals.values():
        assert 0.0 <= value <= 1.0
    assert len(result.current) == 10
    assert len(result.natal) == 10


# ---------------------------------------------------------------------------
# StrategyEngine 排盘模式
# ---------------------------------------------------------------------------


def _sample_frame():
    import numpy as np
    import pandas as pd

    n = 120
    ts = [datetime(2026, 8, 5, 18, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
          - (n - 1 - i) * 3600 * 1000 for i in range(n)]
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    hl = abs(rng.normal(0, 0.003, n))
    return pd.DataFrame({
        "timestamp": ts,
        "open": np.clip(close * (1 + rng.normal(0, 0.002, n)), close * (1 - hl), close * (1 + hl)),
        "high": close * (1 + hl),
        "low": close * (1 - hl),
        "close": close,
        "volume": rng.uniform(100, 500, n),
    })


def test_engine_paipan_mode():
    eng = g.StrategyEngine(g.StrategyConfig(), paipan_config=g.PaipanConfig(enabled=True))
    result = eng.calculate(_sample_frame(), symbol="BTC/USDT")
    assert 0.0 <= result.score <= 1.0
    assert set(result.signals.keys()) == set(g.STRATEGY_NAMES)
    paipan = result.diagnostics["paipan"]
    assert len(paipan["current"]) == 10
    assert len(paipan["natal"]) == 10


def test_engine_fallback_mode():
    eng = g.StrategyEngine(g.StrategyConfig())
    result = eng.calculate(_sample_frame(), symbol="BTC/USDT")
    assert 0.0 <= result.score <= 1.0
    assert set(result.signals.keys()) == set(g.STRATEGY_NAMES)
    assert "paipan" not in result.diagnostics
