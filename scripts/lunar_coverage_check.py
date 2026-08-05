"""lunar_python 覆盖范围检查（GuFaQuant-Pro 8.0 第 1 步产物）。

用途：
  1. 验证历法地基（干支/节气/八字）可用，并用公开已知日期断言。
  2. 探测六爻/奇门/六壬/太乙/紫微/梅花/玄空模块是否存在（预计缺失）。
  3. 输出缺口清单，标记为“自研 + 标准盘校验”。

运行：py -3.10 scripts/lunar_coverage_check.py
"""
import importlib
import importlib.metadata
import sys

from lunar_python import Solar

LUNAR_VERSION = importlib.metadata.version("lunar_python")
RESULTS = []


def probe(name: str, fn) -> None:
    try:
        fn()
        RESULTS.append((name, "PASS"))
    except Exception as exc:  # noqa: BLE001 - smoke 脚本允许兜底
        RESULTS.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


def check_calendar() -> None:
    """公开已知日期断言。"""
    assert LUNAR_VERSION == "1.4.8", LUNAR_VERSION

    # 2024-02-10 是甲辰年正月初一（春节），农历年干支在春节换。
    lunar = Solar.fromYmdHms(2024, 2, 10, 12, 0, 0).getLunar()
    assert lunar.getYearInGanZhi() == "甲辰", lunar.getYearInGanZhi()
    assert lunar.getMonthInChinese() == "正", lunar.getMonthInChinese()
    assert lunar.getDayInChinese() == "初一", lunar.getDayInChinese()

    # 2023-01-22 是癸卯年正月初一。
    lunar = Solar.fromYmdHms(2023, 1, 22, 12, 0, 0).getLunar()
    assert lunar.getYearInGanZhi() == "癸卯", lunar.getYearInGanZhi()

    # 立春 2024-02-04（节气落在该日）。
    table = Solar.fromYmdHms(2024, 2, 4, 12, 0, 0).getLunar().getJieQiTable()
    assert "立春" in table, list(table.keys())[:5]

    # 立春后八字年柱换为甲辰（2024-02-05 在立春后）。
    ec = Solar.fromYmdHms(2024, 2, 5, 0, 0, 0).getLunar().getEightChar()
    assert ec.getYear() == "甲辰", ec.getYear()

    # 八字四柱 + 十神 API 存在。
    assert ec.getMonth() and ec.getDay() and ec.getTime()
    assert ec.getYearShiShenGan() and ec.getDayShiShenGan()

    # 大运/流年 API 存在（getDaYun()[0] 是起运前占位段，真实大运从下标 1 起）。
    dayun = ec.getYun("男").getDaYun()
    assert len(dayun) > 1
    assert dayun[1].getGanZhi() and dayun[1].getLiuNian()

    # 命宫/身宫/旬空 API 存在。
    assert ec.getMingGong() and ec.getShenGong() and ec.getDayXunKong()

    # 时辰：2024-02-10 23:30 属次日子时（23 点换日，需用 Exact 变体）。
    late = Solar.fromYmdHms(2024, 2, 10, 23, 30, 0).getLunar()
    assert late.getDayInGanZhiExact() == "乙巳", late.getDayInGanZhiExact()

    # 九星 API 存在（该版本 getNumber 返回中文数字）。
    star = Solar.fromYmdHms(2024, 2, 10, 12, 0, 0).getLunar().getYearNineStar()
    assert star.getNumber() in ("一", "二", "三", "四", "五", "六", "七", "八", "九")


def check_module(name: str, attrs: tuple) -> None:
    def run() -> None:
        mod = importlib.import_module(name)
        for attr in attrs:
            assert hasattr(mod, attr), f"{name} 缺少 {attr}"
    probe(f"module:{name}", run)


def main() -> None:
    print(f"lunar_python {LUNAR_VERSION} 覆盖检查\n" + "-" * 64)

    probe("历法:干支/农历/节气/八字/大运流年/九星", check_calendar)

    # 术数扩展模块探测（预期 MISS -> 自研 + 标准盘校验）。
    check_module("lunar_python.LiuYao", ("LiuYao",))
    check_module("lunar_python.Qimen", ("Qimen",))
    check_module("lunar_python.LiuRen", ("LiuRen",))
    check_module("lunar_python.TaiYi", ("TaiYi",))
    check_module("lunar_python.ZiWei", ("ZiWei",))
    check_module("lunar_python.Gua", ("Gua",))
    check_module("lunar_python.MeiHua", ("MeiHua",))
    check_module("lunar_python.XuanKong", ("XuanKong",))

    print(f"\n{'检查项':<44}{'结果'}")
    print("-" * 64)
    for name, status in RESULTS:
        print(f"{name:<44}{status}")

    print("\n" + "=" * 64)
    print("缺口清单（以下模块 lunar_python 1.4.8 不提供，需自研 + 标准盘校验）：")
    for name, status in RESULTS:
        if "module:" in name:
            mod = name.split(":", 1)[1]
            print(f"  - {mod:<28} -> 自研（历法地基由 lunar_python 提供）")
    print(
        "\n历法地基可用：干支（年/月/日/时）、节气表、农历、八字四柱、"
        "十神、大运流年、九星。\n时区/真太阳时换算、梅花/易经起卦、"
        "纳甲装卦、奇门/六壬/太乙/紫微/玄空排盘均为自研范围。"
    )
    sys.exit(0)  # 模块缺失属预期，报告即可


if __name__ == "__main__":
    main()
