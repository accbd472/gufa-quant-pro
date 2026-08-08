# -*- coding: utf-8 -*-
"""探测候选池各币在 OKX 的最早 1d K 线时间（作为本命盘上市时间）。

OKX v5 实测语义（与文档相反）：
  - market/candles        -> 最近 300 根（无历史）
  - market/history-candles: after=<ts> 返回该时间戳【之前】(更旧) 的数据
  - 翻页：after=当前最早一根的时间戳，直到返回空 = 拿到最早 K 线

用法: py -3.10 scripts/probe_listing_times.py
输出: runtime/listing_times_probe.json  { "BTC/USDT": "2017-05-31T00:00:00+00:00", ... }
"""
import datetime
import json
import os
import time

import requests

CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
HIST_URL = "https://www.okx.com/api/v5/market/history-candles"


def _proxies() -> dict:
    """从 config.json 读取首选代理；无代理配置则直连。"""
    proxy = ""
    try:
        config = json.load(open("config.json", encoding="utf-8"))
        proxy = str((config.get("exchange") or {}).get("proxy_url") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass
    return {"http": proxy, "https": proxy} if proxy else {}


def _get(params: dict) -> list:
    url = HIST_URL if "after" in params or "before" in params else CANDLES_URL
    r = requests.get(url, params=params, proxies=_proxies(), timeout=20)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or []
    return data


def earliest_ts(inst: str) -> int | None:
    """返回 inst 在 OKX 上最早的 1D K 线时间戳（毫秒）；无数据返回 None。"""
    d = _get({"instId": inst, "bar": "1D", "limit": "300"})
    if not d:
        return None
    cursor = int(d[-1][0])  # 最近 300 根里最早的一根
    first = cursor
    for _ in range(200):  # 防死循环上限（200 页 ≈ 55 年日线）
        d = _get({"instId": inst, "bar": "1D", "after": str(cursor), "limit": "100"})
        if not d:
            break
        oldest = int(d[-1][0])
        if oldest >= cursor:  # 未向前推进
            break
        cursor = oldest
        first = oldest
        time.sleep(0.12)
    return first


def main() -> None:
    config = json.load(open("config.json", encoding="utf-8"))
    symbols = config["runtime"]["symbols"]
    result: dict = {}
    for sym in symbols:
        inst = sym.replace("/USDT", "-USDT")
        try:
            ts = earliest_ts(inst)
            if ts:
                dt = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc)
                result[sym] = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                print(f"{sym:16s} -> {dt.date()}  OK", flush=True)
            else:
                result[sym] = None
                print(f"{sym:16s} -> NO DATA", flush=True)
        except Exception as exc:  # noqa: BLE001 - 单币失败不中断
            result[sym] = None
            print(f"{sym:16s} -> ERR {str(exc)[:120]}", flush=True)
        time.sleep(0.12)
    json.dump(
        result,
        open("runtime/listing_times_probe.json", "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=1,
    )
    ok = sum(1 for v in result.values() if v)
    print(f"\n共 {len(result)} 个，成功 {ok} 个 -> runtime/listing_times_probe.json")


if __name__ == "__main__":
    main()
