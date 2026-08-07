"""GuFaQuant-Pro 8.0 —— 排盘信号映射（确定性简化断卦规则）。

把十项真实排盘结果映射为 [0,1] 看多置信度，供现有 score/风控管线使用。
映射规则为"古法断卦规则的简化数值化"，全部明确披露如下；真正的深度断卦
由 AI 断卦师读取完整盘面（PaipanResult.to_dict）完成。

每个方法同时产出「断卦要点」（verdict）：人话结论 + 依据，供审计、报告
与 AI 断卦师 prompt 使用，与分数同源、完全可复现。

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
JIMEN_LABEL = {"开门": "吉门", "休门": "吉门", "生门": "吉门", "景门": "中平", "杜门": "中平",
               "死门": "凶门", "惊门": "凶门", "伤门": "凶门"}


def _day_gan_from_ganzhi(ganzhi: str) -> str:
    """从 '丙午 乙未 辛亥 丁酉' 形式取日干（含'日'标注，或取第三柱）。"""
    parts = ganzhi.split()
    for part in parts:
        if "日" in part:
            return part[0]
    if len(parts) >= 3:  # 年 月 日 时
        return parts[2][0]
    return ""


def _relation(wx_a: str, wx_b: str) -> tuple[str, str]:
    """五行 wx_a 相对 wx_b（日干）的生克关系 -> (关系词, 吉凶标签)。"""
    if wx_a == wx_b:
        return "比和", "吉"
    if SHENG.get(wx_a) == wx_b:
        return "生", "吉"
    if KE.get(wx_a) == wx_b:
        return "克", "凶"
    if KE.get(wx_b) == wx_a:
        return "被日干克（财）", "中"
    return "泄日干（食伤）", "平"


def _qimen(chart: dict) -> tuple[float, str]:
    """值符星所临宫位的门吉凶。"""
    zhifu = chart.get("zhifu", "")
    star = zhifu.replace("(寄坤)", "")
    for entry in chart.get("jiu_gong", {}).values():
        if entry.get("star") == star:
            door = entry.get("door", "")
            score = JIMEN.get(door, 0.50)
            label = JIMEN_LABEL.get(door, "中平")
            return score, f"值符{star}临{door}（{label}）"
    return 0.50, f"值符{star or '?'}未定位宫门，取中性"


def _liuren(chart: dict) -> tuple[float, str]:
    """初传与日干五行生克。"""
    trans = chart.get("three_transmissions", [])
    if not trans or not trans[0]:
        return 0.50, "无初传，取中性"
    chu = trans[0][-1] if len(trans[0]) > 1 else trans[0][0]
    if chu not in ZHI_WUXING:
        return 0.50, f"初传{chu}无法定五行，取中性"
    day_gan = _day_gan_from_ganzhi(chart.get("ganzhi", ""))
    if not day_gan or day_gan not in GAN_WUXING:
        return 0.50, f"初传{chu}，日干缺失取中性"
    dg_wx = GAN_WUXING[day_gan]
    chu_wx = ZHI_WUXING[chu]
    rel, label = _relation(chu_wx, dg_wx)
    if chu_wx == dg_wx:
        return 0.62, f"初传{chu}（{chu_wx}）与日干{day_gan}（{dg_wx}）比和，气助日干"
    if SHENG.get(chu_wx) == dg_wx:
        return 0.62, f"初传{chu}（{chu_wx}）生日干{day_gan}（{dg_wx}），气助日干"
    if KE.get(chu_wx) == dg_wx:
        return 0.38, f"初传{chu}（{chu_wx}）克日干{day_gan}（{dg_wx}），官鬼压身"
    if KE.get(dg_wx) == chu_wx:
        return 0.52, f"日干{day_gan}（{dg_wx}）克初传{chu}（{chu_wx}），为妻财"
    return 0.45, f"初传{chu}（{chu_wx}）泄日干{day_gan}（{dg_wx}），食伤泄气"


def _taiyi(chart: dict) -> tuple[float, str]:
    gong = chart.get("taiyi_gong", "")
    for num, name in {1: "一宫", 3: "三宫", 6: "六宫", 8: "八宫"}.items():
        if name in gong:
            return 0.58, f"太乙落{gong}（吉宫）"
    if "五宫" in gong:
        return 0.50, f"太乙落{gong}（中宫）"
    return 0.42, f"太乙落{gong}（非吉宫）"


def _tiyong(chart: dict) -> tuple[float, str]:
    rel = chart.get("ti_yong_relation", "")
    if "用生体" in rel or "比和" in rel:
        return 0.65, f"体用：{rel}（吉）"
    if "体克用" in rel:
        return 0.52, f"体用：{rel}（中平）"
    if "体生用" in rel:
        return 0.45, f"体用：{rel}（泄气）"
    return 0.35, f"体用：{rel or '用克体'}（凶）"


def _fengshui(chart: dict) -> tuple[float, str]:
    star = chart.get("year_star", 5)
    if star in (1, 6, 8):
        return 0.62, f"流年入中星 {star}（吉星）"
    if star == 5:
        return 0.35, f"流年入中星 {star}（五黄凶星）"
    return 0.50, f"流年入中星 {star}（平星）"


def _bazi(chart: dict) -> tuple[float, str]:
    strength = chart.get("strength", "")
    day_master = chart.get("day_master", "")
    if strength in ("旺", "相"):
        return 0.56, f"日主{day_master}旺衰{strength}（得令主动）"
    if strength == "休":
        return 0.50, f"日主{day_master}旺衰{strength}（中平）"
    return 0.44, f"日主{day_master}旺衰{strength or '囚死'}（失令）"


def _ziwei(chart: dict) -> tuple[float, str]:
    good = {"紫微", "天府", "太阳", "太阴", "天相", "天梁", "天同"}
    active = {"七杀", "破军", "廉贞", "贪狼"}
    stars = chart.get("palaces", {}).get("命宫", [])
    mains = [s for s in stars if s in good or s in active or s == "巨门"]
    if not mains:
        return 0.50, "命宫无主星（取中性）"
    main = mains[0]
    if main in good:
        return 0.60, f"命宫主星{main}（吉星）"
    if main in active:
        return 0.48, f"命宫主星{main}（动星，波动大）"
    return 0.42, f"命宫主星{main}（暗曜）"


def _bagua(chart: dict) -> tuple[float, str]:
    gong = chart.get("gua_gong", "")
    gong_wx = {"乾": "金", "兑": "金", "离": "火", "震": "木",
               "巽": "木", "坎": "水", "艮": "土", "坤": "土"}.get(gong, "")
    day_gan = _day_gan_from_ganzhi(chart.get("ganzhi", ""))
    if not gong_wx or not day_gan:
        return 0.50, f"卦宫{gong or '?'}或日干缺失，取中性"
    dg_wx = GAN_WUXING.get(day_gan, "")
    if not dg_wx:
        return 0.50, f"日干{day_gan}五行缺失，取中性"
    if gong_wx == dg_wx:
        return 0.55, f"卦宫{gong}（{gong_wx}）与日干{day_gan}（{dg_wx}）比和"
    if SHENG.get(gong_wx) == dg_wx:
        return 0.60, f"卦宫{gong}（{gong_wx}）生日干{day_gan}（{dg_wx}）"
    if SHENG.get(dg_wx) == gong_wx:
        return 0.45, f"日干{day_gan}（{dg_wx}）生卦宫{gong}（{gong_wx}），泄气"
    if KE.get(gong_wx) == dg_wx:
        return 0.38, f"卦宫{gong}（{gong_wx}）克日干{day_gan}（{dg_wx}）"
    return 0.52, f"日干{day_gan}（{dg_wx}）克卦宫{gong}（{gong_wx}），为财"


def _sizhu(chart: dict) -> tuple[float, str]:
    sha = chart.get("shen_sha", {})
    hit = [k for k, v in sha.items() if v]
    if "禄神" in sha or "天乙贵人" in sha:
        return 0.58, f"神煞见吉：{hit or '禄神/天乙贵人'}"
    if "羊刃" in sha or "桃花" in sha:
        return 0.42, f"神煞见凶：{hit}"
    return 0.50, f"神煞无吉凶显象（{hit or '无'}）"


RULE_FUNCS = {
    "奇门": _qimen, "六壬": _liuren, "太乙": _taiyi, "易经": _tiyong,
    "风水": _fengshui, "八字": _bazi, "梅花": _tiyong, "紫微": _ziwei,
    "八卦": _bagua, "四柱": _sizhu,
}


def _current_charts(result_dict: dict) -> dict[str, dict]:
    return result_dict.get("current", {})


def _natal_charts(result_dict: dict) -> dict[str, dict]:
    return result_dict.get("natal", {})


def paipan_signals(result_dict: dict, natal_weight: float = 0.30) -> dict[str, float]:
    """把 PaipanResult.to_dict() 映射为 {古法名: 看多置信度}。

    综合时空盘（current，当前时辰）与本命盘（natal，标的上市时间）：
    current 主导（1 - natal_weight），natal 提供标的个性因子——不同上市时间
    的标的在同一天会得到不同分数，选股才能区分标的，而不是所有币同分。
    natal 缺失（未提供上市时间）时退化为纯 current 信号，保持兼容。
    """
    signals: dict[str, float] = {}
    current = _current_charts(result_dict)
    natal = _natal_charts(result_dict)
    natal_usable = bool(natal) and any(
        chart and "error" not in chart for chart in natal.values()
    )
    for method, func in RULE_FUNCS.items():
        chart = current.get(method, {})
        if chart and "error" not in chart:
            base = float(func(chart)[0])
        else:
            base = 0.50  # 缺盘时中性，避免扭曲
        if natal_usable:
            nchart = natal.get(method, {})
            if nchart and "error" not in nchart:
                natal_sig = float(func(nchart)[0])
            else:
                natal_sig = 0.50
            signals[method] = round(base * (1 - natal_weight) + natal_sig * natal_weight, 4)
        else:
            signals[method] = round(base, 4)
    return signals


def paipan_verdicts(result_dict: dict) -> dict[str, str]:
    """把 PaipanResult.to_dict() 映射为 {古法名: 断卦要点}（人话 + 依据）。"""
    verdicts: dict[str, str] = {}
    for method, func in RULE_FUNCS.items():
        chart = _current_charts(result_dict).get(method, {})
        if chart and "error" not in chart:
            verdicts[method] = func(chart)[1]
        else:
            reason = chart.get("error", "缺盘") if chart else "缺盘"
            verdicts[method] = f"排盘不可用（{reason}），取中性"
    return verdicts
