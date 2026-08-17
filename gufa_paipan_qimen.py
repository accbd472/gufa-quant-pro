"""GuFaQuant-Pro 8.0 —— 奇门遁甲排盘器（时家转盘奇门）。

规则依据（通行《烟波钓叟歌》/《奇门遁甲统宗》体系）：
- 阴阳遁：冬至→夏至 阳遁；夏至→冬至 阴遁（按节气时刻分界）。
- 定局：节气三元定局表（上/中/下元），元由日支定（子午卯酉上元、寅申巳亥中元、辰戌丑未下元）。
- 地盘三奇六仪：戊己庚辛壬癸丁丙乙，阳遁顺布、阴遁逆布，自局数宫起。
- 值符星/值使门：旬首（六甲）所遁之仪落宫对应星与门。
- 天盘：值符星加时干宫；九星/八门/八神/天盘干按转盘环顺布（八神阴遁逆布）。
- 中五宫：天禽寄坤二；转盘环不含中宫。

实现为纯规则排盘；正确性以手工推演与权威案例校验（见 memory/2026-08-05.md）。
"""

from __future__ import annotations

from datetime import timezone

from gufa_paipan import BasePaipan, QimenChart

UTC = timezone.utc

# 九宫与环序（洛书后天八卦转盘环，跳中五）
PALACE_NAMES: dict[int, str] = {
    1: "坎一宫", 2: "坤二宫", 3: "震三宫", 4: "巽四宫", 5: "中五宫",
    6: "乾六宫", 7: "兑七宫", 8: "艮八宫", 9: "离九宫",
}
RING: list[int] = [1, 8, 3, 4, 9, 2, 7, 6]  # 转盘环（坎艮震巽离坤兑乾）

# 九星 / 八门 原位
STAR_BY_PALACE: dict[int, str] = {
    1: "天蓬", 2: "天芮", 3: "天冲", 4: "天辅", 5: "天禽",
    6: "天心", 7: "天柱", 8: "天任", 9: "天英",
}
DOOR_BY_PALACE: dict[int, str] = {
    1: "休门", 2: "死门", 3: "伤门", 4: "杜门",
    6: "开门", 7: "惊门", 8: "生门", 9: "景门",
}
GODS: list[str] = ["值符", "腾蛇", "太阴", "六合", "白虎", "玄武", "九地", "九天"]
DOOR_RING: list[str] = ["休门", "生门", "伤门", "杜门", "景门", "死门", "惊门", "开门"]

# 三奇六仪与六甲遁
YIQI: list[str] = ["戊", "己", "庚", "辛", "壬", "癸", "丁", "丙", "乙"]
JIA_DUN: dict[str, str] = {
    "甲子": "戊", "甲戌": "己", "甲申": "庚", "甲午": "辛", "甲辰": "壬", "甲寅": "癸",
}

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"

# 节气顺序（冬至起）
JIEQI_ORDER: list[str] = [
    "冬至", "小寒", "大寒", "立春", "雨水", "惊蛰",
    "春分", "清明", "谷雨", "立夏", "小满", "芒种",
    "夏至", "小暑", "大暑", "立秋", "处暑", "白露",
    "秋分", "寒露", "霜降", "立冬", "小雪", "大雪",
]

# 三元定局表：节气 -> (上元, 中元, 下元)
YANG_JU: dict[str, tuple] = {
    "冬至": (1, 7, 4), "小寒": (2, 8, 5), "大寒": (3, 9, 6),
    "立春": (8, 5, 2), "雨水": (9, 6, 3), "惊蛰": (1, 7, 4),
    "春分": (3, 9, 6), "清明": (4, 1, 7), "谷雨": (5, 2, 8),
    "立夏": (4, 1, 7), "小满": (5, 2, 8), "芒种": (6, 3, 9),
}
YIN_JU: dict[str, tuple] = {
    "夏至": (9, 3, 6), "小暑": (8, 2, 5), "大暑": (7, 1, 4),
    "立秋": (2, 5, 8), "处暑": (1, 4, 7), "白露": (9, 3, 6),
    "秋分": (7, 1, 4), "寒露": (6, 9, 3), "霜降": (5, 8, 2),
    "立冬": (6, 9, 3), "小雪": (5, 8, 2), "大雪": (4, 7, 1),
}

YUAN_BY_ZHI = {
    "子": "上元", "午": "上元", "卯": "上元", "酉": "上元",
    "寅": "中元", "申": "中元", "巳": "中元", "亥": "中元",
    "辰": "下元", "戌": "下元", "丑": "下元", "未": "下元",
}


def _xun_info(gz: str):
    """时/日干支 -> (旬首, 距旬首偏移 0..9)。如 丁酉 -> ('甲午', 3)。"""
    g = GAN.index(gz[0])
    z = ZHI.index(gz[1])
    d = (z - g) % 12
    xun_zhi = {0: "子", 10: "戌", 8: "申", 6: "午", 4: "辰", 2: "寅"}[d]
    offset = (z - ZHI.index(xun_zhi)) % 12
    return "甲" + xun_zhi, offset


def _current_jieqi(ctx) -> str:
    """当前所处节气（jieqi 为空时由 next_jieqi 反推上一节气）。"""
    if ctx.jieqi:
        return ctx.jieqi
    nxt = ctx.next_jieqi
    if nxt and nxt in JIEQI_ORDER:
        idx = JIEQI_ORDER.index(nxt)
        return JIEQI_ORDER[(idx - 1) % 24]
    # 兜底：按日期粗判（仅作最后手段）
    month, day = ctx.solar_dt.month, ctx.solar_dt.day
    if (month == 1) or (month == 2 and day < 4) or (month == 12 and day >= 22):
        return "小寒" if month == 1 else ("大寒" if month == 2 else "冬至")
    return "立春"


class QimenPaipan(BasePaipan):
    """时家转盘奇门排盘器。"""

    method = "奇门"

    def _build(self, ctx, chart_type: str) -> QimenChart:
        jieqi = _current_jieqi(ctx)
        dun = "阳遁" if jieqi in YANG_JU else "阴遁"
        table = YANG_JU if dun == "阳遁" else YIN_JU
        ju_table = table.get(jieqi)
        if ju_table is None:
            # 节气不在表内（极端情况）：按阴阳遁默认 1 局并备注
            ju = 1
            note = f"节气 {jieqi} 不在定局表，按 {dun}1局处理"
        else:
            # 拆补法：元由符头（日所在旬的旬首）日支定，非当日日支
            xun_shou, _ = _xun_info(ctx.day_gz)
            fu_tou_zhi = xun_shou[1]
            yuan = YUAN_BY_ZHI.get(fu_tou_zhi, "中元")
            ju = ju_table[{"上元": 0, "中元": 1, "下元": 2}[yuan]]
            note = f"{dun}{ju}局（{jieqi}{yuan}，符头{xun_shou}，日支{ctx.day_zhi}）"

        # 地盘三奇六仪
        dipan: dict[str, int] = {}  # 干 -> 宫
        for i, gan in enumerate(YIQI):
            if dun == "阳遁":
                dipan[gan] = (ju - 1 + i) % 9 + 1
            else:
                dipan[gan] = (ju - 1 - i) % 9 + 1
        dipan_inv: dict[int, str] = {v: k for k, v in dipan.items()}

        # 值符星 / 值使门
        xun, offset = _xun_info(ctx.time_gz)
        value_palace = dipan[JIA_DUN[xun]]  # 旬首遁仪落宫
        zhifu_star = STAR_BY_PALACE[value_palace]
        zhishi_door = DOOR_BY_PALACE.get(value_palace, "")
        if value_palace == 5:
            # 天禽寄坤二
            zhifu_star = "天禽(寄坤)"
            value_ring_idx = RING.index(2)
        else:
            value_ring_idx = RING.index(value_palace)

        # 时干落宫（地盘时干宫 = 值符所加）
        # 古法：时干为甲时遁于旬首六仪，取旬首遁仪落宫
        shi_gan = ctx.time_gz[0]
        if shi_gan == "甲":
            shi_gan = JIA_DUN[xun]
        target = dipan[shi_gan]
        target_ring_idx = RING.index(target) if target in RING else 0

        # 天盘九星：值符星落时干宫，其余按星序环顺布
        star_seq = [STAR_BY_PALACE[p] for p in RING]
        tianpan_star: dict[int, str] = {}
        for i in range(8):
            palace = RING[(target_ring_idx + i) % 8]
            tianpan_star[palace] = star_seq[(value_ring_idx + i) % 8]

        # 天盘干：地盘干整体环向旋转（值符原宫干 -> 时干宫）
        shift = (target_ring_idx - value_ring_idx) % 8
        tianpan_gan: dict[int, str] = {}
        for i, palace in enumerate(RING):
            src = RING[(i - shift) % 8]
            tianpan_gan[palace] = dipan_inv.get(src, "")
        tianpan_gan[5] = dipan_inv.get(5, "")  # 中宫干不变

        # 八门：值使门自旬首宫走 offset 步（阳顺阴逆），其余门顺布
        if dun == "阳遁":
            zhishi_target_idx = (value_ring_idx + offset) % 8
        else:
            zhishi_target_idx = (value_ring_idx - offset) % 8
        zhishi_door_idx = DOOR_RING.index(zhishi_door) if zhishi_door in DOOR_RING else 0
        tianpan_door: dict[int, str] = {}
        for i in range(8):
            palace = RING[(zhishi_target_idx + i) % 8]
            tianpan_door[palace] = DOOR_RING[(zhishi_door_idx + i) % 8]

        # 八神：值符神落时干宫，阳遁顺布 / 阴遁逆布
        tianpan_god: dict[int, str] = {}
        for i, god in enumerate(GODS):
            if dun == "阳遁":
                palace = RING[(target_ring_idx + i) % 8]
            else:
                palace = RING[(target_ring_idx - i) % 8]
            tianpan_god[palace] = god

        # 组装九宫
        jiu_gong: dict[str, dict[str, str]] = {}
        for palace in range(1, 10):
            entry: dict[str, str] = {}
            entry["gan"] = dipan_inv.get(palace, "")
            if tianpan_gan.get(palace):
                entry["tian_gan"] = tianpan_gan[palace]
            if palace in tianpan_star:
                entry["star"] = tianpan_star[palace]
            if palace in tianpan_door:
                entry["door"] = tianpan_door[palace]
            if palace in tianpan_god:
                entry["god"] = tianpan_god[palace]
            jiu_gong[PALACE_NAMES[palace]] = entry

        # 天盘时干 / 日干落宫（旋转后）
        def _tian_gan_palace(gan: str) -> str | None:
            if gan not in dipan:
                return None
            palace = dipan[gan]
            if palace == 5:
                return PALACE_NAMES[5]
            idx = RING.index(palace)
            moved = RING[(idx + shift) % 8]
            return PALACE_NAMES[moved]

        chart = QimenChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + [note, f"旬首{xun}（遁{JIA_DUN[xun]}），值符{zhifu_star}，值使{zhishi_door}"],
            yuan=yuan,
            dun=dun,
            ju=ju,
            zhifu=zhifu_star,
            zhishi=zhishi_door,
            jiu_gong=jiu_gong,
            timing_gan_palace=_tian_gan_palace(shi_gan),
            day_gan_palace=_tian_gan_palace(ctx.day_gan),
        )
        return chart

    def natal(self, ctx) -> QimenChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> QimenChart:
        return self._build(ctx, "current")
