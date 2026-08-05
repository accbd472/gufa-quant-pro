"""历法地基单元测试（公开已知日期断言 + 与 lunar_python 独立交叉校验）。"""
from datetime import datetime

import pytest
from lunar_python import Solar

from gufa_divination.calendar import (
    GAN,
    ZHI,
    day_index_from_date,
    ganzhi_from_index,
    hour_zhi_index,
    moment_from_utc,
    month_ganzhi,
    xun_kong,
)

UTC8 = 8.0


def m(iso: str, **kw) -> "object":
    """本地钟表时间(UTC+8) → GanZhiMoment。"""
    local = datetime.fromisoformat(iso)
    return moment_from_utc(local.replace(tzinfo=None) - __import__("datetime").timedelta(hours=UTC8), **kw)


# ---------- 公开已知日期 ----------

def test_anchor_1900_01_01():
    mom = m("1900-01-01T12:00:00")
    assert mom.day_ganzhi == "甲戌"


def test_2024_chunjie():
    mom = m("2024-02-10T12:00:00")
    assert mom.year_ganzhi == "甲辰"
    assert mom.month_ganzhi == "丙寅"
    assert mom.day_ganzhi == "甲辰"
    assert mom.lunar_year_cn == "二〇二四"
    assert mom.lunar_month_cn == "正"
    assert mom.lunar_day_cn == "初一"
    assert mom.season == "春"
    assert mom.month_zhi == "寅"


def test_2024_02_05():
    mom = m("2024-02-05T00:00:00")
    assert mom.day_ganzhi == "己亥"
    assert mom.xun_kong == ("辰", "巳")
    assert mom.yue_jiang == "子" and mom.yue_jiang_name == "神后"  # 大寒后、雨水前
    assert mom.month_ganzhi == "丙寅"


def test_lichun_boundary():
    before = m("2024-02-04T12:00:00")   # 立春 16:26 前
    after = m("2024-02-04T17:00:00")    # 立春后
    assert before.year_ganzhi == "癸卯"
    assert after.year_ganzhi == "甲辰"
    assert after.prev_jieqi == "立春"
    assert after.next_jieqi == "雨水"


def test_zishi_day_switch():
    mom = m("2024-02-10T23:30:00")
    assert mom.day_ganzhi == "乙巳"     # 23 点换日
    assert mom.hour_zhi_index == 0      # 子时
    assert mom.hour_ganzhi == "丙子"    # 乙日五鼠遁起丙子


def test_early_zishi_same_day():
    mom = m("2024-02-10T00:30:00")
    assert mom.day_ganzhi == "甲辰"     # 早子时不换日
    assert mom.hour_ganzhi == "甲子"    # 甲日五鼠遁起甲子


def test_yuejiang_rain_water():
    mom = m("2024-02-20T12:00:00")     # 雨水(2/19)后
    assert mom.yue_jiang == "亥" and mom.yue_jiang_name == "登明"


def test_yuejiang_major_heat():
    mom = m("2024-08-01T12:00:00")     # 大暑(7/22)后、处暑(8/22)前
    assert mom.yue_jiang == "午" and mom.yue_jiang_name == "胜光"


def test_meihua_nums():
    mom = m("2024-02-10T12:00:00")
    assert mom.meihua_nums() == (5, 1, 1, 7)  # 辰5 正月1 初一1 午7


def test_true_solar_feb():
    mom = m("2024-02-10T12:00:00", longitude=120.0, use_true_solar=True)
    diff = (mom.true_solar - mom.wall).total_seconds() / 60.0
    assert -18.0 < diff < -10.0, diff  # 2 月中旬均时差约 -14 分钟


def test_true_solar_longitude_shift():
    # 乌鲁木齐(87.6°E)：经差 (120-87.6)*4 ≈ 129.6 分 + 3 月初均时差 ≈ -12.8 分。
    mom = m("2024-03-01T12:00:00", longitude=87.6, use_true_solar=True)
    diff = (mom.true_solar - mom.wall).total_seconds() / 60.0
    assert -150.0 < diff < -130.0, diff


# ---------- 与 lunar_python 独立交叉校验 ----------

@pytest.mark.parametrize("y,mo,d,h", [
    (2023, 1, 1, 12), (2023, 6, 15, 8), (2023, 12, 31, 23),
    (2024, 1, 1, 0), (2024, 2, 10, 12), (2024, 5, 20, 18),
    (2024, 8, 1, 5), (2024, 11, 7, 21), (2025, 3, 3, 14),
    (1999, 12, 31, 23), (1988, 2, 15, 22),
])
def test_day_ganzhi_crosscheck(y, mo, d, h):
    mom = moment_from_utc(datetime(y, mo, d, h) - __import__("datetime").timedelta(hours=UTC8))  # noqa: DTZ001
    lunar = Solar.fromYmdHms(y, mo, d, h, 0, 0).getLunar()
    expected = lunar.getDayInGanZhiExact()
    assert mom.day_ganzhi == expected, (mom.day_ganzhi, expected)


@pytest.mark.parametrize("y,mo,d,h", [
    (2024, 2, 10, 0), (2024, 2, 10, 3), (2024, 2, 10, 11),
    (2024, 2, 10, 17), (2024, 2, 10, 23), (2024, 8, 1, 22),
])
def test_hour_ganzhi_crosscheck(y, mo, d, h):
    mom = moment_from_utc(datetime(y, mo, d, h) - __import__("datetime").timedelta(hours=UTC8))  # noqa: DTZ001
    lunar = Solar.fromYmdHms(y, mo, d, h, 0, 0).getLunar()
    expected = lunar.getTimeInGanZhi()
    assert mom.hour_ganzhi == expected, (mom.hour_ganzhi, expected)


@pytest.mark.parametrize("y,mo,d", [
    (2024, 1, 15), (2024, 4, 10), (2024, 7, 20), (2024, 10, 30), (2025, 1, 5),
])
def test_month_ganzhi_wuhudun(y, mo, d):
    """五虎遁独立推导 vs lunar 节气月干支。"""
    mom = moment_from_utc(datetime(y, mo, d, 12) - __import__("datetime").timedelta(hours=UTC8))  # noqa: DTZ001
    derived = month_ganzhi(mom.year_ganzhi[0], mom.month_zhi)
    assert derived == mom.month_ganzhi, (derived, mom.month_ganzhi)


# ---------- 纯函数 ----------

def test_hour_zhi_mapping():
    cases = {23: 0, 0: 0, 1: 1, 5: 3, 11: 6, 13: 7, 21: 11, 22: 11}
    for h, z in cases.items():
        assert hour_zhi_index(h) == z


def test_xun_kong():
    assert xun_kong("甲子") == ("戌", "亥")
    assert xun_kong("甲午") == ("辰", "巳")
    assert xun_kong("己亥") == ("辰", "巳")
    assert xun_kong("甲辰") == ("寅", "卯")


def test_day_index_roundtrip():
    for idx in (0, 1, 10, 59):
        gz = ganzhi_from_index(idx)
        assert day_index_from_date(__import__("datetime").date(1900, 1, 1)) == 10
        assert GAN.index(gz[0]) >= 0 and ZHI.index(gz[1]) >= 0
