"""GuFaQuant-Pro 8.0 —— 十项古法排盘数据契约。

本模块只定义排盘结果的纯数据结构（dataclass）与统一接口，不含算法实现。
算法在后续步骤（CalendarService / 各排盘器）中实现。

设计原则：
1. 每项古法输出一个 PaipanChart 子类；字段全部为可 JSON 序列化的基本类型。
2. 共有元信息（时间、干支、节气、旬空等）收敛到 PaipanBase。
3. 本命盘（标的上市时间）与时空盘（当前时辰）使用同一契约，通过 chart_type 区分。
4. 排盘是"过程真实"的保证边界：字段名与取值必须符合对应古法的通行规则，
   不做预测性修饰；解读交给 AI 断卦师（见 AIAdvisor）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# =============================================================================
# 共有基类
# =============================================================================


@dataclass
class PaipanBase:
    """所有古法排盘共有的元信息。"""

    method: str = ""                # 古法名称：奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱
    chart_type: str = ""            # natal=本命盘（标的上市时间），current=时空盘（当前时辰）
    solar_time: str = ""            # 排盘所用公历时间 ISO-8601（真太阳时修正后）
    lunar_text: str = ""            # 农历文本，如"二〇二六年六月廿三 酉时"
    ganzhi: str = ""                # 干支纪时，如"丙午年 乙未月 辛亥日 丁酉时"
    jieqi: str | None = None     # 所在节气，如"立秋前"
    xun_kong: str | None = None  # 日旬空亡，如"寅卯"
    notes: list[str] = field(default_factory=list)  # 备注：真太阳时修正、上市时间来源等

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value for key, value in self.__dict__.items()
        }


# =============================================================================
# 1 奇门遁甲（时家转盘奇门）
# =============================================================================


@dataclass
class QimenChart(PaipanBase):
    """时家奇门排盘：阴阳遁局数 + 九宫排布（八门/九星/八神/三奇六仪）。"""

    yuan: str = ""                  # 上元/中元/下元
    dun: str = ""                   # 阳遁/阴遁
    ju: int = 0                     # 局数 1..9
    zhifu: str = ""                 # 值符星
    zhishi: str = ""                # 值使门
    jiu_gong: dict[str, dict[str, str]] = field(default_factory=dict)
    # jiu_gong: 宫位 -> {door: 八门, star: 九星, god: 八神, gan: 三奇六仪}
    # 如 "坎一宫": {"door": "休门", "star": "天蓬", "god": "值符", "gan": "戊"}
    timing_gan_palace: str | None = None   # 时干落宫
    day_gan_palace: str | None = None      # 日干落宫


# =============================================================================
# 2 大六壬
# =============================================================================


@dataclass
class LiurenChart(PaipanBase):
    """大六壬排盘：月将加时起天地盘，取四课三传。"""

    yuejiang: str = ""              # 月将，如"午将"
    tianpan: dict[str, str] = field(default_factory=dict)
    # tianpan: 地盘支 -> 天盘支（月将加时后的旋转结果）
    four_lessons: list[str] = field(default_factory=list)   # 四课，如 ["子巳", "巳戌", ...]
    three_transmissions: list[str] = field(default_factory=list)  # 三传（初传/中传/末传）
    ke_break: str = ""              # 贼克/遥克/昴星/别责/八专/伏吟/返吟 等课体
    jiang_shen: str | None = None  # 年命所临（本命盘用）


# =============================================================================
# 3 太乙神数
# =============================================================================


@dataclass
class TaiyiChart(PaipanBase):
    """太乙神数：太乙积年 + 十六神布局 + 三基五福。"""

    taiyi_year: int = 0             # 太乙积年
    taiyi_gong: str = ""            # 太乙落宫（岁计）
    yue_gong: str = ""              # 月计落宫
    ri_gong: str = ""               # 日计落宫
    sixteen_gods: dict[str, str] = field(default_factory=dict)  # 十六神 -> 所落宫/位
    sanji: dict[str, str] = field(default_factory=dict)   # 三基：天基/地基/人基
    wufu: dict[str, str] = field(default_factory=dict)    # 五福
    wenyun: str | None = None    # 文运


# =============================================================================
# 4 易经（周易卦象：时间起卦 + 卦辞爻辞）
# =============================================================================


@dataclass
class YijingChart(PaipanBase):
    """易经起卦：以年月日时数成卦，取本卦/变卦与动爻。"""

    ben_gua: str = ""               # 本卦名，如"乾为天"
    ben_gua_hex: str = ""           # 本卦六爻阴阳串，如"111111"
    bian_gua: str = ""              # 变卦名
    bian_gua_hex: str = ""          # 变卦六爻串
    dong_yao: list[int] = field(default_factory=list)  # 动爻位置（1 为初爻）
    gua_ci: str | None = None    # 本卦卦辞（内置词典）
    yao_ci: str | None = None    # 动爻爻辞（内置词典，多动爻时取本卦辞）


# =============================================================================
# 5 风水（玄空飞星：以运/年/月入中宫排九星）
# =============================================================================


@dataclass
class FengshuiChart(PaipanBase):
    """玄空飞星盘：下元九运 + 流年/流月飞星入中。"""

    yuan_yun: str = ""              # 三元九运，如"下元九运"
    year_star: int = 0              # 流年飞星（入中宫之星）
    month_star: int = 0             # 流月飞星（入中宫之星）
    fei_xing: dict[str, dict[str, int]] = field(default_factory=dict)
    # fei_xing: 宫位 -> {"year": 年星, "month": 月星}
    shan_xiang: str | None = None  # 坐向（本命盘可用，时空盘置空）


# =============================================================================
# 6 八字（子平：十神/旺衰/用神）
# =============================================================================


@dataclass
class BaziChart(PaipanBase):
    """四柱八字排盘：侧重十神关系、日主旺衰与用神取用。"""

    pillars: dict[str, str] = field(default_factory=dict)  # 年柱/月柱/日柱/时柱 -> 干支
    day_master: str = ""            # 日主天干
    day_master_wuxing: str = ""     # 日主五行
    wuxing: dict[str, str] = field(default_factory=dict)  # 各柱五行（干支各一）
    hidden_stems: dict[str, list[str]] = field(default_factory=dict)  # 地支藏干
    ten_gods: dict[str, str] = field(default_factory=dict)  # 各柱十神（以日主论）
    nayin: dict[str, str] = field(default_factory=dict)   # 四柱纳音
    taiyuan: str | None = None   # 胎元
    minggong: str | None = None  # 命宫
    shengong: str | None = None  # 身宫
    strength: str = ""              # 日主旺衰：旺/相/休/囚/死（依月令）
    yong_shen: str | None = None  # 用神（简取：扶抑/调候）
    qi_yun: str | None = None    # 起运说明
    da_yun: list[str] = field(default_factory=list)  # 大运干支序列


# =============================================================================
# 7 梅花易数（时间起卦 + 体用）
# =============================================================================


@dataclass
class MeihuaChart(PaipanBase):
    """梅花易数：年月日时起卦，分体用断事。"""

    ben_gua: str = ""               # 本卦
    hu_gua: str = ""                # 互卦
    bian_gua: str = ""              # 变卦
    ben_gua_hex: str = ""
    hu_gua_hex: str = ""
    bian_gua_hex: str = ""
    ti_gua: str = ""                # 体卦
    yong_gua: str = ""              # 用卦
    dong_yao: list[int] = field(default_factory=list)
    ti_yong_relation: str = ""      # 体用生克：体克用/用生体/比和/体生用/用克体


# =============================================================================
# 8 紫微斗数（十二宫 + 星曜 + 四化）
# =============================================================================


@dataclass
class ZiweiChart(PaipanBase):
    """紫微斗数排盘：安命身宫、定十二宫、布主星与四化。"""

    ming_gong: str = ""             # 命宫地支
    shen_gong: str = ""             # 身宫地支
    wuxing_ju: str = ""             # 五行局，如"火六局"
    palaces: dict[str, list[str]] = field(default_factory=dict)
    # palaces: 十二宫名(命宫/兄弟/夫妻/子女/财帛/疾厄/迁移/仆役/官禄/田宅/福德/父母)
    #          -> 该宫主星列表（含辅星），如 ["紫微", "天机"]
    ziwei_palace: str | None = None  # 紫微星所在宫
    tianfu_palace: str | None = None  # 天府星所在宫
    four_hua: dict[str, str] = field(default_factory=dict)  # 四化：禄/权/科/忌 -> 星曜
    chang_sheng: dict[str, str] = field(default_factory=dict)  # 长生十二神落宫


# =============================================================================
# 9 八卦（京房八宫：纳甲装卦 + 世应六亲）
# =============================================================================


@dataclass
class BaguaChart(PaipanBase):
    """八卦排盘：以时间纳甲起卦，布世应、六亲与卦宫。"""

    ben_gua: str = ""               # 卦名
    ben_gua_hex: str = ""           # 六爻串
    bian_gua: str = ""              # 变卦（有动爻时）
    gua_gong: str = ""              # 所属八宫
    shi_yao: int = 0                # 世爻位（1..6，0 表示游魂/归魂特殊标记另见 attributes）
    ying_yao: int = 0               # 应爻位
    liuqin: list[str] = field(default_factory=list)  # 六亲（自下而上）
    najia: list[str] = field(default_factory=list)   # 纳甲干支（自下而上）
    attributes: list[str] = field(default_factory=list)  # 六神/游魂/归魂等


# =============================================================================
# 10 四柱（命理结构：四柱宫位 + 神煞 + 纳音）
# =============================================================================


@dataclass
class SizhuChart(PaipanBase):
    """四柱排盘：侧重宫位六亲（年柱祖上/月柱父母/日柱自身/时柱子女）与神煞。"""

    pillars: dict[str, str] = field(default_factory=dict)
    palaces: dict[str, str] = field(default_factory=dict)  # 宫位 -> 六亲角色
    shen_sha: dict[str, list[str]] = field(default_factory=dict)  # 神煞名 -> 所在柱
    nayin: dict[str, str] = field(default_factory=dict)
    five_hub: str | None = None  # 五行生克总结（干支关系）
    day_master: str = ""


# =============================================================================
# 统一容器
# =============================================================================

CHART_CLASSES: dict[str, Any] = {
    "奇门": QimenChart,
    "六壬": LiurenChart,
    "太乙": TaiyiChart,
    "易经": YijingChart,
    "风水": FengshuiChart,
    "八字": BaziChart,
    "梅花": MeihuaChart,
    "紫微": ZiweiChart,
    "八卦": BaguaChart,
    "四柱": SizhuChart,
}


@dataclass
class PaipanResult:
    """单个标的在某一时刻的完整排盘结果。"""

    symbol: str
    time_label: str                 # 排盘时间标签
    natal: dict[str, dict[str, Any]] = field(default_factory=dict)     # 本命盘：method -> chart dict
    current: dict[str, dict[str, Any]] = field(default_factory=dict)   # 时空盘：method -> chart dict
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "time_label": self.time_label,
            "natal": self.natal,
            "current": self.current,
            "diagnostics": self.diagnostics,
        }


def chart_to_dict(chart: PaipanBase) -> dict[str, Any]:
    """把任意排盘 dataclass 转 dict（兼容字段缺失）。"""
    return chart.to_dict()


# =============================================================================
# 排盘器统一接口（算法在后续步骤实现）
# =============================================================================


class BasePaipan:
    """排盘器基类：子类实现 natal() 与 current()。

    入参均为 PaipanTime 时间上下文（见 gufa_calendar），返回对应古法的
    PaipanChart 子类实例。natal 使用标的上市时间，current 使用当前时辰。
    """

    method = ""

    def natal(self, ctx) -> PaipanBase:
        raise NotImplementedError

    def current(self, ctx) -> PaipanBase:
        raise NotImplementedError


class PaipanService:
    """排盘服务：组装某标的的本命盘（上市时间）与时空盘（当前时辰）。

    第 5-11 步逐一实现十项排盘器并 register；未注册的项不会出现在结果中。
    """

    def __init__(self, config):
        from gufa_calendar import CalendarService  # 延迟导入避免循环依赖

        self.config = config
        self.calendar = CalendarService(config)
        self._panzers: dict[str, BasePaipan] = {}

    def register(self, panzer: BasePaipan) -> None:
        if not panzer.method or panzer.method not in CHART_CLASSES:
            raise ValueError(f"非法排盘器 method: {panzer.method!r}")
        self._panzers[panzer.method] = panzer

    @property
    def methods(self) -> list[str]:
        return list(self._panzers.keys())

    def paipan(
        self,
        symbol: str,
        now_dt=None,
        listing_ts=None,
    ) -> PaipanResult:
        """为 symbol 生成完整排盘结果。

        now_dt: 时空盘基准时间（默认当前 UTC 时间，内部按东八区真太阳时修正）
        listing_ts: 标的上市时间（ohlcv 最早 K 线时间戳或 ISO 字符串）；
                    与配置 listing_time_source 共同决定本命盘基准。
        """
        from datetime import datetime, timezone

        base = now_dt or datetime.now(timezone.utc)
        current_ctx = self.calendar.context(base, note="时空盘：当前时辰")
        result = PaipanResult(
            symbol=symbol,
            time_label=current_ctx.solar_iso,
            diagnostics={"correction_minutes": round(current_ctx.correction_minutes, 2)},
        )

        for method, panzer in self._panzers.items():
            try:
                result.current[method] = panzer.current(current_ctx).to_dict()
            except Exception as exc:  # noqa: BLE001 - 单法失败不影响整体  # 单法失败不影响整体（记录后继续）
                result.current[method] = {
                    "method": method,
                    "chart_type": "current",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                result.diagnostics[f"error.current.{method}"] = str(exc)

        listing = self.calendar.listing_time(symbol, listing_ts)
        if listing is not None:
            natal_ctx = self.calendar.context(listing, note="本命盘：标的上市时间")
            for method, panzer in self._panzers.items():
                try:
                    result.natal[method] = panzer.natal(natal_ctx).to_dict()
                except Exception as exc:  # noqa: BLE001 - 单法失败不影响整体
                    result.natal[method] = {
                        "method": method,
                        "chart_type": "natal",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    result.diagnostics[f"error.natal.{method}"] = str(exc)
        else:
            result.diagnostics["natal_missing"] = symbol

        return result
