"""GuFaQuant-Pro 8.0 —— 紫微斗数排盘器。

安星法依据（通行体系）：
- 命宫/身宫：起寅宫顺数生月，落宫起子时逆数（命宫）/顺数（身宫）生时。
- 十二宫：命宫起逆时针布 命/兄弟/夫妻/子女/财帛/疾厄/迁移/交友/官禄/田宅/福德/父母。
- 五行局：寅宫干 = 年干数×2+1 起，顺布宫干至命宫，命宫干支纳音定局
  （水二/木三/金四/土五/火六）。
- 紫微星：k=ceil(生日/局数)，差=k×局数-生日；差奇→k-差，差偶→k+差；
  起寅宫顺数该数为紫微，逆数为天府。
- 紫微星系（逆时针）：紫微→天机(1)→太阳(隔1)→武曲(1)→天同(1)→廉贞(隔2)。
- 天府星系（顺时针）：天府→太阴(1)→贪狼(1)→巨门(1)→天相(1)→天梁(1)→七杀(1)→破军(隔3)。
- 四化（年干）：甲廉破武阳、乙机梁紫阴、丙同机昌廉、丁阴同机巨、戊贪阴阳机、
  己武贪梁曲、庚阳武府同、辛巨阳曲昌、壬梁紫府武、癸破巨阴贪。
- 辅星：左辅(辰顺生月)、右弼(戌逆生月)、文曲(辰顺生时)、文昌(戌逆生时)、
  地劫(亥顺生时)、地空(亥逆生时)、魁钺、禄存羊陀、天马。

紫微斗数流派（三合/飞星/中州）在部分细节有差异，本实现采用通行三合派安星法。
"""

from __future__ import annotations

import math

from lunar_python import Solar

from gufa_paipan import BasePaipan, ZiweiChart

ZHI_ORDER = "子丑寅卯辰巳午未申酉戌亥"
GAN_ORDER = "甲乙丙丁戊己庚辛壬癸"

# 十二宫名（命宫起逆时针）
PALACE_NAMES = ["命宫", "兄弟", "夫妻", "子女", "财帛", "疾厄",
                "迁移", "交友", "官禄", "田宅", "福德", "父母"]

# 纳音五行 -> 局数
NAYIN_JU = {
    "海中金": 4, "炉中火": 6, "大林木": 3, "路旁土": 5, "剑锋金": 4,
    "山头火": 6, "涧下水": 2, "城头土": 5, "白蜡金": 4, "杨柳木": 3,
    "泉中水": 2, "屋上土": 5, "霹雳火": 6, "松柏木": 3, "长流水": 2,
    "沙中金": 4, "山下火": 6, "平地木": 3, "壁上土": 5, "金箔金": 4,
    "覆灯火": 6, "天河水": 2, "大驿土": 5, "钗钏金": 4, "桑柘木": 3,
    "大溪水": 2, "沙中土": 5, "天上火": 6, "石榴木": 3, "大海水": 2,
}
_NAYIN_PAIRS = [
    "海中金", "炉中火", "大林木", "路旁土", "剑锋金",
    "山头火", "涧下水", "城头土", "白蜡金", "杨柳木",
    "泉中水", "屋上土", "霹雳火", "松柏木", "长流水",
    "沙中金", "山下火", "平地木", "壁上土", "金箔金",
    "覆灯火", "天河水", "大驿土", "钗钏金", "桑柘木",
    "大溪水", "沙中土", "天上火", "石榴木", "大海水",
]


def _nayin(gan: str, zhi: str) -> str:
    """六十甲子纳音（60甲子序 s=(6g-5z) mod 60，每组两个甲子共30纳音）。"""
    g = GAN_ORDER.index(gan)
    z = ZHI_ORDER.index(zhi)
    s = (6 * g - 5 * z) % 60
    return _NAYIN_PAIRS[s // 2]


# 四化（年干 -> 禄权科忌）
SIHUA: dict[str, list[str]] = {
    "甲": ["廉贞", "破军", "武曲", "太阳"],
    "乙": ["天机", "天梁", "紫微", "太阴"],
    "丙": ["天同", "天机", "文昌", "廉贞"],
    "丁": ["太阴", "天同", "天机", "巨门"],
    "戊": ["贪狼", "太阴", "太阳", "天机"],
    "己": ["武曲", "贪狼", "天梁", "文曲"],
    "庚": ["太阳", "武曲", "天府", "天同"],
    "辛": ["巨门", "太阳", "文曲", "文昌"],
    "壬": ["天梁", "紫微", "天府", "武曲"],
    "癸": ["破军", "巨门", "太阴", "贪狼"],
}

# 魁钺（年干）
KUIYUE: dict[str, str] = {
    "甲": "丑未", "戊": "丑未", "庚": "丑未", "乙": "子申", "己": "子申",
    "丙": "亥酉", "丁": "亥酉", "辛": "午寅", "壬": "卯巳", "癸": "卯巳",
}
# 禄存（年干）
LUCUN: dict[str, str] = {
    "甲": "寅", "乙": "卯", "丙": "巳", "戊": "巳", "丁": "午",
    "己": "午", "庚": "申", "辛": "酉", "壬": "亥", "癸": "子",
}
# 天马（年支）
TIANMA: dict[str, str] = {"寅午戌": "申", "申子辰": "寅", "巳酉丑": "亥", "亥卯未": "巳"}


class ZiweiPaipan(BasePaipan):
    """紫微斗数排盘器（三合派安星法）。"""

    method = "紫微"

    def _build(self, ctx, chart_type: str) -> ZiweiChart:
        solar = Solar.fromYmdHms(
            ctx.solar_dt.year, ctx.solar_dt.month, ctx.solar_dt.day,
            ctx.solar_dt.hour, ctx.solar_dt.minute, ctx.solar_dt.second,
        )
        lunar = solar.getLunar()
        lmonth = lunar.getMonth()          # 农历月（含闰月标记）
        lday = lunar.getDay()              # 农历日
        year_gz = lunar.getYearInGanZhi()
        year_gan = year_gz[0]
        year_zhi = year_gz[1]
        shichen = ctx.shichen_name
        shi_idx = ZHI_ORDER.index(shichen)

        # 命宫/身宫：寅(2)起顺数月，落宫起子时逆/顺数时
        yin = 2
        month_pos = (yin + (lmonth - 1)) % 12
        ming = (month_pos - shi_idx) % 12
        shen = (month_pos + shi_idx) % 12

        # 十二宫（命宫起逆时针）
        palace_zhi: dict[str, int] = {}
        for i, name in enumerate(PALACE_NAMES):
            palace_zhi[name] = (ming - i) % 12

        # 宫干：寅宫干 = 年干数×2+1（甲=1），顺布
        yin_gan_idx = ((GAN_ORDER.index(year_gan) + 1) * 2 + 1) % 10 - 1
        palace_gan: dict[str, str] = {}
        for name in PALACE_NAMES:
            offset = (palace_zhi[name] - yin) % 12
            palace_gan[name] = GAN_ORDER[(yin_gan_idx + offset) % 10]

        # 五行局：命宫干支纳音
        ming_zhi = palace_zhi["命宫"]
        ming_gan = palace_gan["命宫"]
        ju = NAYIN_JU[_nayin(ming_gan, ZHI_ORDER[ming_zhi])]
        ju_name = {2: "水二局", 3: "木三局", 4: "金四局", 5: "土五局", 6: "火六局"}[ju]

        # 紫微星 / 天府星
        k = math.ceil(lday / ju)
        diff = k * ju - lday
        step = k - diff if diff % 2 == 1 else k + diff
        ziwei_zhi = (yin + step - 1) % 12
        tianfu_zhi = (yin - (step - 1)) % 12

        # 十四主星
        stars: dict[str, str] = {}  # 星 -> 地支
        stars["紫微"] = ZHI_ORDER[ziwei_zhi]
        # 紫微系（逆时针）：天机-1、太阳-3、武曲-4、天同-5、廉贞+4
        ziwei_seq = [("天机", -1), ("太阳", -3), ("武曲", -4), ("天同", -5), ("廉贞", 4)]
        for name, delta in ziwei_seq:
            stars[name] = ZHI_ORDER[(ziwei_zhi + delta) % 12]
        # 天府系（顺时针）：太阴+1、贪狼+2、巨门+3、天相+4、天梁+5、七杀+6、破军+10
        tianfu_seq = [("太阴", 1), ("贪狼", 2), ("巨门", 3), ("天相", 4),
                      ("天梁", 5), ("七杀", 6), ("破军", 10)]
        for name, delta in tianfu_seq:
            stars[name] = ZHI_ORDER[(tianfu_zhi + delta) % 12]

        # 四化
        four_hua: dict[str, str] = {}
        for hua, star in zip(("禄", "权", "科", "忌"), SIHUA[year_gan]):
            four_hua[hua] = star

        # 辅星
        zuo_fu = (4 + lmonth - 1) % 12          # 辰(4)顺生月
        you_bi = (10 - (lmonth - 1)) % 12       # 戌(10)逆生月
        wen_qu = (4 + shi_idx) % 12             # 辰顺生时
        wen_chang = (10 - shi_idx) % 12         # 戌逆生时
        di_jie = (11 + shi_idx) % 12            # 亥(11)顺生时
        di_kong = (11 - shi_idx) % 12           # 亥逆生时
        aux = {
            "左辅": ZHI_ORDER[zuo_fu], "右弼": ZHI_ORDER[you_bi],
            "文曲": ZHI_ORDER[wen_qu], "文昌": ZHI_ORDER[wen_chang],
            "地劫": ZHI_ORDER[di_jie], "地空": ZHI_ORDER[di_kong],
        }
        kuiyue = KUIYUE[year_gan]
        aux["天魁"] = kuiyue[0]
        aux["天钺"] = kuiyue[1]
        lucun = LUCUN[year_gan]
        aux["禄存"] = lucun
        aux["擎羊"] = ZHI_ORDER[(ZHI_ORDER.index(lucun) + 1) % 12]
        aux["陀罗"] = ZHI_ORDER[(ZHI_ORDER.index(lucun) - 1) % 12]
        for group, zhi in TIANMA.items():
            if year_zhi in group:
                aux["天马"] = zhi
                break

        # 组装十二宫星曜
        palaces: dict[str, list[str]] = {}
        for name in PALACE_NAMES:
            zhi = ZHI_ORDER[palace_zhi[name]]
            in_palace = [s for s, z in stars.items() if z == zhi]
            in_palace += [s for s, z in aux.items() if z == zhi]
            palaces[name] = in_palace

        # 长生十二神（简：按五行局长生位起，此处仅列名录标注）
        notes = [
            f"农历{lmonth}月{lday}日（{year_gz}年），{ju_name}",
            f"命宫{ZHI_ORDER[ming]}、身宫{ZHI_ORDER[shen]}",
            "安星法采用通行三合派（维基/《紫微斗数全书》体系）",
        ]

        chart = ZiweiChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + notes,
            ming_gong=ZHI_ORDER[ming],
            shen_gong=ZHI_ORDER[shen],
            wuxing_ju=ju_name,
            palaces=palaces,
            ziwei_palace=ZHI_ORDER[ziwei_zhi],
            tianfu_palace=ZHI_ORDER[tianfu_zhi],
            four_hua=four_hua,
            chang_sheng={},
        )
        return chart

    def natal(self, ctx) -> ZiweiChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> ZiweiChart:
        return self._build(ctx, "current")
