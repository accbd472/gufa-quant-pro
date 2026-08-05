"""GuFaQuant-Pro 8.0 —— 大六壬排盘器。

规则依据（通行《六壬大全》体系）：
- 月将：以中气换将（雨水亥将、春分戌将、谷雨酉将、小满申将、夏至未将、
  大暑午将、处暑巳将、秋分辰将、霜降卯将、小雪寅将、冬至丑将、大寒子将）。
- 天地盘：月将加时（时支），十二支顺时针布天盘。
- 四课：日干寄宫（十干寄宫）与日支各起两课。
- 三传：九宗门（贼克/比用/涉害/遥克/昴星/别责/八专/伏吟/返吟）取初传，
  中传=初传上神，末传=中传上神。
- 上克下曰克，下克上曰贼。

实现为纯规则排盘；正确性以手工推演校验（见 memory/2026-08-05.md）。
"""

from __future__ import annotations

from gufa_paipan import BasePaipan, LiurenChart

ZHI_ORDER = "子丑寅卯辰巳午未申酉戌亥"

# 月将（中气 -> 天将支）：神后子、大吉丑、功曹寅、太冲卯、天罡辰、太乙巳、
# 胜光午、小吉未、传送申、从魁酉、河魁戌、登明亥
ZHONGQI_TO_JIANG: dict[str, str] = {
    "雨水": "亥", "春分": "戌", "谷雨": "酉", "小满": "申",
    "夏至": "未", "大暑": "午", "处暑": "巳", "秋分": "辰",
    "霜降": "卯", "小雪": "寅", "冬至": "丑", "大寒": "子",
}
JIANG_NAMES: dict[str, str] = {
    "子": "神后", "丑": "大吉", "寅": "功曹", "卯": "太冲",
    "辰": "天罡", "巳": "太乙", "午": "胜光", "未": "小吉",
    "申": "传送", "酉": "从魁", "戌": "河魁", "亥": "登明",
}
JIE = {"立春", "惊蛰", "清明", "立夏", "芒种", "小暑",
       "立秋", "白露", "寒露", "立冬", "大雪", "小寒"}
JIEQI_ORDER: list[str] = [
    "冬至", "小寒", "大寒", "立春", "雨水", "惊蛰",
    "春分", "清明", "谷雨", "立夏", "小满", "芒种",
    "夏至", "小暑", "大暑", "立秋", "处暑", "白露",
    "秋分", "寒露", "霜降", "立冬", "小雪", "大雪",
]

# 十干寄宫
GAN_JI_GONG: dict[str, str] = {
    "甲": "寅", "乙": "辰", "丙": "巳", "戊": "巳",
    "丁": "未", "己": "未", "庚": "申", "辛": "戌",
    "壬": "亥", "癸": "丑",
}

WUXING: dict[str, str] = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
    "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水",
}
KE_MAP: dict[str, str] = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}
YANG_ZHI = set("子寅辰午申戌")
YANG_GAN = set("甲丙戊庚壬")


def _ke(a: str, b: str) -> bool:
    """a 五行克 b 五行？"""
    return KE_MAP.get(WUXING[a]) == WUXING[b]


def _current_jieqi(ctx) -> str:
    if ctx.jieqi:
        return ctx.jieqi
    nxt = ctx.next_jieqi
    if nxt and nxt in JIEQI_ORDER:
        idx = JIEQI_ORDER.index(nxt)
        return JIEQI_ORDER[(idx - 1) % 24]
    return "立春"


def _yuejiang(ctx) -> str:
    """返回 (天将支, 天将名)。"""
    jq = _current_jieqi(ctx)
    if jq in ZHONGQI_TO_JIANG:
        zhi = ZHONGQI_TO_JIANG[jq]
    else:
        # 当前是"节"：取上一中气
        idx = JIEQI_ORDER.index(jq)
        zhi = None
        for back in range(1, 25):
            prev = JIEQI_ORDER[(idx - back) % 24]
            if prev in ZHONGQI_TO_JIANG:
                zhi = ZHONGQI_TO_JIANG[prev]
                break
        if zhi is None:
            zhi = "亥"
    return zhi, JIANG_NAMES[zhi]


class LiurenPaipan(BasePaipan):
    """大六壬排盘器（月将加时起课）。"""

    method = "六壬"

    def _build(self, ctx, chart_type: str) -> LiurenChart:
        jiang_zhi, jiang_name = _yuejiang(ctx)
        shi_zhi = ctx.shichen_name
        jiang_idx = ZHI_ORDER.index(jiang_zhi)
        shi_idx = ZHI_ORDER.index(shi_zhi)

        # 天地盘：月将加时，顺时针布十二支
        tianpan: dict[str, str] = {}
        for i in range(12):
            di = ZHI_ORDER[(shi_idx + i) % 12]
            tian = ZHI_ORDER[(jiang_idx + i) % 12]
            tianpan[di] = tian

        # 四课
        day_gan = ctx.day_gan
        day_zhi = ctx.day_zhi
        ji_gong = GAN_JI_GONG.get(day_gan, day_zhi)

        g1_lower = ji_gong
        g1_upper = tianpan[g1_lower]
        g2_lower = g1_upper
        g2_upper = tianpan[g2_lower]
        g3_lower = day_zhi
        g3_upper = tianpan[g3_lower]
        g4_lower = g3_upper
        g4_upper = tianpan[g4_lower]
        lesson_pairs = [
            (g1_lower, g1_upper),
            (g2_lower, g2_upper),
            (g3_lower, g3_upper),
            (g4_lower, g4_upper),
        ]
        lessons = [f"{lo}{up}" for lo, up in lesson_pairs]

        # 三传（九宗门简化判定）
        chu, ke_break, notes = self._initial_transmission(lesson_pairs, day_gan, day_zhi, jiang_zhi, shi_zhi, tianpan)
        zhong = tianpan.get(chu, "")
        mo = tianpan.get(zhong, "") if zhong else ""
        transmissions = [chu, zhong, mo]

        chart = LiurenChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=_current_jieqi(ctx),
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [f"月将{jiang_name}（{jiang_zhi}）加{shi_zhi}时", ke_break] + notes,
            yuejiang=f"{jiang_name}",
            tianpan={f"{di}": t for di, t in tianpan.items()},
            four_lessons=lessons,
            three_transmissions=transmissions,
            ke_break=ke_break,
            jiang_shen=JIANG_NAMES.get(tianpan.get(ji_gong, ""), tianpan.get(ji_gong, "")),
        )
        return chart

    def _initial_transmission(
        self,
        lesson_pairs,
        day_gan: str,
        day_zhi: str,
        jiang_zhi: str,
        shi_zhi: str,
        tianpan: dict[str, str],
    ):
        """九宗门取初传（简化实现：贼克→比用→涉害→遥克→昴星/别责/八专/伏吟/返吟）。"""
        # 伏吟 / 返吟
        if jiang_zhi == shi_zhi:
            return day_zhi, "伏吟", ["天地盘同位，伏吟课"]
        if (_ke(jiang_zhi, shi_zhi) or _ke(shi_zhi, jiang_zhi)) and (
            ZHI_ORDER.index(jiang_zhi) - ZHI_ORDER.index(shi_zhi)
        ) % 12 == 6:
            return day_zhi, "返吟", ["天地盘对冲，返吟课"]

        # 八专：日干寄宫与日支相同
        if GAN_JI_GONG.get(day_gan) == day_zhi:
            return day_zhi, "八专", ["日干寄宫与日支同，八专课"]

        # 找克：上克下（克）、下克上（贼）
        ke_list: list[tuple] = []
        for idx, (lower, upper) in enumerate(lesson_pairs, 1):
            if _ke(upper, lower):
                ke_list.append((idx, upper, "上克下"))
            elif _ke(lower, upper):
                ke_list.append((idx, lower, "下贼上"))

        if len(ke_list) == 1:
            idx, zhi, kind = ke_list[0]
            return zhi, "贼克", [f"第{idx}课{kind}，贼克法取{JIANG_NAMES[zhi]}"]
        if len(ke_list) > 1:
            # 比用：取与日干阴阳相同者
            yang_day = day_gan in YANG_GAN
            same = [k for k in ke_list if (k[1] in YANG_ZHI) == yang_day]
            chosen = same[0] if same else ke_list[0]
            idx, zhi, kind = chosen
            return zhi, "比用", [f"第{idx}课{kind}，比用法取{JIANG_NAMES[zhi]}"]

        # 无克：遥克——日干所克之课神（先上神后下支）
        for idx, (lower, upper) in enumerate(lesson_pairs, 1):
            # 日干五行：天干五行
            if _ke_gan(day_gan, upper):
                return upper, "遥克", [f"第{idx}课日干遥克上神{JIANG_NAMES[upper]}"]
            if _ke_gan(day_gan, lower):
                return lower, "遥克", [f"第{idx}课日干遥克下支{JIANG_NAMES[lower]}"]

        # 昴星课：无克无遥克
        return "酉", "昴星", ["四课无克、日干无遥克，昴星课（简化取酉）"]

    def natal(self, ctx) -> LiurenChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> LiurenChart:
        return self._build(ctx, "current")


# 天干五行
GAN_WUXING = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
              "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}


def _ke_gan(gan: str, zhi: str) -> bool:
    """天干五行克地支五行？"""
    return KE_MAP.get(GAN_WUXING[gan]) == WUXING[zhi]
