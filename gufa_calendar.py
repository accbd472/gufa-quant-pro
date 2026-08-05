"""GuFaQuant-Pro 8.0 —— 历法服务层。

职责：
1. 真太阳时修正（均时差 + 经度差），供各排盘器统一使用。
2. 基于 lunar_python 封装农历 / 干支 / 节气 / 旬空 / 时辰等时间上下文。
3. 解析标的上市时间（本命盘基准）。

lunar_python 是 6tail lunar-java 的 Python 移植（MIT），历法数据与
农历/干支/节气算法以其为准；本模块只做时间上下文组装，不重复实现天文算法。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from lunar_python import Solar

UTC = timezone.utc
CHINA_TZ = timezone(timedelta(hours=8))  # 东八区（本地标准时基准，真太阳时在其上修正）


def _tz_aware(dt: datetime) -> datetime:
    """把 naive datetime 视为东八区，其余保持原时区。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CHINA_TZ)
    return dt


@dataclass
class PaipanTime:
    """一次排盘所需的统一时间上下文。"""

    solar_dt: datetime               # 排盘用公历时间（真太阳时修正后，aware）
    solar_iso: str = ""              # ISO-8601
    standard_iso: str = ""           # 修正前标准时 ISO
    correction_minutes: float = 0.0  # 真太阳时修正量（分钟）
    lunar_text: str = ""             # 农历文本
    ganzhi_full: str = ""            # 完整干支："丙午年 乙未月 辛亥日 丁酉时"
    year_gz: str = ""
    month_gz: str = ""
    day_gz: str = ""
    time_gz: str = ""
    day_gan: str = ""                # 日干（用于部分起卦法）
    day_zhi: str = ""                # 日支
    shichen_index: int = -1          # 时辰索引：子=0,丑=1,...亥=11
    shichen_name: str = ""           # 时辰名
    jieqi: str | None = None      # 当前所在节气（可能为空）
    next_jieqi: str | None = None  # 下一个节气
    xun_kong: str | None = None   # 日旬空亡
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "solar_iso": self.solar_iso,
            "standard_iso": self.standard_iso,
            "correction_minutes": round(self.correction_minutes, 2),
            "lunar_text": self.lunar_text,
            "ganzhi": self.ganzhi_full,
            "year": self.year_gz,
            "month": self.month_gz,
            "day": self.day_gz,
            "time": self.time_gz,
            "shichen": self.shichen_name,
            "jieqi": self.jieqi,
            "next_jieqi": self.next_jieqi,
            "xun_kong": self.xun_kong,
            "notes": self.notes,
        }


SHICHEN_NAMES = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]


class CalendarService:
    """历法服务：真太阳时修正 + lunar_python 时间上下文。"""

    STANDARD_LONGITUDE = 120.0  # 东八区中央经线

    def __init__(self, config: Any):
        """config 为 PaipanConfig（gufa_quant_pro 中定义）；此处鸭子类型避免循环依赖。"""
        self.config = config

    # ------------------------------------------------------------------
    # 真太阳时修正
    # ------------------------------------------------------------------

    @staticmethod
    def _equation_of_time_minutes(dt: datetime) -> float:
        """均时差（分钟），低精度经典公式，精度约 ±0.5 分钟，满足排盘需求。"""
        day_of_year = dt.timetuple().tm_yday
        b = 2.0 * math.pi * (day_of_year - 81) / 364.0
        return 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    def true_solar_time(self, dt: datetime) -> datetime:
        """把本地标准时间修正为真太阳时（东八区经度基准由配置 longitude 参与计算）。"""
        aware = _tz_aware(dt)
        local_standard = aware.astimezone(CHINA_TZ)
        eot = self._equation_of_time_minutes(local_standard)
        longitude_correction = 4.0 * (self.config.longitude - self.STANDARD_LONGITUDE)
        correction = eot + longitude_correction
        true_solar = local_standard + timedelta(minutes=correction)
        return true_solar

    # ------------------------------------------------------------------
    # 时间上下文
    # ------------------------------------------------------------------

    def context(self, dt: datetime, note: str = "") -> PaipanTime:
        aware = _tz_aware(dt)
        standard_iso = aware.astimezone(UTC).isoformat(timespec="seconds")
        true_dt = self.true_solar_time(aware) if self.config.true_solar_time else aware
        correction = (true_dt - aware).total_seconds() / 60.0

        solar = Solar.fromYmdHms(
            true_dt.year, true_dt.month, true_dt.day,
            true_dt.hour, true_dt.minute, true_dt.second,
        )
        lunar = solar.getLunar()

        year_gz = lunar.getYearInGanZhi()
        month_gz = lunar.getMonthInGanZhi()
        day_gz = lunar.getDayInGanZhi()
        time_gz = lunar.getTimeInGanZhi()
        shichen_index = lunar.getTimeZhiIndex()
        if not 0 <= shichen_index <= 11:
            shichen_index = 0
        shichen = SHICHEN_NAMES[shichen_index]

        jieqi = lunar.getJieQi() or None
        next_jieqi = None
        try:
            nxt = lunar.getNextJieQi(True)
            if nxt is not None:
                next_jieqi = nxt.getName()
        except Exception:  # noqa: BLE001 - 节气缺失不影响排盘主流程
            next_jieqi = None

        notes: list[str] = []
        if self.config.true_solar_time:
            notes.append(f"真太阳时修正 {correction:+.1f} 分钟（经度 {self.config.longitude}°E）")
        if note:
            notes.append(note)

        return PaipanTime(
            solar_dt=true_dt,
            solar_iso=true_dt.astimezone(UTC).isoformat(timespec="seconds"),
            standard_iso=standard_iso,
            correction_minutes=correction,
            lunar_text=lunar.toString() + " " + shichen + "时",
            ganzhi_full=f"{year_gz}年 {month_gz}月 {day_gz}日 {time_gz}时",
            year_gz=year_gz,
            month_gz=month_gz,
            day_gz=day_gz,
            time_gz=time_gz,
            day_gan=lunar.getDayGan(),
            day_zhi=lunar.getDayZhi(),
            shichen_index=shichen_index,
            shichen_name=shichen,
            jieqi=jieqi,
            next_jieqi=next_jieqi,
            xun_kong=lunar.getDayXunKong(),
            notes=notes,
        )

    # ------------------------------------------------------------------
    # 上市时间（本命盘基准）
    # ------------------------------------------------------------------

    def listing_time(
        self,
        symbol: str,
        first_candle_ts: Any | None = None,
    ) -> datetime | None:
        """解析标的上市时间。

        - manual: 取配置 listing_times[symbol]
        - ohlcv: 用最早 K 线时间戳（exchange_timezone 解析）
        返回 None 表示无法确定（调用方回退为只用时空盘）。
        """
        if self.config.listing_time_source == "manual":
            iso = self.config.listing_times.get(symbol)
            if not iso:
                return None
            try:
                return _tz_aware(datetime.fromisoformat(iso))
            except ValueError:
                return None
        if first_candle_ts is None:
            return None
        tz = UTC if self.config.exchange_timezone.upper() == "UTC" else CHINA_TZ
        try:
            ts = float(first_candle_ts)
            if ts > 1e11:  # 毫秒时间戳（ccxt fetch_ohlcv 默认毫秒）
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=tz)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        try:
            parsed = datetime.fromisoformat(str(first_candle_ts).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed
        except ValueError:
            return None
