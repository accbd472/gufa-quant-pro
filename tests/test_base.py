"""断卦公共层单元测试。"""
import pytest

from gufa_divination.base import (
    MethodReading,
    chang_sheng_score,
    clamp_score,
    combine_scores,
    gan_yinyang,
    ke,
    month_power,
    month_power_name,
    power_score,
    relation_score,
    score_level,
    sheng,
    shi_shen,
    wuxing_relation,
)


def test_sheng_cycle():
    assert sheng("木", "火") and sheng("火", "土") and sheng("土", "金")
    assert sheng("金", "水") and sheng("水", "木")
    assert not sheng("木", "金")


def test_ke_cycle():
    assert ke("木", "土") and ke("土", "水") and ke("水", "火")
    assert ke("火", "金") and ke("金", "木")
    assert not ke("木", "火")


def test_month_power():
    assert month_power("木", "寅") == 4        # 春木旺
    assert month_power("金", "寅") == 1        # 春金囚
    assert month_power("火", "午") == 4        # 夏火旺
    assert month_power("水", "午") == 1
    assert month_power("金", "酉") == 4        # 秋金旺
    assert month_power("水", "酉") == 3        # 秋水相
    assert month_power("水", "子") == 4        # 冬水旺
    assert month_power("土", "辰") == 4        # 四季月土旺
    assert month_power_name("木", "寅") == "旺"
    assert month_power_name("金", "寅") == "囚"


def test_power_score_bounds():
    assert power_score("木", "寅") > 0.7
    assert power_score("金", "寅") < 0.4
    assert 0.0 <= power_score("火", "子") <= 1.0


def test_gan_yinyang():
    assert gan_yinyang("甲") == "阳" and gan_yinyang("乙") == "阴"
    assert gan_yinyang("庚") == "阳" and gan_yinyang("辛") == "阴"


def test_shi_shen_jiagong():
    """甲日干对十天干。"""
    expected = {
        "甲": "比肩", "乙": "劫财", "丙": "食神", "丁": "伤官", "戊": "偏财",
        "己": "正财", "庚": "七杀", "辛": "正官", "壬": "偏印", "癸": "正印",
    }
    for gan, ss in expected.items():
        assert shi_shen("甲", gan) == ss, (gan, shi_shen("甲", gan))


def test_shi_shen_guixin():
    """癸日干：水。庚=生我(印,异阴→正印?) 验一组。"""
    assert shi_shen("癸", "壬") == "劫财"     # 同水异阴阳
    assert shi_shen("癸", "甲") == "伤官"     # 水生木，异阴阳
    assert shi_shen("癸", "己") == "七杀"     # 土克水，己阴 → 异阴阳? 癸阴己阴同 → 正官
    assert shi_shen("癸", "戊") == "正官"     # 戊阳 vs 癸阴 异阴阳 → 正官


def test_chang_sheng_score():
    assert chang_sheng_score("帝旺") > 0.7
    assert chang_sheng_score("绝") < 0.3
    assert chang_sheng_score("沐浴") == 0.55


def test_scoring():
    assert clamp_score(1.2) == 1.0
    assert clamp_score(-0.3) == 0.0
    assert combine_scores([(1.0, 0.8), (1.0, 0.4)]) == pytest.approx(0.6)
    assert combine_scores([]) == 0.5
    assert combine_scores([(2.0, 0.9), (1.0, 0.3)]) == pytest.approx(0.7)
    assert score_level(0.8) == "大吉"
    assert score_level(0.6) == "吉"
    assert score_level(0.5) == "平"
    assert score_level(0.35) == "凶"
    assert score_level(0.2) == "大凶"


def test_relation_score():
    assert relation_score("生我") > relation_score("克我")
    assert relation_score("比和") == 0.55


def test_wuxing_relation():
    assert wuxing_relation("木", "木") == "比和"
    assert wuxing_relation("木", "火") == "我生"
    assert wuxing_relation("木", "土") == "我克"
    assert wuxing_relation("木", "水") == "生我"
    assert wuxing_relation("木", "金") == "克我"


def test_method_reading():
    mr = MethodReading(
        method="梅花", school="邵康节时间起卦",
        chart={"gua": "离"}, rule_score=0.6, rule_reading="体生用，泄气",
    )
    d = mr.to_dict()
    assert d["method"] == "梅花" and d["rule_level"] == "吉"
    assert d["rule_score"] == 0.6
    mr.with_score(0.9, "体用比和", ["体用比和"])
    assert mr.rule_score == 0.9
    assert mr.rules_used == ["体用比和"]
