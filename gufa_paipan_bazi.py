"""GuFaQuant-Pro 8.0 —— 八字（子平）与四柱排盘器。

数据基础：lunar_python 的 EightChar（四柱干支、藏干、十神、纳音、胎元、
命宫、身宫、大运）。本模块在其上补充：
- 日主旺衰（按月令旺相休囚死）
- 用神（简取扶抑法：身强喜克泄耗，身弱喜生扶）
- 神煞（天乙贵人/文昌/驿马/桃花/华盖/禄神/羊刃，以日干查）
- 四柱宫位六亲（年祖上、月父母、日自身、时子女）
- 大运（标的无性别，按"顺行"规则取：阳年男顺 / 阴年女顺）

规则为通行子平法；神煞采用常用简化表。
"""

from __future__ import annotations

from lunar_python import Solar

from gufa_paipan import BasePaipan, BaziChart, SizhuChart

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"

# 五行
GAN_WUXING: dict[str, str] = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
}
ZHI_WUXING: dict[str, str] = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
    "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水",
}
SHENG: dict[str, str] = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE: dict[str, str] = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}

# 十神（以日干论：同我比劫、生我印、我生食伤、克我官杀、我克财）
YIN_YANG: dict[str, str] = {
    "甲": "阳", "乙": "阴", "丙": "阳", "丁": "阴", "戊": "阳",
    "己": "阴", "庚": "阳", "辛": "阴", "壬": "阳", "癸": "阴",
}

# 旺相休囚死：月支五行 -> {日主五行: 状态}
_ORDER5 = ["木", "火", "土", "金", "水"]


def _wang_shuai(day_gan: str, month_zhi: str) -> str:
    """日主按月令的旺相休囚死。"""
    dm = GAN_WUXING[day_gan]
    mz = ZHI_WUXING[month_zhi]
    if dm == mz:
        return "旺"
    if SHENG.get(mz) == dm:      # 月令生我
        return "相"
    if SHENG.get(dm) == mz:      # 我生月令
        return "休"
    if KE.get(dm) == mz:         # 我克月令
        return "囚"
    return "死"                   # 月令克我


def _yong_shen(day_gan: str, month_zhi: str) -> str:
    """简取用神（扶抑法）。返回用神十神类别。"""
    state = _wang_shuai(day_gan, month_zhi)
    if state in {"旺", "相"}:
        # 身强：喜克（官杀）、泄（食伤）
        return "食伤/官杀（泄克）"
    # 身弱：喜生（印）、帮（比劫）
    return "印/比劫（生扶）"


# 神煞表（以日干查）
TIANYI: dict[str, str] = {"甲": "丑未", "戊": "丑未", "庚": "丑未", "乙": "子申", "己": "子申",
                          "丙": "亥酉", "丁": "亥酉", "壬": "卯巳", "癸": "卯巳", "辛": "寅午"}
WENCHANG: dict[str, str] = {"甲": "巳", "乙": "午", "丙": "申", "戊": "申", "丁": "酉",
                            "己": "酉", "庚": "亥", "辛": "子", "壬": "寅", "癸": "卯"}
LUSHEN: dict[str, str] = {"甲": "寅", "乙": "卯", "丙": "巳", "戊": "巳", "丁": "午",
                          "己": "午", "庚": "申", "辛": "酉", "壬": "亥", "癸": "子"}
YANGREN: dict[str, str] = {"甲": "卯", "丙": "午", "戊": "午", "庚": "酉", "壬": "子"}
_SANHE = {"寅午戌": "申", "申子辰": "寅", "巳酉丑": "亥", "亥卯未": "巳"}
_TAOHUA = {"寅午戌": "卯", "申子辰": "酉", "巳酉丑": "午", "亥卯未": "子"}
_HUAGAI = {"寅午戌": "戌", "申子辰": "辰", "巳酉丑": "丑", "亥卯未": "未"}


def _sanhe_group(zhi: str) -> str | None:
    for group, _ in _SANHE.items():
        if zhi in group:
            return group
    return None


def _shen_sha(day_gan: str, pillars: dict[str, str]) -> dict[str, list[str]]:
    """常用神煞（以日干查，四柱地支/天干中出现者）。"""
    result: dict[str, list[str]] = {}

    # 天乙贵人（看地支与天干）
    for pillar_name in ("年", "月", "日", "时"):
        gz = pillars[pillar_name]
        for ch in gz:
            if TIANYI.get(day_gan, "") and ch in TIANYI[day_gan]:
                result.setdefault("天乙贵人", []).append(pillar_name)
                break
    # 文昌（看日支以外的地支？看四柱地支）
    for pillar_name, gz in pillars.items():
        if WENCHANG.get(day_gan) and gz[1] == WENCHANG[day_gan]:
            result.setdefault("文昌", []).append(pillar_name)
    # 禄神
    for pillar_name, gz in pillars.items():
        if LUSHEN.get(day_gan) and gz[1] == LUSHEN[day_gan]:
            result.setdefault("禄神", []).append(pillar_name)
    # 羊刃
    for pillar_name, gz in pillars.items():
        if YANGREN.get(day_gan) and gz[1] == YANGREN[day_gan]:
            result.setdefault("羊刃", []).append(pillar_name)
    # 驿马/桃花/华盖（按日支三合局）
    group = _sanhe_group(pillars["日"][1])
    if group:
        for name, table in (("驿马", _SANHE), ("桃花", _TAOHUA), ("华盖", _HUAGAI)):
            target = table[group]
            for pillar_name, gz in pillars.items():
                if gz[1] == target:
                    result.setdefault(name, []).append(pillar_name)
    return result


# 宫位六亲
PALACE_LIUQIN = {"年": "祖上/父母宫", "月": "父母/兄弟宫", "日": "自身/夫妻宫", "时": "子女宫"}


class BaziPaipan(BasePaipan):
    """八字排盘器（子平：十神/旺衰/用神/大运）。"""

    method = "八字"

    def _build(self, ctx, chart_type: str) -> BaziChart:
        solar = Solar.fromYmdHms(
            ctx.solar_dt.year, ctx.solar_dt.month, ctx.solar_dt.day,
            ctx.solar_dt.hour, ctx.solar_dt.minute, ctx.solar_dt.second,
        )
        ec = solar.getLunar().getEightChar()

        pillars = {"年": ec.getYear(), "月": ec.getMonth(), "日": ec.getDay(), "时": ec.getTime()}
        day_master = ec.getDayGan()
        month_zhi = ec.getMonth()[1]

        wuxing = {k: GAN_WUXING[v[0]] + ZHI_WUXING[v[1]] for k, v in pillars.items()}
        hidden = {
            k: (ec.getYearHideGan() if k == "年" else
                ec.getMonthHideGan() if k == "月" else
                ec.getDayHideGan() if k == "日" else ec.getTimeHideGan())
            for k in pillars
        }
        ten_gods = {
            "年": ec.getYearShiShenGan(), "月": ec.getMonthShiShenGan(),
            "日": ec.getDayShiShenZhi()[0] if ec.getDayShiShenZhi() else "",
            "时": ec.getTimeShiShenGan(),
        }
        nayin = {
            "年": ec.getYearNaYin(), "月": ec.getMonthNaYin(),
            "日": ec.getDayNaYin(), "时": ec.getTimeNaYin(),
        }

        # 大运：标的无性别，按顺行取（阳年男顺 / 阴年女顺）
        year_gan_yang = YIN_YANG[ec.getYear()[0]] == "阳"
        yun = ec.getYun(1 if year_gan_yang else 0)
        da_yun: list[str] = []
        for dy in yun.getDaYun():
            gz = dy.getGanZhi()
            if gz:
                da_yun.append(gz)
        qi_yun = f"出生后{max(yun.getStartYear(), 0)}年{max(yun.getStartMonth(), 0)}个月{max(yun.getStartDay(), 0)}天起运"
        if not year_gan_yang:
            qi_yun += "（阴年取女顺行）"
        else:
            qi_yun += "（阳年取男顺行）"

        notes = [
            "大运按顺行规则（标的无性别：阳年男顺/阴年女顺）",
            f"日主{day_master}（{GAN_WUXING[day_master]}），月令{month_zhi}（{ZHI_WUXING[month_zhi]}）",
        ]

        chart = BaziChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + notes,
            pillars=pillars,
            day_master=day_master,
            day_master_wuxing=GAN_WUXING[day_master],
            wuxing=wuxing,
            hidden_stems=hidden,
            ten_gods=ten_gods,
            nayin=nayin,
            taiyuan=ec.getTaiYuan(),
            minggong=ec.getMingGong(),
            shengong=ec.getShenGong(),
            strength=_wang_shuai(day_master, month_zhi),
            yong_shen=_yong_shen(day_master, month_zhi),
            qi_yun=qi_yun,
            da_yun=da_yun[:12],
        )
        return chart

    def natal(self, ctx) -> BaziChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> BaziChart:
        return self._build(ctx, "current")


class SizhuPaipan(BasePaipan):
    """四柱排盘器（宫位六亲 + 神煞 + 纳音）。"""

    method = "四柱"

    def _build(self, ctx, chart_type: str) -> SizhuChart:
        solar = Solar.fromYmdHms(
            ctx.solar_dt.year, ctx.solar_dt.month, ctx.solar_dt.day,
            ctx.solar_dt.hour, ctx.solar_dt.minute, ctx.solar_dt.second,
        )
        ec = solar.getLunar().getEightChar()
        pillars = {"年": ec.getYear(), "月": ec.getMonth(), "日": ec.getDay(), "时": ec.getTime()}
        nayin = {
            "年": ec.getYearNaYin(), "月": ec.getMonthNaYin(),
            "日": ec.getDayNaYin(), "时": ec.getTimeNaYin(),
        }
        day_master = ec.getDayGan()
        shen_sha = _shen_sha(day_master, pillars)

        # 五行生克总结：四柱天干地支五行分布
        counts: dict[str, int] = {}
        for gz in pillars.values():
            for ch in (gz[0], gz[1]):
                wx = GAN_WUXING.get(ch, ZHI_WUXING.get(ch, ""))
                if wx:
                    counts[wx] = counts.get(wx, 0) + 1
        dominant = max(counts, key=counts.get) if counts else ""
        five_hub = f"五行分布：{'、'.join(f'{k}{v}' for k, v in sorted(counts.items()))}；{dominant}偏旺" if dominant else ""

        chart = SizhuChart(
            method=self.method,
            chart_type=chart_type,
            solar_time=ctx.solar_iso,
            lunar_text=ctx.lunar_text,
            ganzhi=ctx.ganzhi_full,
            jieqi=ctx.jieqi,
            xun_kong=ctx.xun_kong,
            notes=ctx.notes + ["四柱侧重宫位六亲与神煞，与'八字'盘互补"],
            pillars=pillars,
            palaces={k: PALACE_LIUQIN[k] for k in pillars},
            shen_sha=shen_sha,
            nayin=nayin,
            five_hub=five_hub,
            day_master=day_master,
        )
        return chart

    def natal(self, ctx) -> SizhuChart:
        return self._build(ctx, "natal")

    def current(self, ctx) -> SizhuChart:
        return self._build(ctx, "current")
