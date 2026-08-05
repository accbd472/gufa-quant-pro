"""gufa_divination —— GuFaQuant-Pro 8.0 真实古法排盘包。

包结构（每项术数一个模块，全部输出结构化盘象 + 规则分）：
  calendar.py  历法地基（干支/节气/真太阳时/月将/旬空）
  base.py      五行生克/旺衰/十神/打分约定
  yijing.py    易经（金钱卦/大衍筮法）
  meihua.py    梅花易数（时间起卦 + 体用生克）
  liuyao.py    六爻（京房纳甲）
  liuren.py    大六壬（九宗门）
  qimen.py     奇门遁甲（转盘·拆补法）
  bazi.py      八字（子平）
  sizhu.py     四柱（本命盘四柱总评）
  ziwei.py     紫微斗数（三合派）
  taiyi.py     太乙神数
  fengshui.py  风水（玄空飞星）
  bagua.py     八卦（先天/后天八卦、六十四卦库）
  engine.py    DivinationEngine 聚合入口
  schools.py   流派常量

“真实性”边界：本包保证排盘/断卦过程严格遵循所选古法流派（并用公开
标准盘用例校验），不保证预测结果准确；不构成投资建议。
"""

__version__ = "0.1.0"
