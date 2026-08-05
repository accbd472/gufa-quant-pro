"""GuFaQuant-Pro 8.0 —— 排盘信号映射（确定性简化断卦规则）。

把十项真实排盘结果映射为 [0,1] 看多置信度，供现有 score/风控管线使用。
映射规则为"古法断卦规则的简化数值化"，全部明确披露如下；真正的深度断卦
由 AI 断卦师读取完整盘面（PaipanResult.to_dict）完成。

简化断卦规则（每项均给出可复现的依据）：
- 奇门：值符星所临宫位的八门吉凶。开/休/生为三吉门 -> 0.62；景/杜为中平 -> 0.50；
  死/惊/伤为三凶门 -> 0.38。
- 六壬：初传与日干的五行生克。初传生干或比和 -> 0.62（气助日干）；初传克干 -> 0.38；
  日干克初传（财）-> 0.52；初传泄干（食伤）-> 0.45。
- 太乙：太乙落宫。一/三/六/八宫（吉）-> 0.58；五中宫 -> 0.50；其余 -> 0.42。
- 易经：与梅花同源（体用生克）：用生体/比和 0.65；体克用 0.52；体生用 0.45；用克体 0.35。
- 风水：流年入中星吉凶：一白/六白/八白（吉星）-> 0.62；五黄（凶）-> 0.35；其余 0.50。
- 八字：日主旺衰：旺/相 -> 0.56（得令主动）；休 -> 0.50；囚/死 -> 0.44。
- 梅花：体用生克（同易经）。
- 紫微：命宫主星吉凶：紫微/天府/太阳/太阴/天相/天梁/天同（吉星）-> 0.60；
  七杀/破军/廉贞/贪狼（动星）-> 0.48；巨门 -> 0.42；无主星 -> 0.50。
- 八卦：卦宫五行与日干五行：日干生宫（泄）-> 0.45；宫生日干 -> 0.60；比和 -> 0.55；
  宫克日干 -> 0.38；日干克宫（财）-> 0.52。
- 四柱：神煞吉凶：有禄神/天乙贵人 -> 0.58；羊刃/桃花 -> 0.42；否则 0.50。

所有映射为公开规则；预测准确性不保证（README 免责声明）。
"""

from __future__ import annotations

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
GAN_WUXING = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
              "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
ZHI_WUXING = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
              "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}

JIMEN = {"开门": 0.62, "休门": 0.62, "生门": 0.62, "景门": 0.50, "杜门": 0.50,
         "死门": 0.38, "惊门": 0.38, "伤门": 0.38}


def _qimen(chart: dict) -> float:
    """值符星所临宫位的门吉凶。"""
    zhifu = chart.get("zhifu", "")
    star = zhifu.replace("(寄坤)", "")
    for entry in chart.get("jiu_gong", {}).values():
        if entry.get("star") == star:
            return JIMEN.get(entry.get("door", ""), 0.50)
    return 0.50


def _liuren(chart: dict) -> float:
    """初传与日干五行生克。"""
    trans = chart.get("three_transmissions", [])
    if not trans or not trans[0]:
        return 0.50
    chu = trans[0][-1] if len(trans[0]) > 1 else trans[0][0]
    if chu not in ZHI_WUXING:
        return 0.50
    # 日干从 ganzhi 中取（日柱）——四课基于日干，此处以 chart.ganzhi 日柱为准
    gz = chart.get("ganzhi", "")
    day_gan = ""
    parts = gz.split(" ")
    for part in parts:
        if "日" in part:
            day_gan = part[0]
            break
    if not day_gan or day_gan not in GAN_WUXING:
        return 0.50
    dg_wx = GAN_WUXING[day_gan]
    chu_wx = ZHI_WUXING[chu]
    if chu_wx == dg_wx:
        return 0.62
    if SHENG.get(chu_wx) == dg_wx:
        return 0.62          # 初传生干
    if KE.get(chu_wx) == dg_wx:
        return 0.38          # 初传克干（官鬼）
    if KE.get(dg_wx) == chu_wx:
        return 0.52          # 日干克初传（妻财）
    return 0.45              # 初传泄干（食伤）


def _taiyi(chart: dict) -> float:
    gong = chart.get("taiyi_gong", "")
    for num, name in {1: "一宫", 3: "三宫", 6: "六宫", 8: "八宫"}.items():
        if name in gong:
            return 0.58
    if "五宫" in gong:
        return 0.50
    return 0.42


def _tiyong(chart: dict) -> float:
    rel = chart.get("ti_yong_relation", "")
    if "用生体" in rel or "比和" in rel:
        return 0.65
    if "体克用" in rel:
        return 0.52
    if "体生用" in rel:
        return 0.45
    return 0.35


def _fengshui(chart: dict) -> float:
    star = chart.get("year_star", 5)
    if star in (1, 6, 8):
        return 0.62
    if star == 5:
        return 0.35
    return 0.50


def _bazi(chart: dict) -> float:
    strength = chart.get("strength", "")
    if strength in ("旺", "相"):
        return 0.56
    if strength == "休":
        return 0.50
    return 0.44


def _ziwei(chart: dict) -> float:
    good = {"紫微", "天府", "太阳", "太阴", "天相", "天梁", "天同"}
    active = {"七杀", "破军", "廉贞", "贪狼"}
    stars = chart.get("palaces", {}).get("命宫", [])
    mains = [s for s in stars if s in good or s in active or s == "巨门"]
    if not mains:
        return 0.50
    main = mains[0]
    if main in good:
        return 0.60
    if main in active:
        return 0.48
    return 0.42


def _bagua(chart: dict) -> float:
    gong = chart.get("gua_gong", "")
    gong_wx = {"乾": "金", "兑": "金", "离": "火", "震": "木",
               "巽": "木", "坎": "水", "艮": "土", "坤": "土"}.get(gong, "")
    gz = chart.get("ganzhi", "")
    day_gan = ""
    for part in gz.split(" "):
        if "日" in part:
            day_gan = part[0]
            break
    if not gong_wx or not day_gan:
        return 0.50
    dg_wx = GAN_WUXING.get(day_gan, "")
    if not dg_wx:
        return 0.50
    if gong_wx == dg_wx:
        return 0.55
    if SHENG.get(gong_wx) == dg_wx:
        return 0.60
    if SHENG.get(dg_wx) == gong_wx:
        return 0.45
    if KE.get(gong_wx) == dg_wx:
        return 0.38
    return 0.52


def _sizhu(chart: dict) -> float:
    sha = chart.get("shen_sha", {})
    if "禄神" in sha or "天乙贵人" in sha:
        return 0.58
    if "羊刃" in sha or "桃花" in sha:
        return 0.42
    return 0.50


SIGNAL_FUNCS = {
    "奇门": _qimen, "六壬": _liuren, "太乙": _taiyi, "易经": _tiyong,
    "风水": _fengshui, "八字": _bazi, "梅花": _tiyong, "紫微": _ziwei,
    "八卦": _bagua, "四柱": _sizhu,
}


def paipan_signals(result_dict: dict) -> dict[str, float]:
    """把 PaipanResult.to_dict() 映射为 {古法名: 置信度}。"""
    current = result_dict.get("current", {})
    signals: dict[str, float] = {}
    for method, func in SIGNAL_FUNCS.items():
        chart = current.get(method, {})
        if chart and "error" not in chart:
            signals[method] = round(float(func(chart)), 4)
        else:
            signals[method] = 0.50  # 缺盘时中性，避免扭曲
    return signals
