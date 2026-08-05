"""历法地基：干支、节气、真太阳时、时辰、月将、旬空。

以 lunar_python（MIT，6tail/lunar-java 移植）为历法数据源，在其上封装
古法排盘所需的统一时间地基。所有术数模块只依赖本模块，不直接接触库。

约定（古法主流规则，可配置项在 engine 层）：
  - 时辰：23:00 起为次日子时（晚子时换日），00:00-00:59 为当日早子时。
  - 干支年：立春换年（八字年）；干支月：节气换月（月建），五虎遁定干。
  - 月将：中气过宫（雨水后亥将登明…大寒后子将神后）。
  - 旬空：六甲旬末两字。
  - 真太阳时：经度 - 时区子午线经差 × 4 分 + 均时差（可关）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from lunar_python import Solar

GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
GAN_WUXING = "木木火火土土金金水水"
ZHI_WUXING = "水水土木木土火火土金金土"

# 十二时辰：子(23-1) 丑(1-3) 寅(3-5) 卯(5-7) 辰(7-9) 巳(9-11)
#           午(11-13) 未(13-15) 申(15-17) 酉(17-19) 戌(19-21) 亥(21-23)
HOUR_NAMES = list(ZHI)

# 节气月建：立春起寅月 … 小寒起丑月。
JIE_MONTH_ZHI = {
    "立春": "寅", "惊蛰": "卯", "清明": "辰", "立夏": "巳",
    "芒种": "午", "小暑": "未", "立秋": "申", "白露": "酉",
    "寒露": "戌", "立冬": "亥", "大雪": "子", "小寒": "丑",
}
# 月将（中气过宫）：雨水后亥将登明 … 大寒后子将神后。
ZHONGQI_YUEJIANG = {
    "雨水": ("亥", "登明"), "春分": ("戌", "河魁"), "谷雨": ("酉", "从魁"),
    "小满": ("申", "传送"), "夏至": ("未", "小吉"), "大暑": ("午", "胜光"),
    "处暑": ("巳", "太乙"), "秋分": ("辰", "天罡"), "霜降": ("卯", "太冲"),
    "小雪": ("寅", "功曹"), "冬至": ("丑", "大吉"), "大寒": ("子", "神后"),
}
JIEQI_NAMES = tuple(JIE_MONTH_ZHI) + tuple(ZHONGQI_YUEJIANG)
JIEQI_ORDER = (
    "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑",
    "立秋", "处暑", "白露", "秋分", "寒露", "霜降",
    "立冬", "小雪", "大雪", "冬至", "小寒", "大寒",
)

# 干支日锚点：1900-01-01 为甲戌（干支序 10）。
_ANCHOR_DATE = date(1900, 1, 1)
_ANCHOR_INDEX = 10


def gan_index(gan: str) -> int:
    return GAN.index(gan)


def zhi_index(zhi: str) -> int:
    return ZHI.index(zhi)


def ganzhi_index(ganzhi: str) -> int:
    """六十甲子序（0=甲子 … 59=癸亥）。"""
    g = gan_index(ganzhi[0])
    z = zhi_index(ganzhi[1])
    assert (g - z) % 2 == 0, f"非法干支组合: {ganzhi}"
    return ((g - z) // 2 * 12 + z) % 60


def ganzhi_from_index(idx: int) -> str:
    return GAN[idx % 10] + ZHI[idx % 12]


def wuxing_of_gan(gan: str) -> str:
    return GAN_WUXING[gan_index(gan)]


def wuxing_of_zhi(zhi: str) -> str:
    return ZHI_WUXING[zhi_index(zhi)]


def day_index_from_date(d: date) -> int:
    """公历日期 → 干支日序（0=甲子），未做子时换日。"""
    return (_ANCHOR_INDEX + (d - _ANCHOR_DATE).days) % 60


def hour_zhi_index(hour: int) -> int:
    """钟表小时 → 时辰地支序（23/0→子0，1→丑1 … 21/22→亥11）。"""
    return ((hour + 1) // 2) % 12


def hour_ganzhi(day_gan: str, hour: int) -> str:
    """五鼠遁：按（子时换日后的）日干与钟表小时定时干支。"""
    shi_zhi = hour_zhi_index(hour)
    shi_gan = (gan_index(day_gan) % 5) * 2 + shi_zhi
    return GAN[shi_gan % 10] + ZHI[shi_zhi]


def month_ganzhi(year_gan: str, month_zhi: str) -> str:
    """五虎遁：年干定寅月干，再沿六十甲子顺推月支（子/丑月需跨 12 步回绕）。"""
    yin_month_gan = GAN[((gan_index(year_gan) % 5) * 2 + 2) % 10]
    anchor = ganzhi_index(yin_month_gan + "寅")
    offset = (zhi_index(month_zhi) - 2) % 12
    return ganzhi_from_index((anchor + offset) % 60)


def xun_kong(day_ganzhi: str) -> tuple[str, str]:
    """六甲旬空：返回旬空两字。例：甲子旬空戌亥。"""
    g = gan_index(day_ganzhi[0])
    z = zhi_index(day_ganzhi[1])
    start_zhi = (z - g) % 12  # 本旬甲所临之地支
    return ZHI[(start_zhi + 10) % 12], ZHI[(start_zhi + 11) % 12]


def equation_of_time_minutes(dt: datetime) -> float:
    """均时差近似（分钟）。Meeus 简化式，精度约 ±1 分钟，够用于定时辰。"""
    n = dt.timetuple().tm_yday
    b = 2.0 * math.pi * (n - 81) / 365.0
    return 9.87 * math.sin(2.0 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)


@dataclass(frozen=True)
class GanZhiMoment:
    """一个排盘时刻的全部历法要素（对任何术数通用）。"""

    wall: datetime                 # 本地钟表时间（naive）
    true_solar: datetime           # 真太阳时（关闭时等于 wall）
    use_true_solar: bool
    tz_offset_hours: float
    longitude: float | None

    year_ganzhi: str               # 立春换年
    month_ganzhi: str              # 节气换月（月建）
    day_ganzhi: str                # 已做 23:00 子时换日
    hour_ganzhi: str               # 五鼠遁
    hour_zhi_index: int            # 0=子 … 11=亥
    day_gan_index: int
    day_zhi_index: int
    day_index: int                 # 六十甲子序（子时换日后）

    xun_kong: tuple[str, str]
    prev_jieqi: str
    next_jieqi: str
    jieqi_table: dict[str, datetime] = field(repr=False)
    month_zhi: str                 # 月建地支（寅=立春起）
    yue_jiang: str                 # 月将地支（中气过宫）
    yue_jiang_name: str            # 月将名（登明/河魁…）
    season: str

    lunar_year: int                # 农历年数（数字）
    lunar_month: int               # 农历月数（数字）
    lunar_day: int                 # 农历日数（数字）
    lunar_year_cn: str
    lunar_month_cn: str
    lunar_day_cn: str

    def meihua_nums(self) -> tuple[int, int, int, int]:
        """梅花易数时间起卦数：年支数(子1..亥12)、月、日、时。"""
        return (
            self.day_zhi_index % 12 + 1,
            self.lunar_month,
            self.lunar_day,
            self.hour_zhi_index + 1,
        )

    def to_dict(self) -> dict:
        out = {
            "wall": self.wall.isoformat(timespec="seconds"),
            "true_solar": self.true_solar.isoformat(timespec="seconds"),
            "use_true_solar": self.use_true_solar,
            "tz_offset_hours": self.tz_offset_hours,
            "longitude": self.longitude,
            "year_ganzhi": self.year_ganzhi,
            "month_ganzhi": self.month_ganzhi,
            "day_ganzhi": self.day_ganzhi,
            "hour_ganzhi": self.hour_ganzhi,
            "hour_zhi": ZHI[self.hour_zhi_index],
            "xun_kong": "".join(self.xun_kong),
            "prev_jieqi": self.prev_jieqi,
            "next_jieqi": self.next_jieqi,
            "month_zhi": self.month_zhi,
            "yue_jiang": self.yue_jiang,
            "yue_jiang_name": self.yue_jiang_name,
            "season": self.season,
            "lunar": f"{self.lunar_year_cn}年{self.lunar_month_cn}月{self.lunar_day_cn}",
        }
        return out


def _solar_to_datetime(s: Solar) -> datetime:
    # 节气时刻为本地钟表时间（naive），与 wall 同基准比较。
    return datetime(s.getYear(), s.getMonth(), s.getDay(), s.getHour(), s.getMinute(), s.getSecond())  # noqa: DTZ001


def _parse_lunar(wall: datetime) -> tuple:
    """取 lunar 对象及节气表（表值为 Solar，统一转 datetime）。wall 为本地钟表时间。"""
    solar = Solar.fromYmdHms(
        wall.year, wall.month, wall.day, wall.hour, wall.minute, wall.second
    )
    lunar = solar.getLunar()
    table = lunar.getJieQiTable()
    table = {k: _solar_to_datetime(v) for k, v in table.items() if k in JIEQI_ORDER}
    return lunar, table


def _prev_next(table: dict[str, datetime], now: datetime) -> tuple[str, str]:
    """最近已过节气 / 下一个节气。

    注意：getJieQiTable 的窗口是“冬至→大雪”，含上一冬的冬至/小寒/大寒，
    因此必须按时间序迭代，不能按名字序。
    """
    entries = sorted((t, name) for name, t in table.items() if t is not None)
    prev = next_name = None
    for t, name in entries:
        if t <= now:
            prev = name
        elif next_name is None:
            next_name = name
    if prev is None:
        prev = entries[0][1] if entries else "立春"
    if next_name is None:  # 表窗口外，按名字顺序推下一个节气
        next_name = JIEQI_ORDER[(JIEQI_ORDER.index(prev) + 1) % 24]
    return prev, next_name


def moment_from_utc(
    now_utc: datetime,
    tz_offset_hours: float = 8.0,
    longitude: float | None = None,
    use_true_solar: bool = False,
) -> GanZhiMoment:
    """从 UTC 时刻构建排盘时刻。

    参数：
      now_utc           UTC 时刻（naive 或带时区均可，naive 视为 UTC）。
      tz_offset_hours   目标时区偏移（东八区=8.0）。
      longitude         当地经度（度），开启真太阳时且提供经度时才修正。
      use_true_solar    是否启用真太阳时定时辰/节气。
    """
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    wall = now_utc + timedelta(hours=tz_offset_hours)

    true_solar = wall
    if use_true_solar and longitude is not None:
        tz_meridian = tz_offset_hours * 15.0
        correction = (longitude - tz_meridian) * 4.0 + equation_of_time_minutes(wall)
        true_solar = wall + timedelta(minutes=correction)

    clock = true_solar if use_true_solar else wall
    day_idx = day_index_from_date(clock.date())
    if clock.hour >= 23:  # 晚子时换日
        day_idx = (day_idx + 1) % 60
    day_ganzhi = ganzhi_from_index(day_idx)
    hour_zhi = hour_zhi_index(clock.hour)
    hour_gz = hour_ganzhi(day_ganzhi[0], clock.hour)

    lunar, table = _parse_lunar(wall)

    # 八字年：立春换年。库按“立春日期”换年，此处按立春精确时刻修正
    # （立春当日、时刻未到者仍属前一年）。
    year_gz = lunar.getYearInGanZhiByLiChun()
    lichun_time = table.get("立春")
    if lichun_time is not None and wall.date() == lichun_time.date() and wall < lichun_time:
        year_gz = ganzhi_from_index((ganzhi_index(year_gz) - 1) % 60)

    prev_jieqi, next_jieqi = _prev_next(table, wall)
    month_zhi = JIE_MONTH_ZHI.get(prev_jieqi, "寅")
    month_gz = month_ganzhi(year_gz[0], month_zhi)

    # 月将按“最近一个已过的中气”定（中气过宫）。
    yue_jiang, yue_jiang_name = _resolve_yuejiang(table, wall)

    season = _season(prev_jieqi)

    return GanZhiMoment(
        wall=wall,
        true_solar=true_solar,
        use_true_solar=use_true_solar,
        tz_offset_hours=tz_offset_hours,
        longitude=longitude,
        year_ganzhi=year_gz,
        month_ganzhi=month_gz,
        day_ganzhi=day_ganzhi,
        hour_ganzhi=hour_gz,
        hour_zhi_index=hour_zhi,
        day_gan_index=gan_index(day_ganzhi[0]),
        day_zhi_index=zhi_index(day_ganzhi[1]),
        day_index=day_idx,
        xun_kong=xun_kong(day_ganzhi),
        prev_jieqi=prev_jieqi,
        next_jieqi=next_jieqi,
        jieqi_table=table,
        month_zhi=month_zhi,
        yue_jiang=yue_jiang,
        yue_jiang_name=yue_jiang_name,
        season=season,
        lunar_year=lunar.getYear(),
        lunar_month=lunar.getMonth(),
        lunar_day=lunar.getDay(),
        lunar_year_cn=lunar.getYearInChinese(),
        lunar_month_cn=lunar.getMonthInChinese(),
        lunar_day_cn=lunar.getDayInChinese(),
    )


def _resolve_yuejiang(table: dict[str, datetime], now: datetime) -> tuple[str, str]:
    """最近已过的中气 → 月将（中气过宫）。按时间序迭代（表窗口含上一冬）。"""
    past_zhongqi = None
    for t, name in sorted((t, n) for n, t in table.items() if t is not None and n in ZHONGQI_YUEJIANG):
        if t <= now:
            past_zhongqi = name
    if past_zhongqi is None:
        return ZHONGQI_YUEJIANG["大寒"]
    return ZHONGQI_YUEJIANG[past_zhongqi]


def _season(prev_jieqi: str) -> str:
    if prev_jieqi in ("立春", "雨水", "惊蛰", "春分", "清明", "谷雨"):
        return "春"
    if prev_jieqi in ("立夏", "小满", "芒种", "夏至", "小暑", "大暑"):
        return "夏"
    if prev_jieqi in ("立秋", "处暑", "白露", "秋分", "寒露", "霜降"):
        return "秋"
    return "冬"


def jieqi_at_or_before(table: dict[str, datetime], name: str, now: datetime) -> datetime | None:
    t = table.get(name)
    return t if t is not None and t <= now else None
