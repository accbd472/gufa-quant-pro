"""GuFaQuant-Pro 8.0 —— 卦学与风水排盘器。

包含：易经（时间起卦+卦辞）、梅花易数（时间起卦+体用）、
八卦（京房八宫世应卦）、六爻（纳甲装卦/六亲/六神）、风水（玄空飞星）。

规则依据：
- 起卦：梅花易数时间起卦（年支数+月+日=上卦，+时支数=下卦，总数除6=动爻）。
- 六十四卦：上下卦名/卦象与卦辞见 gufa_yijing_data（通行本）。
- 京房八宫卦序与世应：本宫世六应三，一世世一应四……游魂世四，归魂世三。
- 纳甲：乾内甲子寅辰外壬午申戌；坎内戊寅辰午外戊申戌子；艮内丙辰午申外丙戌子寅；
  震内庚子寅辰外庚午申戌；巽内辛丑亥酉外辛未巳卯；离内己卯丑亥外己酉未巳；
  坤内乙未巳卯外癸丑亥酉；兑内丁巳卯丑外丁亥酉未。
- 六亲：以卦宫五行为我（乾兑金、离火、震巽木、坎水、艮坤土），爻支五行论
  兄弟/父母/子孙/官鬼/妻财。
- 六神：日干起（甲乙青龙、丙丁朱雀、戊己勾陈、庚辛腾蛇、壬癸玄武）。
- 玄空飞星：三元九运（2024-2043 下元九紫）；年星入中 = 9-((年-2000) mod 9)；
  月星以年支定正月星（子午卯酉八白、辰戌丑未五黄、寅申巳亥二黑），逐月顺数；
  九宫洛书顺飞（中→乾→兑→艮→离→坎→坤→震→巽）。

梅花/六爻/八卦/易经 共用于时间起卦；风水独立。
"""

from __future__ import annotations

from gufa_paipan import BaguaChart, BasePaipan, FengshuiChart, MeihuaChart, YijingChart

# =============================================================================
# 卦学基础
# =============================================================================

ZHI_ORDER = "子丑寅卯辰巳午未申酉戌亥"

# 先天卦数（梅花易数）：乾1兑2离3震4巽5坎6艮7坤8
BAGUA_NUM = {"乾": 1, "兑": 2, "离": 3, "震": 4, "巽": 5, "坎": 6, "艮": 7, "坤": 8}
# 卦象（下卦、上卦 -> 64卦名）行=下卦，列=上卦，序：乾兑离震巽坎艮坤
_GUA_NAMES = [
    ["乾为天", "泽天夬", "火天大有", "雷天大壮", "风天小畜", "水天需", "山天大畜", "地天泰"],
    ["天泽履", "兑为泽", "火泽睽", "雷泽归妹", "风泽中孚", "水泽节", "山泽损", "地泽临"],
    ["天火同人", "泽火革", "离为火", "雷火丰", "风火家人", "水火既济", "山火贲", "地火明夷"],
    ["天雷无妄", "泽雷随", "火雷噬嗑", "震为雷", "风雷益", "水雷屯", "山雷颐", "地雷复"],
    ["天风姤", "泽风大过", "火风鼎", "雷风恒", "巽为风", "水风井", "山风蛊", "地风升"],
    ["天水讼", "泽水困", "火水未济", "雷水解", "风水涣", "坎为水", "山水蒙", "地水师"],
    ["天山遁", "泽山咸", "火山旅", "雷山小过", "风山渐", "水山蹇", "艮为山", "地山谦"],
    ["天地否", "泽地萃", "火地晋", "雷地豫", "风地观", "水地比", "山地剥", "坤为地"],
]
_UPPER = ["乾", "兑", "离", "震", "巽", "坎", "艮", "坤"]
_LOWER = ["乾", "兑", "离", "震", "巽", "坎", "艮", "坤"]

# 八卦卦象（自下而上，1=阳）
_BAGUA_HEX = {"乾": "111", "兑": "110", "离": "101", "震": "100",
             "巽": "011", "坎": "010", "艮": "001", "坤": "000"}

# 卦名 -> hex（由上下卦卦象计算，避免手写错误）
_GUA_HEX: dict[str, str] = {}
for _row, _lower in enumerate(_LOWER):
    for _col, _upper in enumerate(_UPPER):
        _GUA_HEX[_GUA_NAMES[_row][_col]] = _BAGUA_HEX[_lower] + _BAGUA_HEX[_upper]

# 京房八宫卦序（每宫8卦：本宫、一世…五世、游魂、归魂）
BA_GONG = {
    "乾": ["乾为天", "天风姤", "天山遁", "天地否", "风地观", "山地剥", "火地晋", "火天大有"],
    "坎": ["坎为水", "水泽节", "水雷屯", "水火既济", "泽火革", "雷火丰", "地火明夷", "地水师"],
    "艮": ["艮为山", "山火贲", "山天大畜", "山泽损", "火泽睽", "天泽履", "风泽中孚", "风山渐"],
    "震": ["震为雷", "雷地豫", "雷水解", "雷风恒", "地风升", "水风井", "泽风大过", "泽雷随"],
    "巽": ["巽为风", "风天小畜", "风火家人", "风雷益", "天雷无妄", "火雷噬嗑", "山雷颐", "山风蛊"],
    "离": ["离为火", "火山旅", "火风鼎", "火水未济", "山水蒙", "风水涣", "天水讼", "天火同人"],
    "坤": ["坤为地", "地雷复", "地泽临", "地天泰", "雷天大壮", "泽天夬", "水天需", "水地比"],
    "兑": ["兑为泽", "泽水困", "泽地萃", "泽山咸", "水山蹇", "地山谦", "雷山小过", "雷泽归妹"],
}
# 世爻位置（8宫序 -> 世爻 1..6，0=上爻用6）
SHI_YAO = {0: 6, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 4, 7: 3}
# 应爻 = 世爻 +3（环）
GONG_WUXING = {"乾": "金", "兑": "金", "离": "火", "震": "木", "巽": "木", "坎": "水", "艮": "土", "坤": "土"}
ZHI_WUXING = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
              "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
# 纳甲（八卦 -> 六爻干支，自下而上）
NAJIA = {
    "乾": ["甲子", "甲寅", "甲辰", "壬午", "壬申", "壬戌"],
    "坎": ["戊寅", "戊辰", "戊午", "戊申", "戊戌", "戊子"],
    "艮": ["丙辰", "丙午", "丙申", "丙戌", "丙子", "丙寅"],
    "震": ["庚子", "庚寅", "庚辰", "庚午", "庚申", "庚戌"],
    "巽": ["辛丑", "辛亥", "辛酉", "辛未", "辛巳", "辛卯"],
    "离": ["己卯", "己丑", "己亥", "己酉", "己未", "己巳"],
    "坤": ["乙未", "乙巳", "乙卯", "癸丑", "癸亥", "癸酉"],
    "兑": ["丁巳", "丁卯", "丁丑", "丁亥", "丁酉", "丁未"],
}
SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}
LIUSHEN = ["青龙", "朱雀", "勾陈", "腾蛇", "白虎", "玄武"]  # 日干起序


def _time_gua(ctx):
    """梅花时间起卦：返回 (上卦名, 下卦名, 动爻位1..6, 本卦名, 本卦hex)。"""
    year_zhi = ctx.year_gz[1]
    year_num = ZHI_ORDER.index(year_zhi) + 1
    month = ctx.solar_dt.month
    day = ctx.solar_dt.day
    shi_num = ctx.shichen_index + 1  # 子=1...亥=12
    upper_num = (year_num + month + day) % 8 or 8
    lower_num = (year_num + month + day + shi_num) % 8 or 8
    dong = (year_num + month + day + shi_num) % 6 or 6
    upper = next(k for k, v in BAGUA_NUM.items() if v == upper_num)
    lower = next(k for k, v in BAGUA_NUM.items() if v == lower_num)
    gua_name = _GUA_NAMES[_LOWER.index(lower)][_UPPER.index(upper)]
    return upper, lower, dong, gua_name, _GUA_HEX.get(gua_name, "")


def _change_hex(hex_str: str, dong: int) -> str:
    """动爻变爻（1=初爻）。"""
    chars = list(hex_str)
    idx = len(chars) - dong
    chars[idx] = "0" if chars[idx] == "1" else "1"
    return "".join(chars)


def _hex_to_name(hex_str: str) -> str:
    """卦象串 -> 卦名。"""
    for name, hx in _GUA_HEX.items():
        if hx == hex_str:
            return name
    return ""


def _hu_gua(hex_str: str) -> str:
    """互卦：取二三四爻为下卦、三四五爻为上卦。"""
    chars = list(hex_str)
    lower = chars[1] + chars[2] + chars[3]
    upper = chars[2] + chars[3] + chars[4]
    return _hex_to_name(lower + upper)


# =============================================================================
# 1 易经（时间起卦 + 卦辞）
# =============================================================================


class YijingPaipan(BasePaipan):
    """易经起卦：时间起卦，输出本卦/变卦/动爻/卦辞。"""

    method = "易经"

    def _build(self, ctx, chart_type: str) -> YijingChart:
        from gufa_yijing_data import GUA_CI  # 卦辞数据

        upper, lower, dong, ben_name, ben_hex = _time_gua(ctx)
        bian_hex = _change_hex(ben_hex, dong)
        bian_name = _hex_to_name(bian_hex) or ""
        chart = YijingChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [f"时间起卦：{upper}上{lower}下，第{dong}爻动"],
            ben_gua=ben_name,
            ben_gua_hex=ben_hex,
            bian_gua=bian_name,
            bian_gua_hex=bian_hex,
            dong_yao=[dong],
            gua_ci=GUA_CI.get(ben_name, ""),
            yao_ci=None,
        )
        return chart

    def natal(self, ctx) -> YijingChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> YijingChart:
        return self._build(ctx, "current")


# =============================================================================
# 2 梅花易数（时间起卦 + 体用）
# =============================================================================


class MeihuaPaipan(BasePaipan):
    """梅花易数：时间起卦，互卦变卦，体用生克。"""

    method = "梅花"

    def _build(self, ctx, chart_type: str) -> MeihuaChart:
        upper, lower, dong, ben_name, ben_hex = _time_gua(ctx)
        hu_hex = _hu_gua(ben_hex)
        hu_name = _hex_to_name(hu_hex) or ""
        bian_hex = _change_hex(ben_hex, dong)
        bian_name = _hex_to_name(bian_hex) or ""

        # 体用：动爻在上下卦，动者为用，静者为体
        if dong <= 3:
            ti, yong = lower, upper
        else:
            ti, yong = upper, lower
        ti_wx = {k: v for k, v in zip("乾兑离震巽坎艮坤", "金金火木木水土土")}[ti]
        yong_wx = {k: v for k, v in zip("乾兑离震巽坎艮坤", "金金火木木水土土")}[yong]
        if ti_wx == yong_wx:
            relation = "比和"
        elif SHENG.get(ti_wx) == yong_wx:
            relation = "体生用（泄）"
        elif SHENG.get(yong_wx) == ti_wx:
            relation = "用生体（吉）"
        elif KE.get(ti_wx) == yong_wx:
            relation = "体克用（劳）"
        else:
            relation = "用克体（凶）"

        chart = MeihuaChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [f"时间起卦：{upper}上{lower}下，第{dong}爻动；体{ti}用{yong}"],
            ben_gua=ben_name,
            hu_gua=hu_name,
            bian_gua=bian_name,
            ben_gua_hex=ben_hex,
            hu_gua_hex=hu_hex,
            bian_gua_hex=bian_hex,
            ti_gua=ti,
            yong_gua=yong,
            dong_yao=[dong],
            ti_yong_relation=relation,
        )
        return chart

    def natal(self, ctx) -> MeihuaChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> MeihuaChart:
        return self._build(ctx, "current")


# =============================================================================
# 3 八卦（京房八宫世应）
# =============================================================================


class BaguaPaipan(BasePaipan):
    """八卦排盘：时间起卦后归入京房八宫，定世应、六亲、纳甲。"""

    method = "八卦"

    def _build(self, ctx, chart_type: str) -> BaguaChart:
        upper, _, dong, ben_name, ben_hex = _time_gua(ctx)

        gong = ""
        gong_idx = 0
        for g, guas in BA_GONG.items():
            if ben_name in guas:
                gong = g
                gong_idx = guas.index(ben_name)
                break
        if not gong:
            gong = upper
            gong_idx = 0

        shi = SHI_YAO.get(gong_idx, 6)
        ying = (shi + 2) % 6 + 1 if shi <= 3 else (shi - 3)  # 应爻 = 世 +3 环
        # 应爻简化：应 = 世 + 3（1..6 环）
        ying = (shi + 2) % 6 + 1

        # 纳甲与六亲
        gong_wx = GONG_WUXING[gong]
        najia = NAJIA[gong]
        liuqin: list[str] = []
        for gz in najia:
            zhi = gz[1]
            zhi_wx = ZHI_WUXING[zhi]
            if zhi_wx == gong_wx:
                liuqin.append("兄弟")
            elif SHENG.get(zhi_wx) == gong_wx:
                liuqin.append("父母")
            elif SHENG.get(gong_wx) == zhi_wx:
                liuqin.append("子孙")
            elif KE.get(zhi_wx) == gong_wx:
                liuqin.append("官鬼")
            else:
                liuqin.append("妻财")

        bian_hex = _change_hex(ben_hex, dong)
        bian_name = _hex_to_name(bian_hex) or ""
        attributes = [f"第{dong}爻动"]
        if gong_idx == 6:
            attributes.append("游魂卦")
        elif gong_idx == 7:
            attributes.append("归魂卦")
        elif gong_idx == 0:
            attributes.append("八纯卦")

        chart = BaguaChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [f"时间起卦：{ben_name}，{gong}宫第{gong_idx + 1}卦"],
            ben_gua=ben_name,
            ben_gua_hex=ben_hex,
            bian_gua=bian_name,
            gua_gong=gong,
            shi_yao=shi,
            ying_yao=ying,
            liuqin=liuqin,
            najia=najia,
            attributes=attributes,
        )
        return chart

    def natal(self, ctx) -> BaguaChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> BaguaChart:
        return self._build(ctx, "current")


# =============================================================================
# 4 风水（玄空飞星）
# =============================================================================

PALACE9 = {5: "中宫", 6: "乾宫", 7: "兑宫", 8: "艮宫", 9: "离宫", 1: "坎宫", 2: "坤宫", 3: "震宫", 4: "巽宫"}
# 洛书飞布序：中(5)→乾(6)→兑(7)→艮(8)→离(9)→坎(1)→坤(2)→震(3)→巽(4)
_FEIXING_ORDER = [5, 6, 7, 8, 9, 1, 2, 3, 4]
_MONTH_STAR_BY_ZHI = {"子": 8, "午": 8, "卯": 8, "酉": 8,
                      "辰": 5, "戌": 5, "丑": 5, "未": 5,
                      "寅": 2, "申": 2, "巳": 2, "亥": 2}


def _yuan_yun(year: int) -> str:
    if year < 1864 or year > 2043:
        return "超出三运范围"
    yun = (year - 1864) // 20 + 1
    names = {1: "上元一运（一白坎）", 2: "上元二运（二黑坤）", 3: "上元三运（三碧震）",
             4: "中元四运（四绿巽）", 5: "中元五运（五黄中）", 6: "中元六运（六白乾）",
             7: "下元七运（七赤兑）", 8: "下元八运（八白艮）", 9: "下元九运（九紫离）"}
    return names[yun]


def _feixing(year_star: int) -> dict[int, int]:
    """年星入中后九宫飞布：宫 -> 星数。"""
    result: dict[int, int] = {}
    for i, palace in enumerate(_FEIXING_ORDER):
        star = (year_star - 1 + i) % 9 + 1
        result[palace] = star
    return result


class FengshuiPaipan(BasePaipan):
    """风水排盘：玄空飞星（三元九运 + 流年/流月飞星盘）。"""

    method = "风水"

    def _build(self, ctx, chart_type: str) -> FengshuiChart:
        year = ctx.solar_dt.year
        year_star = 9 - ((year - 2000) % 9)
        year_star = year_star if year_star > 0 else 9
        year_zhi = ctx.year_gz[1]
        first_month_star = _MONTH_STAR_BY_ZHI.get(year_zhi, 8)
        month_star = (first_month_star + ctx.solar_dt.month - 1 - 1) % 9 + 1

        year_map = _feixing(year_star)
        month_map = _feixing(month_star)
        fei_xing: dict[str, dict[str, int]] = {}
        for palace in range(1, 10):
            fei_xing[PALACE9[palace]] = {"year": year_map[palace], "month": month_map[palace]}

        chart = FengshuiChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [f"{year}年{year_star}白入中，{ctx.solar_dt.month}月{month_star}星入中"],
            yuan_yun=_yuan_yun(year),
            year_star=year_star,
            month_star=month_star,
            fei_xing=fei_xing,
            shan_xiang=None,
        )
        return chart

    def natal(self, ctx) -> FengshuiChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> FengshuiChart:
        return self._build(ctx, "current")
