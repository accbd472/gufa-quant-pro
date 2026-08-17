"""GuFaQuant-Pro 8.0 —— 太乙神数排盘器（简式）。

规则来源与局限（如实披露，避免冒充权威）：
- 太乙积年：锚点《太乙统宗宝鉴》"大德七年癸卯（1303），岁积一千零一十五万五千二百一十九年"，
  即 积年 = 10155219 + (公元年 - 1303)。
- 太乙行九宫：《易纬·乾凿度》"太乙行九宫"法，九宫序：一乾、二离、三艮、四震、五中、
  六兑、七坤、八坎、九巽（右旋，乾巽一九相对）。
- 岁计落宫：太乙自一宫起、岁行一宫、九宫周行（简式）。
- 月计/日计：以岁计为基准按农历月 / 儒略日推进（简式）。
- 十六神：子地主、丑阳德、艮和德、寅吕申、卯高丛、辰太阳、巽太炅、巳太神、
  午大神、未大威、坤天道、申大武、酉武德、戌太簇、乾阴主、亥阴德；以月支定位（简式）。

说明：太乙神数流派算法分歧大（黄宗羲讥其"经纬混淆行度无稽"），本实现仅作
可复现的结构化排盘输入，供 AI 断卦师参考，不代表权威排盘。
"""

from __future__ import annotations

from gufa_paipan import BasePaipan, TaiyiChart

# 太乙九宫（乾凿度序）
TAIYI_PALACES: dict[int, str] = {
    1: "一宫乾", 2: "二宫离", 3: "三宫艮", 4: "四宫震", 5: "五宫中",
    6: "六宫兑", 7: "七宫坤", 8: "八宫坎", 9: "九宫巽",
}

# 十六神（按地支/四维序）
SIXTEEN_GODS: list[str] = [
    "地主", "阳德", "和德", "吕申", "高丛", "太阳", "太炅", "太神",
    "大神", "大威", "天道", "大武", "武德", "太簇", "阴主", "阴德",
]
GOD_KEYS: list[str] = [
    "子", "丑", "艮", "寅", "卯", "辰", "巽", "巳",
    "午", "未", "坤", "申", "酉", "戌", "乾", "亥",
]

JIYUAN_ANCHOR_YEAR = 1303   # 大德七年（癸卯）
JIYUAN_ANCHOR_VALUE = 10155219

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"


def taiyi_jiyuan(year: int) -> int:
    """太乙积年（《太乙统宗宝鉴》锚点）。"""
    return JIYUAN_ANCHOR_VALUE + (year - JIYUAN_ANCHOR_YEAR)


def _ganzhi_from_number(n: int) -> str:
    """六十甲子序数（1 起）转干支。"""
    g = GAN[(n - 1) % 10]
    z = ZHI[(n - 1) % 12]
    return g + z


class TaiyiPaipan(BasePaipan):
    """太乙神数排盘器（简式）。"""

    method = "太乙"

    def _build(self, ctx, chart_type: str) -> TaiyiChart:
        year = ctx.solar_dt.year
        jiyuan = taiyi_jiyuan(year)
        taiyi_num = jiyuan % 60 or 60
        gz = _ganzhi_from_number(taiyi_num)

        # 岁计落宫（一宫起、岁行一宫、九宫周行）
        sui_gong = (jiyuan - 1) % 9 + 1
        # 月计落宫（月柱地支推进，8.9：不再用公历月）
        lunar_month = ctx.jieqi_month     # 节气月序（立春=1 寅月）
        yue_gong = (jiyuan * 12 + lunar_month - 1) % 9 + 1
        # 日计落宫（儒略日推进）
        jd = self._julian_day(ctx.solar_dt)
        ri_gong = int(jd) % 9 + 1

        # 十六神（以月柱地支定位，8.9：不再由公历月推算）
        month_zhi = ctx.month_gz[1]
        sixteen: dict[str, str] = {}
        start = GOD_KEYS.index(month_zhi) if month_zhi in GOD_KEYS else 0
        for i in range(16):
            key = GOD_KEYS[(start + i) % 16]
            sixteen[key] = SIXTEEN_GODS[(start + i) % 16]

        # 三基（简式）：天基=岁计太乙、地基=大游、人基=小游
        sanji = {
            "天基": TAIYI_PALACES[sui_gong],
            "地基": TAIYI_PALACES[(sui_gong + 3) % 9 + 1],
            "人基": TAIYI_PALACES[(sui_gong + 6) % 9 + 1],
        }

        notes = [
            f"太乙积年 {jiyuan}（《太乙统宗宝鉴》锚点：大德七年癸卯岁积{JIYUAN_ANCHOR_VALUE}）",
            f"太乙数 {taiyi_num}（{gz}）",
            "岁计落宫采用《乾凿度》太乙行九宫简式（一宫起岁行一宫）；月计/日计以岁计为基准按农历月/儒略日推进；"
            "太乙流派算法分歧大，本盘为结构化简式",
        ]

        chart = TaiyiChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + notes,
            taiyi_year=jiyuan,
            taiyi_gong=TAIYI_PALACES[sui_gong],
            yue_gong=TAIYI_PALACES[yue_gong],
            ri_gong=TAIYI_PALACES[ri_gong],
            sixteen_gods=sixteen,
            sanji=sanji,
            wufu={"大游": TAIYI_PALACES[(sui_gong + 1) % 9 + 1]},
            wenyun=f"{gz}年（太乙数{taiyi_num}）",
        )
        return chart

    @staticmethod
    def _julian_day(dt) -> float:
        """公历转儒略日（弗拉马利翁简式）。"""
        y, m, d = dt.year, dt.month, dt.day
        if m <= 2:
            y -= 1
            m += 12
        a = y // 100
        b = 2 - a + a // 4
        return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5

    def natal(self, ctx) -> TaiyiChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> TaiyiChart:
        return self._build(ctx, "current")
