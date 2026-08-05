"""断卦公共层：五行生克、月令旺衰、十二长生、十神、打分约定。

所有术数模块输出统一的 MethodReading：
  - chart       结构化盘象（JSON 兼容）
  - rule_score  确定性规则分 [0,1]，0.5 为中性
  - rule_reading 按命中的规则生成的断语
  - rules_used  命中的规则名（审计）
打分约定集中在本模块，吉凶映射统一可审计；各术数在其上叠加自身断法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gufa_divination.calendar import GAN, ZHI, wuxing_of_gan, wuxing_of_zhi

# ---------- 五行 ----------

WUXING = "木火土金水"
WUXING_ORDER = {wx: i for i, wx in enumerate(WUXING)}  # 木0 火1 土2 金3 水4

SHENG_CYCLE = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE_CYCLE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}


def sheng(a: str, b: str) -> bool:
    """a 生 b。"""
    return SHENG_CYCLE[a] == b


def ke(a: str, b: str) -> bool:
    """a 克 b。"""
    return KE_CYCLE[a] == b


def wuxing_of(gan_or_zhi: str) -> str:
    """干支单字 → 五行（天干/地支通用）。"""
    if gan_or_zhi in GAN:
        return wuxing_of_gan(gan_or_zhi)
    return wuxing_of_zhi(gan_or_zhi)


def gan_yinyang(gan: str) -> str:
    """天干阴阳：甲丙戊庚壬为阳，乙丁己辛癸为阴。"""
    return "阳" if GAN.index(gan) % 2 == 0 else "阴"


def zhi_yinyang(zhi: str) -> str:
    """地支阴阳：子寅辰午申戌为阳，丑卯巳未酉亥为阴。"""
    return "阳" if ZHI.index(zhi) % 2 == 0 else "阴"


def yinyang_of(gan_or_zhi: str) -> str:
    if gan_or_zhi in GAN:
        return gan_yinyang(gan_or_zhi)
    return zhi_yinyang(gan_or_zhi)


# ---------- 月令旺衰（旺相休囚死） ----------

# 月支（按地支序）→ 各五行状态。4=旺 3=相 2=休 1=囚 0=死
_MONTH_POWER: dict[str, dict[str, int]] = {
    # 春（寅卯）
    "寅": {"木": 4, "火": 3, "水": 2, "金": 1, "土": 0},
    "卯": {"木": 4, "火": 3, "水": 2, "金": 1, "土": 0},
    # 夏（巳午）
    "巳": {"火": 4, "土": 3, "木": 2, "水": 1, "金": 0},
    "午": {"火": 4, "土": 3, "木": 2, "水": 1, "金": 0},
    # 秋（申酉）
    "申": {"金": 4, "水": 3, "土": 2, "火": 1, "木": 0},
    "酉": {"金": 4, "水": 3, "土": 2, "火": 1, "木": 0},
    # 冬（亥子）
    "亥": {"水": 4, "木": 3, "金": 2, "土": 1, "火": 0},
    "子": {"水": 4, "木": 3, "金": 2, "土": 1, "火": 0},
    # 四季月（辰戌丑未）：土旺
    "辰": {"土": 4, "金": 3, "火": 2, "木": 1, "水": 0},
    "戌": {"土": 4, "金": 3, "火": 2, "木": 1, "水": 0},
    "丑": {"土": 4, "金": 3, "火": 2, "木": 1, "水": 0},
    "未": {"土": 4, "金": 3, "火": 2, "木": 1, "水": 0},
}
POWER_NAMES = ("死", "囚", "休", "相", "旺")


def month_power(wuxing: str, month_zhi: str) -> int:
    """五行在月令的状态：4=旺 3=相 2=休 1=囚 0=死。"""
    return _MONTH_POWER.get(month_zhi, {}).get(wuxing, 2)


def month_power_name(wuxing: str, month_zhi: str) -> str:
    return POWER_NAMES[month_power(wuxing, month_zhi)]


# 旺相 → 偏强，休囚死 → 偏弱
_POWER_SCORE = {4: 0.78, 3: 0.66, 2: 0.5, 1: 0.34, 0: 0.22}


def power_score(wuxing: str, month_zhi: str) -> float:
    """月令旺衰 → [0,1] 强弱分（集中映射，可审计）。"""
    return _POWER_SCORE[month_power(wuxing, month_zhi)]


# ---------- 十二长生（旺衰表） ----------

CHANG_SHENG = (
    "长生", "沐浴", "冠带", "临官", "帝旺", "衰", "病", "死", "墓", "绝", "胎", "养",
)
_CHANG_SHENG_SCORE = {
    "长生": 0.66, "沐浴": 0.55, "冠带": 0.62, "临官": 0.74, "帝旺": 0.8,
    "衰": 0.44, "病": 0.36, "死": 0.28, "墓": 0.34, "绝": 0.22,
    "胎": 0.42, "养": 0.48,
}


def chang_sheng_score(state: str) -> float:
    """十二长生状态 → [0,1] 强弱分。"""
    return _CHANG_SHENG_SCORE.get(state, 0.5)


# ---------- 十神 ----------

def shi_shen(day_gan: str, other_gan: str) -> str:
    """以日干论十神。"""
    if day_gan == other_gan:
        return "比肩"
    d_wx, o_wx = wuxing_of_gan(day_gan), wuxing_of_gan(other_gan)
    same_yy = gan_yinyang(day_gan) == gan_yinyang(other_gan)
    if d_wx == o_wx:
        return "劫财" if not same_yy else "比肩"
    if sheng(d_wx, o_wx):  # 我生
        return "食神" if same_yy else "伤官"
    if ke(d_wx, o_wx):     # 我克
        return "偏财" if same_yy else "正财"
    if sheng(o_wx, d_wx):  # 生我
        return "偏印" if same_yy else "正印"
    if ke(o_wx, d_wx):     # 克我
        return "七杀" if same_yy else "正官"
    raise ValueError(f"无法判定十神: {day_gan} vs {other_gan}")


# ---------- 打分约定 ----------

SCORE_NEUTRAL = 0.5

# 五行关系 → 基准吉凶分（集中映射，可审计）。
# 比和=平 生我=生助(吉) 我生=泄气(稍凶) 我克=制财(平偏吉) 克我=受克(凶)
RELATION_SCORE = {
    "比和": 0.55,
    "生我": 0.7,
    "我生": 0.45,
    "我克": 0.55,
    "克我": 0.35,
}


def relation_score(relation: str) -> float:
    return RELATION_SCORE.get(relation, SCORE_NEUTRAL)


def wuxing_relation(main_wx: str, other_wx: str) -> str:
    """主方五行 vs 客方五行的关系（主方视角）。"""
    if main_wx == other_wx:
        return "比和"
    if sheng(main_wx, other_wx):
        return "我生"
    if ke(main_wx, other_wx):
        return "我克"
    if sheng(other_wx, main_wx):
        return "生我"
    return "克我"


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def combine_scores(parts: list[tuple[float, float]]) -> float:
    """加权合并规则分：parts = [(weight, score), ...]。"""
    total_w = sum(w for w, _ in parts)
    if total_w <= 0:
        return SCORE_NEUTRAL
    return clamp_score(sum(w * s for w, s in parts) / total_w)


def score_level(score: float) -> str:
    """规则分 → 档位（供断语与 AI 解读用）。"""
    if score >= 0.72:
        return "大吉"
    if score >= 0.58:
        return "吉"
    if score > 0.42:
        return "平"
    if score > 0.28:
        return "凶"
    return "大凶"


# ---------- MethodReading ----------

@dataclass
class MethodReading:
    """一项术数的完整解读输出。"""

    method: str                 # 术数名（与 STRATEGY_NAMES 对齐）
    school: str                 # 流派/算法标识
    chart: dict[str, Any]       # 结构化盘象
    rule_score: float           # [0,1] 规则分，0.5 中性
    rule_reading: str           # 规则断语
    rules_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "school": self.school,
            "chart": self.chart,
            "rule_score": round(float(self.rule_score), 6),
            "rule_level": score_level(self.rule_score),
            "rule_reading": self.rule_reading,
            "rules_used": list(self.rules_used),
            "errors": list(self.errors),
        }

    def with_score(self, score: float, reading: str, rules: list[str] | None = None) -> MethodReading:
        self.rule_score = clamp_score(score)
        self.rule_reading = reading
        if rules:
            self.rules_used.extend(rules)
        return self
