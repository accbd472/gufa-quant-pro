# -*- coding: utf-8 -*-
"""
GuFaQuant-Pro 科幻实时监控大屏（独立服务，端口 8601）
====================================================
- 读取 runtime/ 下 state.json / health.json / equity.jsonl / orders.audit.jsonl / gufa_quant.jsonl
- SSE 实时推送，前端暗黑霓虹科幻风，无任何外部 CDN 依赖（离线可用）
- 用法: python gufa_dashboard.py --config config.json [--port 8601] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

APP_NAME = "GuFaQuant-DASHBOARD"
POLL_INTERVAL = 2.0          # SSE 推送间隔（秒）
EQUITY_MAX_POINTS = 720      # 权益曲线最大点数（2s 采样 → 24h）
LOG_TAIL = 120               # 日志尾部行数

# AI 历史买卖动作：orders.audit.jsonl 中与交易动作相关的事件
TRADE_EVENTS = {"trigger_entry": "买入", "trigger_exit": "卖出", "order_fill": "成交",
                "trigger_entry_skip": "✕ 跳过", "depth_blocked": "⚠ 拦截"}

# 大屏统一使用本地时区（Asia/Shanghai，固定 UTC+8，无夏令时）
LOCAL_TZ = timezone(timedelta(hours=8))


def _local_hhmmss(iso: str) -> str:
    """ISO 时间字符串 → 本地(UTC+8) HH:MM:SS；解析失败则原样截取。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    except Exception:
        return iso[11:19] if len(iso) >= 19 else iso


def _local_mmdd(iso: str) -> str:
    """ISO 时间字符串 → 本地(UTC+8) MM-DD；解析失败则原样截取。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%m-%d")
    except Exception:
        return iso[5:10] if len(iso) >= 10 else iso


def _local_iso(iso: str) -> str:
    """ISO 时间字符串 → 本地(UTC+8) ISO（保留完整格式，前端 slice 即可）；失败原样返回。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
    except Exception:
        return iso

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>古法量化 · 决策中枢</title>
<style>
  :root {
    --cy: #22d3ee; --pu: #a78bfa; --gr: #34d399; --rd: #f87171; --am: #fbbf24;
    --bg: #0b0f17; --panel: rgba(17, 24, 39, .78); --line: rgba(34, 211, 238, .18);
    --txt: #dbe7f3; --dim: #7b93ad;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    background:
      radial-gradient(1100px 600px at 80% -10%, rgba(167, 139, 250, .07), transparent 60%),
      radial-gradient(900px 500px at 5% 110%, rgba(34, 211, 238, .05), transparent 60%),
      var(--bg);
    color: var(--txt); font-family: "Microsoft YaHei", "PingFang SC", "Consolas", monospace;
    overflow: hidden; font-size: 13px;
  }
  #app { position: relative; z-index: 2; height: 100vh; display: flex; flex-direction: column; padding: 10px 14px 8px; gap: 8px; }

  /* ---------- 顶栏 ---------- */
  header { display: flex; align-items: center; gap: 16px; padding: 8px 16px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
  .logo { font-size: 19px; font-weight: 700; letter-spacing: 2px; color: var(--cy); white-space: nowrap; }
  .logo small { font-size: 10px; color: var(--pu); display: block; margin-top: 1px; font-weight: 400; }
  .st { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--dim); }
  .led { width: 8px; height: 8px; border-radius: 50%; background: var(--am); box-shadow: 0 0 8px var(--am); }
  .led.ok { background: var(--gr); box-shadow: 0 0 8px var(--gr); }
  .led.warn { background: var(--am); box-shadow: 0 0 8px var(--am); }
  .led.busy { background: var(--cy); box-shadow: 0 0 8px var(--cy); animation: pulse 1.6s infinite; }
  .led.bad { background: var(--rd); box-shadow: 0 0 8px var(--rd); animation: pulse .7s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .4; } }
  .sp { flex: 1; }
  .sclock { font-size: 16px; color: var(--cy); font-weight: 600; font-variant-numeric: tabular-nums; }
  .tag { font-size: 10px; padding: 2px 9px; border-radius: 12px; border: 1px solid var(--line); color: var(--am); }
  .tag.sb { color: var(--pu); }
  .fbtn { font-size: 11px; padding: 3px 10px; border-radius: 8px; border: 1px solid var(--line);
    color: var(--dim); cursor: pointer; background: transparent; }
  .fbtn:hover { color: var(--cy); border-color: var(--cy); }

  /* ---------- 时辰卡 ---------- */
  .shichen { display: flex; align-items: center; gap: 8px; padding: 3px 12px;
    border: 1px solid rgba(167,139,250,.35); border-radius: 10px; background: rgba(167,139,250,.06); }
  .shichen .gz { font-size: 13px; color: var(--pu); font-weight: 700; letter-spacing: 1px; }
  .shichen .cd { font-size: 10px; color: var(--dim); font-variant-numeric: tabular-nums; }

  /* ---------- 指标条 ---------- */
  .kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 7px 12px; display: flex; flex-direction: column; gap: 2px; }
  .kpi .l { font-size: 10px; color: var(--dim); letter-spacing: 1px; }
  .kpi .v { font-size: 17px; font-weight: 700; color: var(--txt); font-variant-numeric: tabular-nums; }
  .kpi .v.green { color: var(--gr); } .kpi .v.red { color: var(--rd); } .kpi .v.amber { color: var(--am); }
  .kpi .s { font-size: 9px; color: var(--dim); }

  /* ---------- 主区 ---------- */
  main { flex: 1; display: grid; min-height: 0;
    grid-template-columns: 1.15fr 0.85fr;
    grid-template-rows: 1.1fr 1.35fr 0.65fr;
    grid-template-areas:
      "equity  ancient"
      "decision ancient"
      "stream  stream";
    gap: 8px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 8px 10px; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .card h3 { font-size: 11px; letter-spacing: 1.5px; color: var(--dim); margin-bottom: 6px;
    display: flex; align-items: center; gap: 6px; flex-shrink: 0; font-weight: 600; }
  .card h3::after { content: ""; flex: 1; height: 1px; background: linear-gradient(90deg, var(--line), transparent); }
  .chart { flex: 1; min-height: 0; }
  canvas { width: 100%; height: 100%; display: block; }

  /* ---------- Tab ---------- */
  .tabs { display: flex; gap: 2px; flex-shrink: 0; margin-bottom: 6px; border-bottom: 1px solid var(--line); }
  .tab { padding: 4px 13px; font-size: 11px; color: var(--dim); cursor: pointer;
    border: 1px solid transparent; border-bottom: none; border-radius: 8px 8px 0 0; }
  .tab:hover { color: var(--cy); }
  .tab.active { color: var(--cy); border-color: var(--line); background: rgba(34,211,238,.07); }
  .tab .bdg { display: inline-block; min-width: 15px; text-align: center; margin-left: 3px;
    padding: 0 4px; font-size: 9px; border-radius: 7px; background: rgba(34,211,238,.15); color: var(--cy); }
  .tab-panes { flex: 1; min-height: 0; overflow: hidden; }
  .tab-pane { display: none; height: 100%; overflow-y: auto; }
  .tab-pane.active { display: flex; flex-direction: column; }
  .rsym { font-size: 10px; padding: 1px 8px; border-radius: 8px; border: 1px solid var(--line);
    color: var(--dim); cursor: pointer; font-variant-numeric: tabular-nums; }
  .rsym:hover { color: var(--cy); }
  .rsym.active { color: var(--cy); background: rgba(34,211,238,.1); border-color: rgba(34,211,238,.45); }
  #eqWin { cursor: default; border-color: transparent; padding: 1px 0; }

  /* ---------- 行情/持仓行 ---------- */
  .quotes { display: flex; flex-direction: column; gap: 3px; overflow-y: auto; flex: 1; min-height: 0;
    border: 1px solid rgba(34,211,238,.12); border-radius: 8px; padding: 4px 8px; }
  .quote { display: flex; align-items: center; gap: 8px; font-size: 11.5px; font-variant-numeric: tabular-nums; }
  .quote.held { background: rgba(34,211,238,.05); border-left: 2px solid var(--cy); padding-left: 5px; }
  .quote .sym { width: 74px; color: var(--cy); white-space: nowrap; }
  .quote .qavg { width: 72px; text-align: right; color: var(--dim); font-size: 10px; }
  .quote .qp { flex: 1; text-align: right; }
  .quote .qch { width: 58px; text-align: right; }
  .quote .qch.up { color: var(--gr); } .quote .qch.down { color: var(--rd); }
  .quote .qv { width: 76px; text-align: right; color: var(--dim); }
  .quote.posrow { cursor: pointer; }
  .quote.posrow:hover { background: rgba(34,211,238,.1); }
  .quote.posrow.posactive { border-left-color: var(--pu); background: rgba(167,139,250,.1); }

  /* ---------- 初选条 ---------- */
  .picks { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; flex: 1; min-height: 0; }
  .pick { display: flex; align-items: center; gap: 10px; font-size: 11.5px; }
  .pick .sym { width: 74px; color: var(--cy); white-space: nowrap; }
  .pick .bar { flex: 1; height: 10px; background: rgba(34,211,238,.08); border-radius: 5px; overflow: hidden; }
  .pick .fill { height: 100%; background: linear-gradient(90deg, rgba(34,211,238,.3), var(--cy)); transition: width .6s; }
  .pick .sc { width: 42px; text-align: right; color: var(--gr); }
  .pick .st2 { width: 50px; text-align: right; font-size: 9px; color: var(--dim); }
  .dead { color: var(--rd); font-size: 10px; opacity: .8; }

  /* ---------- 布防 ---------- */
  .verdicts { display: flex; flex-direction: column; gap: 4px; overflow-y: auto; flex: 1; min-height: 0; }
  .verdict { display: flex; align-items: center; gap: 8px; font-size: 11px; }
  .verdict .sym { width: 80px; color: var(--cy); }
  .vbadge { padding: 1px 9px; border-radius: 10px; font-size: 10px; font-weight: 600; }
  .vbadge.BUY { background: rgba(52,211,153,.12); color: var(--gr); border: 1px solid rgba(52,211,153,.4); }
  .vbadge.SELL { background: rgba(248,113,113,.12); color: var(--rd); border: 1px solid rgba(248,113,113,.4); }
  .vbadge.hold { background: rgba(251,191,36,.1); color: var(--am); border: 1px solid rgba(251,191,36,.35); }
  .verdict .cn { font-size: 10px; color: var(--pu); }
  .verdict .cf { flex: 1; font-size: 10px; color: var(--dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ---------- 古法合参 ---------- */
  .ppwrap { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .pptabs { display: flex; gap: 2px; flex-shrink: 0; margin-bottom: 6px; }
  .pptab { padding: 3px 11px; font-size: 11px; color: var(--dim); cursor: pointer;
    border: 1px solid var(--line); border-radius: 10px; }
  .pptab:hover { color: var(--cy); }
  .pptab.active { color: var(--cy); background: rgba(34,211,238,.1); border-color: rgba(34,211,238,.5); }
  .pptab + .pptab { margin-left: 3px; }
  /* 奇门九宫：洛书布局 4 9 2 / 3 5 7 / 8 1 6 */
  .qmgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; flex-shrink: 0; }
  .qmcell { border: 1px solid var(--line); border-radius: 7px; padding: 3px 6px;
    background: rgba(15,23,42,.55); display: flex; flex-direction: column; gap: 1px; min-height: 62px; }
  .qmcell .pos { font-size: 9px; color: #55697f; display: flex; justify-content: space-between; }
  .qmcell .pos b { color: var(--dim); font-weight: 400; }
  .qmcell .stars { display: flex; justify-content: space-between; font-size: 10px; }
  .qmcell .stars .god { color: var(--pu); }
  .qmcell .stars .star { color: var(--cy); }
  .qmcell .gans { display: flex; justify-content: center; gap: 10px; font-size: 12px; align-items: baseline; }
  .qmcell .gans .tg { color: var(--am); font-weight: 700; }
  .qmcell .gans .dg { color: var(--txt); }
  .qmcell .door { font-size: 11px; text-align: center; color: var(--txt); font-weight: 600; }
  .qmcell.hot { border-color: rgba(52,211,153,.6); background: rgba(52,211,153,.07); }
  .qmcell.hott { border-color: rgba(251,191,36,.55); background: rgba(251,191,36,.06); }
  .qmcell .mid { display:flex; align-items:center; justify-content:center; color:#3d5165; font-size:10px; }
  /* 六壬 */
  .lrpan { display:grid; grid-template-columns: repeat(4, 1fr); gap:2px; }
  .lrcell { border:1px solid var(--line); border-radius:6px; text-align:center; padding:3px 2px; font-size:11px; }
  .lrcell .t { color: var(--cy); font-weight:700; }
  .lrcell .b { color: #55697f; font-size:9px; }
  /* 卦象爻线 */
  .hexagram { display:flex; flex-direction:column; gap:3px; align-items:center; }
  .yao { display:flex; gap:2px; }
  .yao span { width: 14px; height: 4px; border-radius: 1px; background: var(--txt); }
  .yao.yang span + span { margin-left: 8px; }
  .yao.dong span { background: var(--am); box-shadow: 0 0 5px rgba(251,191,36,.7); }
  .yao .ylabel { width: 46px; text-align: right; font-size: 9px; color: var(--dim); margin-right: 6px; }
  /* 紫微十二宫环形 4x4 去中心 */
  .zwgrid { display:grid; grid-template-columns: repeat(4, 1fr); gap:3px; }
  .zwcell { border:1px solid var(--line); border-radius:7px; padding:3px 6px; background: rgba(15,23,42,.55);
    min-height: 48px; font-size: 9.5px; }
  .zwcell .pn { color: var(--cy); font-size: 10px; font-weight: 600; }
  .zwcell .pz { color: #55697f; font-size: 8.5px; float: right; }
  .zwcell .st { color: var(--txt); line-height: 1.5; }
  .zwcell .st em { color: var(--pu); font-style: normal; }
  .zwcell.ming { border-color: rgba(167,139,250,.6); background: rgba(167,139,250,.08); }
  .zwcell.zw { border-color: rgba(251,191,36,.5); }
  .pphead { display: flex; align-items: baseline; gap: 10px; flex-shrink: 0; }
  .pphead .gz { font-size: 13px; color: var(--pu); font-weight: 700; }
  .pphead .meta { font-size: 9.5px; color: var(--dim); }
  .pprow { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .ppcard { border: 1px solid var(--line); border-radius: 8px; padding: 6px 9px; background: rgba(15,23,42,.5); }
  .ppcard h4 { font-size: 10px; color: var(--dim); margin-bottom: 4px; letter-spacing: 1px; }
  .ppcard .kv { font-size: 11px; line-height: 1.7; }
  .ppcard .kv b { color: var(--cy); }
  .ppcard .err { font-size: 10px; color: var(--rd); }
  .radarbox { flex: 1; min-height: 120px; }

  /* ---------- AI 解读/交易 ---------- */
  .aisteps { display: flex; flex-direction: column; gap: 3px; overflow-y: auto; flex: 1; min-height: 0; }
  .aistep { display: flex; align-items: center; gap: 6px; font-size: 10.5px; padding: 3px 7px;
    border-radius: 6px; border: 1px solid transparent; color: var(--dim); line-height: 1.4; }
  .aistep .st { flex: 0 0 24px; font-weight: 700; }
  .aistep b { flex: 0 0 30px; color: var(--cy); }
  .aistep .rd { flex: 1; color: var(--txt); }
  .aistep.ok { border-color: rgba(52,211,153,.35); background: rgba(52,211,153,.05); }
  .aistep.fail { border-color: rgba(248,113,113,.35); background: rgba(248,113,113,.05); }
  .aistep.ok .st { color: var(--gr); } .aistep.fail .st { color: var(--rd); }
  .trade { border: 1px solid var(--line); border-left: 2px solid var(--pu); border-radius: 6px;
    padding: 4px 7px; font-size: 10px; line-height: 1.4; }
  .trade .h { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .trade .tm { color: var(--dim); }
  .trade .sym { color: var(--cy); font-weight: 700; }
  .trade .side.buy { color: var(--gr); font-weight: 700; }
  .trade .side.sell { color: var(--rd); font-weight: 700; }
  .trade .side.skip { color: var(--am); }
  .trade .nt { color: var(--dim); font-size: 9px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .trades { display: flex; flex-direction: column; gap: 4px; overflow-y: auto; }

  /* ---------- 日志 ---------- */
  .logbox { flex: 1; overflow-y: auto; font-size: 10.5px; line-height: 1.6; font-family: Consolas, monospace; }
  .logbox .t { color: #55697f; }
  .logbox .WARNING { color: var(--am); }
  .logbox .ERROR { color: var(--rd); }
  .logbox .INFO { color: #93b8d8; }
  .logbox .BUY { color: var(--gr); font-weight: 700; }
  .logbox .SELL { color: var(--rd); font-weight: 700; }

  .empty { color: #4a5f75; font-size: 11px; text-align: center; padding: 14px 0; margin: auto; }
  footer { font-size: 9px; color: #5c7288; text-align: center; letter-spacing: 2px; flex-shrink: 0; }

  @media (max-width: 1300px){ .kpis { grid-template-columns: repeat(3, 1fr); } .kpi .v { font-size: 14px; } }
  @media (orientation: portrait) and (max-width: 820px){
    #app { padding: 6px 8px; gap: 6px; }
    header { flex-wrap: wrap; gap: 6px 10px; padding: 6px 10px; }
    main { grid-template-columns: 1fr; grid-template-rows: auto; grid-template-areas: unset; overflow-y: auto; }
    .card { min-height: 200px; }
    .kpis { grid-template-columns: repeat(2, 1fr); }
    .pprow { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="logo">古法量化<small>决策中枢</small></div>
    <div class="st">系统 <span class="led" id="ledSys"></span><span id="sysTxt">启动</span></div>
    <div class="st">网络 <span class="led" id="ledNet"></span><span id="netTxt">--</span></div>
    <div class="st">AI <span class="led" id="ledAI"></span><span id="aiTxt">--</span></div>
    <div class="sp"></div>
    <div class="shichen" id="shichenBox" title="当前时辰（排盘基准）">
      <span class="gz" id="scGz">--</span><span class="cd" id="scCd">--</span>
    </div>
    <span class="tag" id="tagMode">--</span>
    <span class="tag sb" id="tagSandbox">--</span>
    <button class="fbtn" id="btnFull" title="全屏">⛶ 全屏</button>
    <div class="sclock" id="clock">--:--:--</div>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="l">权益 (USDT)</div><div class="v" id="mEquity">--</div><div class="s" id="kPeak">--</div></div>
    <div class="kpi"><div class="l">今日盈亏</div><div class="v" id="kDayPnl">--</div><div class="s" id="kDayPct"></div></div>
    <div class="kpi"><div class="l">距峰值回撤</div><div class="v" id="kDrawdown">--</div><div class="s" id="kPeakV"></div></div>
    <div class="kpi"><div class="l">持仓</div><div class="v" id="kPos">--</div><div class="s" id="kPosV"></div></div>
    <div class="kpi"><div class="l">布防触发</div><div class="v" id="kTrig">--</div><div class="s">监听中</div></div>
    <div class="kpi"><div class="l">今日成交</div><div class="v" id="kFills">--</div><div class="s" id="kLive"></div></div>
  </div>

  <main>
    <div class="card" style="grid-area:equity">
      <h3>权益曲线
        <span class="rsym" id="eqWin" style="margin-left:auto"></span>
        <span style="display:inline-flex;gap:2px" id="dranges">
          <span class="rsym active" data-r="today">今日</span>
          <span class="rsym" data-r="yday">昨日</span>
          <span class="rsym" data-r="3d">3天</span>
          <span class="rsym" data-r="7d">7天</span>
        </span>
      </h3>
      <div class="chart"><canvas id="eqChart"></canvas></div>
    </div>

    <div class="card" style="grid-area:decision">
      <div class="tabs">
        <div class="tab active" data-pane="pane-pos">持仓<span class="bdg" id="bPos">0</span></div>
        <div class="tab" data-pane="pane-quote">行情</div>
        <div class="tab" data-pane="pane-sel">初选<span class="bdg" id="bSel">0</span></div>
        <div class="tab" data-pane="pane-trig">布防<span class="bdg" id="bTrig">0</span></div>
        <div class="tab" data-pane="pane-trades">成交<span class="bdg" id="bTrades">0</span></div>
      </div>
      <div class="tab-panes">
        <div class="tab-pane active" id="pane-pos"><div class="quotes" id="positions"><div class="empty">无持仓</div></div></div>
        <div class="tab-pane" id="pane-quote"><div class="quotes" id="liveQuotes"><div class="empty">等待行情…</div></div></div>
        <div class="tab-pane" id="pane-sel"><div class="picks" id="picks"><div class="empty">等待初选…</div></div></div>
        <div class="tab-pane" id="pane-trig"><div class="verdicts" id="verdicts"><div class="empty">等待布防…</div></div></div>
        <div class="tab-pane" id="pane-trades"><div class="trades" id="trades"><div class="empty">暂无交易动作</div></div></div>
      </div>
    </div>

    <div class="card" style="grid-area:ancient">
      <div class="tabs">
        <div class="tab active" data-pane="pane-radar">合参雷达<span id="radarSym" style="color:#55697f;font-size:9px;margin-left:4px"></span></div>
        <div class="tab" data-pane="pane-pp">排盘<span id="ppTime" style="color:#55697f;font-size:9px;margin-left:4px"></span></div>
        <div class="tab" data-pane="pane-read">十项解读<span id="aiStepSum" style="color:#55697f;font-size:9px;margin-left:4px"></span></div>
      </div>
      <div class="tab-panes">
        <div class="tab-pane active" id="pane-radar"><div class="radarbox"><canvas id="radar"></canvas></div></div>
        <div class="tab-pane" id="pane-pp">
          <div class="pptabs">
            <div class="pptab active" data-p="pp-qm">奇门</div>
            <div class="pptab" data-p="pp-lr">六壬</div>
            <div class="pptab" data-p="pp-bz">八字</div>
            <div class="pptab" data-p="pp-yi">三易</div>
            <div class="pptab" data-p="pp-zw">紫微</div>
            <div class="pptab" data-p="pp-ty">太乙</div>
            <div class="pptab" data-p="pp-fs">飞星</div>
          </div>
          <div class="ppwrap" id="paipanBox"><div class="empty">等待排盘…</div></div>
        </div>
        <div class="tab-pane" id="pane-read"><div class="aisteps" id="aisteps"><div class="empty">—</div></div></div>
      </div>
    </div>

    <div class="card" style="grid-area:stream">
      <h3>关键事件</h3>
      <div class="logbox" id="log"></div>
    </div>
  </main>
  <footer>古法量化 · 决策中枢 // 数据源: state + health + equity + orders</footer>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
let eqHist = [], radarNames = [], radarVals = [], currentMode = "";
let winStats = {}, curRange = "today";
const TEN_ORDER = ["奇门","六壬","太乙","易经","风水","八字","梅花","紫微","八卦","四柱"];

/* ---------- Tab 切换 ---------- */
document.querySelectorAll(".tab").forEach(t=>{
  t.addEventListener("click",()=>{
    const group = t.parentElement;
    group.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    const panes = group.nextElementSibling;
    panes.querySelectorAll(".tab-pane").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    $(t.dataset.pane).classList.add("active");
  });
});

/* ---------- 全屏 ---------- */
$("btnFull").onclick = ()=>{ if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(()=>{}); };

/* ---------- 权益曲线 ---------- */
function drawEq(){
  const c = $("eqChart"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth, H = c.height = c.clientHeight;
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle = "rgba(34,211,238,.07)"; ctx.lineWidth = 1;
  for (let i=1;i<5;i++){ const y=H*i/5; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }
  if (!eqHist.length){ ctx.fillStyle="#4a5f75"; ctx.font="12px Consolas"; ctx.fillText("暂无数据", W/2-26, H/2); return; }
  let min=Infinity, max=-Infinity;
  for (const p of eqHist){ if(p<min)min=p; if(p>max)max=p; }
  if (max-min < 1e-9){ max+=1; min-=1; }
  const span = max - min;
  min -= span*0.12; max += span*0.12;   // 上下留白, 防止数据点贴边
  const pad = 10;
  const X = i => pad + (W-2*pad) * i / Math.max(1, eqHist.length-1);
  const Y = v => pad + (H-2*pad) * (1 - (v-min)/(max-min));
  const last = eqHist[eqHist.length-1], first = eqHist[0];
  const up = last >= first;
  const col = up ? "#34d399" : "#f87171";
  const g = ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0, up ? "rgba(52,211,153,.25)" : "rgba(248,113,113,.25)");
  g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath();
  eqHist.forEach((v,i)=> i? ctx.lineTo(X(i),Y(v)) : ctx.moveTo(X(i),Y(v)));
  ctx.lineTo(X(eqHist.length-1), H); ctx.lineTo(X(0), H); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
  ctx.beginPath();
  eqHist.forEach((v,i)=> i? ctx.lineTo(X(i),Y(v)) : ctx.moveTo(X(i),Y(v)));
  ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.shadowColor = col; ctx.shadowBlur = 6; ctx.stroke(); ctx.shadowBlur = 0;
  const lx=X(eqHist.length-1), ly=Y(last);
  ctx.beginPath(); ctx.arc(lx, ly, 2.5, 0, 7); ctx.fillStyle = "#fff"; ctx.fill();
  ctx.font = "10px Consolas"; ctx.fillStyle = "rgba(123,147,173,.9)";
  ctx.fillText(max.toFixed(0), 4, 12); ctx.fillText(min.toFixed(0), 4, H-4);
  ctx.fillStyle = col; ctx.fillText(last.toFixed(1), W-46, ly-6>10?ly-6:ly+12);
}

let radarSym = "", radarAll = {}, radarReadings = {}, radarSelSym = "";
let lastLogs = "";

/* ---------- AI 十项解读 ---------- */
function renderTenReadings(sym){
  const rd = radarReadings[sym] || {};
  const items = TEN_ORDER.map(n=>{
    const it = rd[n];
    if (!it) return null;
    const bias = it.bias || "neutral";
    const mark = bias==="bullish" ? "▲" : (bias==="bearish" ? "▼" : "•");
    const cls = bias==="bullish" ? "ok" : (bias==="bearish" ? "fail" : "");
    return `<div class="aistep ${cls}"><span class="st">${mark}</span> <b>${n}</b> <span class="rd">${it.reading||""}</span></div>`;
  }).filter(Boolean);
  if (!items.length){
    $("aiStepSum").textContent = sym ? "// 等待" + sym.replace('/USDT','') : "";
    $("aisteps").innerHTML = '<div class="empty">等待 AI 断卦…</div>';
    return;
  }
  $("aiStepSum").textContent = "// " + (sym?sym.replace('/USDT',''):"");
  $("aisteps").innerHTML = items.join("");
}

/* ---------- 币种选择联动 ---------- */
function selectSym(sym){
  radarSelSym = sym; radarSym = sym;
  radarVals = radarAll[sym] || [0,0,0,0,0,0,0,0,0,0];
  $("radarSym").textContent = sym ? "// " + sym.replace('/USDT','') : "";
  document.querySelectorAll("#positions .posrow").forEach(x=>x.classList.toggle("posactive", x.dataset.sym===sym));
  drawRadar();
  renderTenReadings(sym);
}

/* ---------- 区间分析 ---------- */
function applyRange(){
  const w = winStats[curRange] || {};
  const f2 = n => (n==null || isNaN(n)) ? "--" : n.toFixed(2);
  const pnl = w.pnl, pct = w.pnl_pct;
  let parts = [];
  if (pnl!=null) parts.push((pnl>=0?"+":"")+f2(pnl));
  if (pct!=null) parts.push((pct>=0?"+":"")+pct.toFixed(2)+"%");
  $("eqWin").textContent = parts.length ? parts.join(" / "): "";
  $("eqWin").style.color = (pnl==null||pnl===0) ? "#7b93ad" : (pnl>0 ? "var(--gr)" : "var(--rd)");
}

/* ---------- 雷达图 ---------- */
function drawRadar(){
  const c = $("radar"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth, H = c.height = c.clientHeight;
  ctx.clearRect(0,0,W,H);
  const N = radarNames.length, cx = W/2, cy = H/2, R = Math.min(W,H)/2 - 24;
  const hasData = radarVals.some(v=>Math.abs(v)>0.01);
  if (!N || !hasData){
    ctx.fillStyle="#4a5f75"; ctx.font="11px Microsoft YaHei";
    ctx.fillText(radarSym ? "等待 "+radarSym.replace('/USDT','')+" 评估…" : "等待 AI 评估…", cx-60, cy);
    return;
  }
  const ang = i => -Math.PI/2 + i*2*Math.PI/N;
  for (let ring=1; ring<=4; ring++){
    ctx.beginPath();
    for (let i=0;i<=N;i++){ const a=ang(i%N); const r=R*ring/4; i? ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)) : ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); }
    ctx.strokeStyle = "rgba(34,211,238,.09)"; ctx.stroke();
  }
  for (let i=0;i<N;i++){ const a=ang(i); ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+R*Math.cos(a), cy+R*Math.sin(a)); ctx.strokeStyle="rgba(34,211,238,.09)"; ctx.stroke(); }
  ctx.beginPath();
  for (let i=0;i<=N;i++){ const a=ang(i%N); const v=Math.max(-1,Math.min(1,radarVals[i%N]||0)); const r=R*Math.abs(v); i? ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)) : ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); }
  ctx.closePath();
  const sumV = radarVals.reduce((s,v)=>s+(v||0),0);
  const bull = sumV >= 0;
  const g = ctx.createRadialGradient(cx,cy,8,cx,cy,R);
  g.addColorStop(0, bull ? "rgba(52,211,153,.45)" : "rgba(248,113,113,.45)");
  g.addColorStop(1, "rgba(34,211,238,.08)");
  ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = bull ? "#34d399" : "#f87171"; ctx.lineWidth = 1.4; ctx.shadowColor = bull ? "#34d399" : "#f87171"; ctx.shadowBlur=8; ctx.stroke(); ctx.shadowBlur=0;
  ctx.font = "10px Microsoft YaHei"; ctx.fillStyle = "#93b8d8"; ctx.textAlign = "center";
  for (let i=0;i<N;i++){ const a=ang(i); const tx=cx+(R+13)*Math.cos(a), ty=cy+(R+13)*Math.sin(a);
    ctx.fillText(radarNames[i], tx, ty+3); }
}

/* ---------- 交易记录 ---------- */
function renderTrades(list){
  const el = $("trades");
  if (!el) return;
  if (!list || !list.length){ el.innerHTML = '<div class="empty">暂无交易动作</div>'; return; }
  $("bTrades").textContent = list ? list.length : 0;
  el.innerHTML = list.map(t=>{
    const isSell = t.side==="sell";
    const isCancel = t.canceled;
    const act = isCancel ? t.label : (isSell ? "▼ 卖出" : "▲ 买入");
    const sideCls = isCancel ? "skip" : (isSell ? "sell" : "buy");
    const amt = t.amount ? Number(t.amount).toPrecision(4) : "--";
    const price = t.price ? Number(t.price).toPrecision(6) : "--";
    const val = t.value ? Number(t.value).toFixed(0)+"U" : "";
    return `<div class="trade"><div class="h"><span class="tm">${t.date} ${t.time}</span>
      <span class="sym">${t.sym.replace('/USDT','')}</span>
      <span class="side ${sideCls}">${act}</span>
      <span>${amt} @ ${price} ${val}</span></div>
      ${t.note?`<div class="nt">${t.note}</div>`:""}</div>`;
  }).join("");
}

/* ---------- 状态灯 ---------- */
function setLed(id, cls, txt){ const l=$(id); if(!l) return; l.className="led "+(cls||""); const t=$(id.slice(3).toLowerCase()+"Txt"); if(t) t.textContent=txt; }

/* ---------- 主渲染 ---------- */
function render(d){
  currentMode = d.mode||"";
  setLed("ledSys", d.status==="ok"?"ok":(d.status==="degraded"?"warn":"bad"), {ok:"正常",halted:"暂停",degraded:"降级",error:"异常"}[d.status]||(d.status||"?"));
  setLed("ledNet", d.net_ok?"ok":"bad", d.net_ok?"在线":"离线");
  const aiCls = d.ai_status==="ready"?"ok":(d.ai_status==="degraded"?"warn":(d.ai_busy?"busy":"bad"));
  const aiTxt = d.ai_status==="ready"?"就绪":(d.ai_status==="degraded"?"降级":(d.ai_busy?"处理中":"离线"));
  setLed("ledAI", aiCls, aiTxt);
  $("tagMode").textContent = d.mode||"--";
  $("tagSandbox").textContent = d.sandbox ? "模拟盘" : "实盘";
  // KPI
  if (d.equity!=null) $("mEquity").textContent = d.equity.toFixed(2);
  if (d.peak!=null) $("kPeak").textContent = "峰值 " + d.peak.toFixed(0);
  const dayPnl = (d.equity!=null && d.day_start!=null) ? d.equity - d.day_start : null;
  if (dayPnl!=null){
    const pct = d.day_start ? dayPnl/d.day_start*100 : 0;
    const el = $("kDayPnl");
    el.textContent = (dayPnl>=0?"+":"")+dayPnl.toFixed(2);
    el.className = "v " + (dayPnl>0.0001?"green":(dayPnl<-0.0001?"red":""));
    $("kDayPct").textContent = (pct>=0?"+":"")+pct.toFixed(2)+"% · 日初 "+d.day_start.toFixed(0);
  }
  const dd = (d.equity!=null && d.peak!=null && d.peak>0) ? (d.equity/d.peak-1)*100 : null;
  if (dd!=null){
    const el = $("kDrawdown");
    el.textContent = dd.toFixed(2)+"%";
    el.className = "v " + (dd<-0.005?"red":(dd>-0.0001?"green":"amber"));
    const abs = (d.equity!=null && d.peak!=null) ? d.equity - d.peak : null;
    $("kPeakV").textContent = abs!=null ? (abs>=0?"+":"")+abs.toFixed(0)+"U 距峰值" : "--";
  } else {
    $("kPeakV").textContent = "--";
  }
  $("kPos").textContent = d.position_count!=null ? d.position_count : "--";
  let posVal = 0;
  if (d.positions && d.positions.length) d.positions.forEach(p=>{ if(p.value) posVal += Number(p.value); });
  $("kPosV").textContent = posVal ? "市值 "+posVal.toFixed(0)+"U" : "空仓";
  $("kTrig").textContent = (d.triggers&&d.triggers.length)||0;
  $("kFills").textContent = d.fills!=null?d.fills:"--";
  $("kLive").textContent = d.live_updated_at ? "更新 " + String(d.live_updated_at).slice(11,19) : "";
  // 区间
  if (d.equity_windows){ winStats = d.equity_windows; applyRange(); }
  // 初选
  if (d.selection_date) $("bSel").textContent = d.picks ? d.picks.length : 0;
  if (d.picks && d.picks.length){
    $("picks").innerHTML = d.picks.map(p=>`<div class="pick"><span class="sym">${p.sym.replace('/USDT','')}</span>
      <div class="bar"><div class="fill" style="width:${(p.score*100).toFixed(1)}%"></div></div>
      <span class="sc">${p.score.toFixed(3)}</span><span class="st2">${p.action||''}</span></div>`).join("");
    const dead = d.dead && Object.keys(d.dead).length
      ? Object.entries(d.dead).map(([k,v])=>`<span>✕ ${k.replace('/USDT','')}: ${v}</span>&nbsp;&nbsp;`).join("") : "";
    if (dead) $("picks").innerHTML += `<div class="dead" style="margin-top:6px">${dead}</div>`;
  } else {
    $("picks").innerHTML = '<div class="empty">等待初选…</div>';
  }
  // 行情
  $("bPos").textContent = d.position_count!=null?d.position_count:0;
  $("bTrig").textContent = (d.triggers&&d.triggers.length)||0;
  const quoteEntries = d.quotes && Object.keys(d.quotes).length ? Object.entries(d.quotes) : [];
  if (quoteEntries.length){
    const shortName = (sym)=>{
      const m = String(sym).match(/^(?:swap:)?([^/]+)\//);
      return (m?m[1]:sym) + (String(sym).startsWith("swap:")?"◆":"");
    };
    $("liveQuotes").innerHTML = quoteEntries.map(([sym,q])=>{
      const held = q && Number(q.value)>0;
      const pct = q && q.change_pct!=null ? q.change_pct : 0;
      const cls = pct>0.0001 ? "up" : (pct<-0.0001 ? "down" : "");
      const arrow = pct>0.0001 ? "▲" : (pct<-0.0001 ? "▼" : "•");
      return `<div class="quote${held?" held":""}"><span class="sym">${shortName(sym)}${held?"◆":""}</span>
        <span class="qp">${q?Number(q.price).toPrecision(6):"--"}</span>
        <span class="qch ${cls}">${arrow}${Math.abs(pct).toFixed(2)}%</span>
        <span class="qv">${held?Number(q.value).toFixed(2)+"U":"--"}</span></div>`;
    }).join("");
  } else { $("liveQuotes").innerHTML = '<div class="empty">等待行情…</div>'; }
  // 持仓
  if (d.positions && d.positions.length){
    const cur = radarSelSym || (d.radar && d.radar.symbol) || "";
    $("positions").innerHTML = d.positions.map(p=>{
      const pnlCls = p.pnl_pct>0.0001 ? "up" : (p.pnl_pct<-0.0001 ? "down" : "");
      const arrow = p.pnl_pct>0.0001 ? "▲" : (p.pnl_pct<-0.0001 ? "▼" : "•");
      const act = p.sym===cur ? " posactive" : "";
      const cost = p.avg ? Number(p.avg).toPrecision(7) : "--";
      return `<div class="quote held posrow${act}" data-sym="${p.sym}"><span class="sym">${String(p.sym).replace('/USDT','')}◆</span>
        <span class="qavg">@${cost}</span>
        <span class="qp">${Number(p.price).toPrecision(6)}</span>
        <span class="qch ${pnlCls}">${arrow}${Math.abs(p.pnl_pct).toFixed(2)}%</span>
        <span class="qv">${Number(p.value).toFixed(0)}U</span></div>`;
    }).join("");
    document.querySelectorAll("#positions .posrow").forEach(el=>{ el.onclick = ()=>selectSym(el.dataset.sym); });
  } else {
    $("positions").innerHTML = '<div class="empty">无持仓</div>';
  }
  // 布防
  if (d.mode==="signal" && d.triggers && d.triggers.length){
    $("verdicts").innerHTML = d.triggers.map(t=>`<div class="verdict"><span class="sym">${t.sym.replace('/USDT','')}</span>
      <span class="vbadge hold">入${t.entry_n}</span>
      <span class="cn">出${t.exit_n}</span>
      <span class="cf">${t.target!=null?(t.target*100).toFixed(0)+"%仓":""}${t.first_at?" · "+String(t.first_at).slice(0,5):""}</span></div>`).join("");
  } else if (d.verdicts && d.verdicts.length){
    $("verdicts").innerHTML = d.verdicts.map(v=>`<div class="verdict"><span class="sym">${v.sym.replace('/USDT','')}</span>
      <span class="vbadge ${v.action}">${v.action}</span>
      <span class="cn">${v.confidence!=null?v.confidence.toFixed(2):"--"}</span>
      <span class="cf">${v.conflicts||""}</span></div>`).join("");
  } else if (d.mode==="signal") { $("verdicts").innerHTML = '<div class="empty">未布防触发条件</div>'; }
  else { $("verdicts").innerHTML = '<div class="empty">无新决策</div>'; }
  // 雷达与解读
  if (d.radar_readings) radarReadings = d.radar_readings;
  if (d.radar){
    radarNames = d.radar.names||[]; radarAll = d.radar.all||{}; radarSym = d.radar.symbol||"";
    if (radarSelSym && radarAll[radarSelSym]) { radarSym = radarSelSym; }
    radarVals = radarAll[radarSym] || (d.radar.values||[]);
    $("radarSym").textContent = radarSym ? "// "+radarSym.replace('/USDT','') : "";
    drawRadar();
  }
  const activeSym = (radarSelSym && radarReadings[radarSelSym]) ? radarSelSym : (d.radar && d.radar.symbol) || "";
  renderTenReadings(activeSym);
  // 权益曲线
  if (d.equity_hist) eqHist = d.equity_hist;
  drawEq();
  // 交易记录
  renderTrades(d.trade_history);
  // 日志
  if (d.logs && d.logs !== lastLogs){
    const lb = $("log");
    const stick = lb.scrollTop + lb.clientHeight >= lb.scrollHeight - 20;
    lb.innerHTML = d.logs;
    if (stick) lb.scrollTop = lb.scrollHeight;
    lastLogs = d.logs;
  }
}

/* ---------- 排盘渲染（十项全量） ---------- */
let ppTab = "pp-qm", ppFull = null, ppFullAt = 0;
document.querySelectorAll(".pptab").forEach(t=>{
  t.addEventListener("click",()=>{
    document.querySelectorAll(".pptab").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    ppTab = t.dataset.p;
    renderPaipanFull();
  });
});
function fetchFullPaipan(){
  fetch("/api/paipan/full").then(r=>r.json()).then(d=>{ ppFull = d; renderPaipanFull(); }).catch(()=>{});
}
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* 奇门九宫：洛书布局（上南下北：巽离坤 / 震中兑 / 艮坎乾） */
const QM_ORDER = ["巽四宫","离九宫","坤二宫","震三宫","中五宫","兑七宫","艮八宫","坎一宫","乾六宫"];
function renderQimen(qm, scores){
  const gong = qm.jiu_gong || {};
  let html = `<div class="pphead"><span class="gz">${esc(qm.dun||"")}${esc(qm.ju||"")}局 · ${esc(qm.yuan||"")}</span>
    <span class="meta">值符 ${esc(qm.zhifu||"-")} · 值使 ${esc(qm.zhishi||"-")} · 旬空 ${esc(qm.xun_kong||"-")}</span></div>`;
  const hot = qm.timing_gan_palace, hotd = qm.day_gan_palace;
  html += '<div class="qmgrid">';
  for (const name of QM_ORDER){
    const c = gong[name] || {};
    const cls = name===hot ? " hot" : (name===hotd ? " hott" : "");
    const label = name.replace("宫","");
    if (name === "中五宫"){
      html += `<div class="qmcell${cls}"><div class="pos"><b>${label}</b><b>${esc(c.gan||"")}</b></div><div class="mid">寄坤二宫</div></div>`;
      continue;
    }
    html += `<div class="qmcell${cls}"><div class="pos"><b>${label}</b><b>${esc(c.gan||"")}</b></div>
      <div class="stars"><span class="god">${esc(c.god||"")}</span><span class="star">${esc(c.star||"")}</span></div>
      <div class="door">${esc(c.door||"")}</div>
      <div class="gans"><span class="tg">${esc(c.tian_gan||"")}</span><span class="dg">${esc(c.gan||"")}</span></div></div>`;
  }
  html += '</div>';
  html += `<div class="pphead" style="margin-top:2px"><span class="meta">${esc(qm.notes?qm.notes[2]||"":"")}</span></div>`;
  return html;
}

/* 六壬：天盘十二支环绕 + 四课 + 三传 */
const LR_ORDER = ["巳","午","未","申","酉","戌","亥","子","丑","寅","卯","辰"];
function renderLiuren(lr){
  const tp = lr.tianpan || {};
  let html = `<div class="pphead"><span class="gz">${esc(lr.yuejiang||"")}将 · ${esc(lr.ke_break||"")}</span>
    <span class="meta">贵人 ${esc(lr.jiang_shen||"-")} · 旬空 ${esc(lr.xun_kong||"-")}</span></div>`;
  html += '<div class="lrpan">';
  for (const z of LR_ORDER){
    const t = tp[z] || z;
    html += `<div class="lrcell${t!==z?" rot":""}"><span class="t">${esc(t)}</span><div class="b">${esc(z)}</div></div>`;
  }
  html += '</div>';
  const lessons = lr.four_lessons || [];
  const trans = lr.three_transmissions || [];
  html += '<div class="pprow">';
  html += `<div class="ppcard"><h4>四课</h4><div class="kv">${lessons.map(esc).join(" · ") || "-"}</div></div>`;
  html += `<div class="ppcard"><h4>三传</h4><div class="kv"><b>${trans.map(esc).join(" → ") || "-"}</b></div></div>`;
  html += '</div>';
  return html;
}

/* 八字四柱表 */
function renderBazi(bz, sz){
  const p = bz.pillars || {}, wx = bz.wuxing || {}, tg = bz.ten_gods || {}, hs = bz.hidden_stems || {}, ny = bz.nayin || {};
  let html = `<div class="pphead"><span class="gz">日主 ${esc(bz.day_master||"")}·${esc(bz.day_master_wuxing||"")}（${esc(bz.strength||"")}）</span>
    <span class="meta">用神 ${esc(bz.yong_shen||"-")}</span></div>`;
  html += '<div class="pprow">';
  for (const k of ["年","月","日","时"]){
    html += `<div class="ppcard"><h4>${k}柱 ${esc(p[k]||"")}</h4><div class="kv">
      五行 ${esc(wx[k]||"")}<br>十神 ${esc(tg[k]||"")}<br>藏干 ${(hs[k]||[]).map(esc).join(" ")||"-"}<br>${esc(ny[k]||"")}</div></div>`;
  }
  html += '</div>';
  if (sz && sz.palaces){
    const pc = sz.palaces;
    html += `<div class="ppcard" style="margin-top:4px"><h4>宫位</h4><div class="kv">${Object.entries(pc).map(([k,v])=>k+"："+esc(v)).join(" · ")}</div></div>`;
  }
  if (bz.da_yun && bz.da_yun.length){
    html += `<div class="ppcard" style="margin-top:4px"><h4>大运</h4><div class="kv">${bz.da_yun.slice(0,8).map(esc).join(" → ")}${bz.da_yun.length>8?" → …":""}</div>
      <div class="kv" style="color:var(--dim);font-size:9.5px">${esc(bz.qi_yun||"")}</div></div>`;
  }
  return html;
}

/* 三易：本卦→变卦六爻图 + 梅花体用 + 纳甲六亲 */
const YAO_NAMES = ["初","二","三","四","五","上"];
function hexYao(hex, dong){
  // hex 自下而上（index 0=初爻，1=阳）；渲染上爻在顶
  let out = "";
  for (let i=5;i>=0;i--){
    const yang = hex[i]==="1";
    const isD = dong && dong.includes(i+1);
    out += `<div class="yao${yang?"":" yin"}${isD?" dong":""}">${yang?'<span></span>':'<span></span><span></span>'}</div>`;
  }
  return out;
}
function renderYi(charts, scores){
  const yj = charts["易经"]||{}, mh = charts["梅花"]||{}, bg = charts["八卦"]||{};
  let html = "";
  html += `<div class="pphead"><span class="gz">${esc(yj.ben_gua||"")} → ${esc(yj.bian_gua||"")}</span>
    <span class="meta">动爻 ${(yj.dong_yao||[]).join(",")||"-"}爻 · 卦辞 ${esc((yj.gua_ci||"").slice(0,26))}</span></div>`;
  // 六爻图：左本卦右变卦（上爻在顶）
  html += '<div class="pprow">';
  html += `<div class="ppcard"><h4>本卦 ${esc(yj.ben_gua||"")}</h4><div class="hexagram">${hexYao(yj.ben_gua_hex||"", yj.dong_yao)}</div>
    <div class="kv" style="margin-top:3px">互卦 ${esc(yj.hu_gua||"-")}</div></div>`;
  html += `<div class="ppcard"><h4>变卦 ${esc(yj.bian_gua||"")}</h4><div class="hexagram">${hexYao(yj.bian_gua_hex||"", null)}</div></div>`;
  html += '</div>';
  if (mh.ben_gua){
    html += `<div class="ppcard" style="margin-top:4px"><h4>梅花易数</h4><div class="kv">体 ${esc(mh.ti_gua||"")} · 用 ${esc(mh.yong_gua||"")} → <b>${esc(mh.ti_yong_relation||"")}</b> · 互卦 ${esc(mh.hu_gua||"-")}</div></div>`;
  }
  if (bg.najia){
    const lq = bg.liuqin||[], nj = bg.najia||[];
    html += `<div class="ppcard" style="margin-top:4px"><h4>纳甲六爻（自上而下）</h4><div class="kv">`;
    for (let i=5;i>=0;i--){
      html += `${YAO_NAMES[i]}爻 ${esc(nj[i]||"")} · ${esc(lq[i]||"")}${(bg.dong_yao||[]).includes(i+1)?' <b style="color:var(--am)">动</b>':""}<br>`;
    }
    html += `世爻 ${esc(bg.shi_yao||"")} · 应爻 ${esc(bg.ying_yao||"")} · 卦宫 ${esc(bg.gua_gong||"")}</div></div>`;
  }
  return html;
}

/* 紫微十二宫环形布局（4x4 去中心 2x2） */
function renderZiwei(zw){
  const pal = zw.palaces || {};
  // 十二地支环形（寅起东北顺时针）→ 4x4 网格去中心 2x2：
  // 行0: 巳午未申 | 行1: 辰[空]酉 | 行2: 卯[空]戌 | 行3: 寅丑子亥
  const ring = ["寅","卯","辰","巳","午","未","申","酉","戌","亥","子","丑"];
  const cells = [3,4,5,6, 2,-1,7, 1,-1,8, 0,11,10,9]; // 索引 ring，-1 = 中央空位
  let html = `<div class="pphead"><span class="gz">命宫${esc(zw.ming_gong||"")} · 身宫${esc(zw.shen_gong||"")} · ${esc(zw.wuxing_ju||"")}</span>
    <span class="meta">四化：禄${esc((zw.four_hua||{}).禄||"")} 权${esc((zw.four_hua||{}).权||"")} 科${esc((zw.four_hua||{}).科||"")} 忌${esc((zw.four_hua||{}).忌||"")}</span></div>`;
  html += '<div class="zwgrid">';
  for (const ci of cells){
    if (ci < 0){ html += '<div></div>'; continue; }
    const zhi = ring[ci];
    // 地支→宫名（zwPalaceAt：与后端 palace_zhi 推导互逆）
    const pn = zwPalaceAt(zw, zhi);
    const stars = pal[pn] || [];
    const cls = pn==="命宫" ? " ming" : (stars.includes("紫微") ? " zw" : "");
    html += `<div class="zwcell${cls}"><div><span class="pn">${esc(pn)}</span><span class="pz">${esc(zhi)}</span></div>
      <div class="st">${stars.map(s=>`<em>${esc(s)}</em>`).join(" ") || "—"}</div></div>`;
  }
  html += '</div>';
  return html;
}
/* 紫微十二宫：命宫起逆时针（兄弟、夫妻…），地支由命宫地支起逆布 */
const ZW_PALACES = ["命宫","兄弟","夫妻","子女","财帛","疾厄","迁移","交友","官禄","田宅","福德","父母"];
function zwPalaceAt(zw, zhi){
  const mg = zw.ming_gong || "寅";
  // 地支→宫名：与后端 palace_zhi[name]=(命宫支-i)%12 互逆，i=(命宫支-该支) mod 12
  const ZHI = "子丑寅卯辰巳午未申酉戌亥";
  const off = (ZHI.indexOf(mg) - ZHI.indexOf(zhi) + 24) % 12;
  return ZW_PALACES[off] || "";
}

/* 太乙：计神落宫 + 三基五符 */
function renderTaiyi(ty){
  let html = `<div class="pphead"><span class="gz">太乙积年 ${esc(ty.taiyi_year||"")}</span>
    <span class="meta">${esc(ty.wenyun||"")}</span></div>`;
  html += '<div class="pprow">';
  html += `<div class="ppcard"><h4>计神落宫</h4><div class="kv">岁计 <b>${esc(ty.taiyi_gong||"")}</b><br>月计 ${esc(ty.yue_gong||"")}<br>日计 ${esc(ty.ri_gong||"")}</div></div>`;
  const sg = ty.sixteen_gods || {}, sj = ty.sanji || {}, wf = ty.wufu || {};
  html += `<div class="ppcard"><h4>三基五符</h4><div class="kv">${Object.entries(sj).map(([k,v])=>k+" "+esc(v)).join(" · ")||"-"}<br>${Object.entries(wf).map(([k,v])=>k+" "+esc(v)).join(" · ")||"-"}</div></div>`;
  html += '</div>';
  if (Object.keys(sg).length){
    html += `<div class="ppcard" style="margin-top:4px"><h4>十六神</h4><div class="kv" style="line-height:1.8">${Object.entries(sg).map(([k,v])=>esc(k)+"·"+esc(v)).join("  ")}</div></div>`;
  }
  return html;
}
/* 风水：玄空飞星九宫（洛书布局，年星/月星同盘） */
const FS_ORDER = ["巽宫","离宫","坤宫","震宫","中宫","兑宫","艮宫","坎宫","乾宫"];
function renderFengshui(fs){
  const fx = fs.fei_xing || {};
  let html = `<div class="pphead"><span class="gz">${esc(fs.yuan_yun||"")}</span>
    <span class="meta">年星 ${esc(fs.year_star)} 入中 · 月星 ${esc(fs.month_star)} 入中</span></div>`;
  html += '<div class="qmgrid">';
  for (const name of FS_ORDER){
    const c = fx[name] || {};
    const stars = [c.year, c.month].filter(v=>v!=null);
    html += `<div class="qmcell"><div class="pos"><b>${name.replace("宫","")}</b></div>
      <div class="gans">${stars.map(s=>`<span class="tg">${esc(s)}</span>`).join(" ")}</div></div>`;
  }
  html += '</div>';
  return html;
}

function renderPaipanFull(){
  const box = $("paipanBox"); if (!box) return;
  if (!ppFull || !ppFull.charts){ box.innerHTML = '<div class="empty">等待排盘…</div>'; return; }
  const charts = ppFull.charts, scores = ppFull.scores || {};
  let html = `<div class="pphead"><span class="gz">${esc(ppFull.ganzhi||"未排盘")}</span>
    <span class="meta">${esc(ppFull.lunar||"")} · ${esc(ppFull.jieqi||"")} · 旬空 ${esc(ppFull.xun_kong||"-")}</span></div>`;
  const errs = ppFull.errors || {};
  if (errs["_live"]){ html += `<div class="ppcard"><h4>排盘异常</h4><div class="err">${esc(errs["_live"])}</div></div>`; }
  try {
    if (ppTab === "pp-qm") html += renderQimen(charts["奇门"]||{}, scores);
    else if (ppTab === "pp-lr") html += renderLiuren(charts["六壬"]||{});
    else if (ppTab === "pp-bz") html += renderBazi(charts["八字"]||{}, charts["四柱"]||{});
    else if (ppTab === "pp-yi") html += renderYi(charts, scores);
    else if (ppTab === "pp-zw") html += renderZiwei(charts["紫微"]||{});
    else if (ppTab === "pp-ty") html += renderTaiyi(charts["太乙"]||{});
    else if (ppTab === "pp-fs") html += renderFengshui(charts["风水"]||{});
  } catch(e){ html += `<div class="ppcard"><h4>渲染异常</h4><div class="err">${esc(String(e))}</div></div>`; }
  const errList = Object.entries(errs).filter(([k])=>k!=="_live");
  if (errList.length){
    html += `<div class="ppcard" style="margin-top:4px"><h4>单盘异常</h4><div class="err">${errList.map(([k,v])=>esc(k)+": "+esc(v)).join("<br>")}</div></div>`;
  }
  box.innerHTML = html;
  $("ppTime").textContent = "// " + (ppFull.shichen || "");
}

/* 旧接口保留兼容（SSE 内轻量摘要时仍走这里） */
function renderPaipan(pp){
  const sc = pp && pp.shichen;
  if (sc && sc.name) $("ppTime").textContent = "// " + sc.name;
}

/* ---------- 时钟 + 时辰倒计时 ---------- */
let shichenRemain = 0, shichenName = "";
setInterval(()=>{
  const t = new Date(Date.now()+8*3600*1000); const p = n => String(n).padStart(2,"0");
  $("clock").textContent = p(t.getUTCHours())+":"+p(t.getUTCMinutes())+":"+p(t.getUTCSeconds());
  if (shichenRemain > 0){
    shichenRemain -= 1;
    const m = Math.floor(shichenRemain/60), s = shichenRemain%60;
    $("scCd").textContent = "寅卯更替 " + p(m) + ":" + p(s);
  }
}, 1000);

/* ---------- 区间切换 ---------- */
document.querySelectorAll("#dranges .rsym").forEach(el=>{
  el.onclick = ()=>{
    curRange = el.dataset.r;
    applyRange();
    document.querySelectorAll("#dranges .rsym").forEach(x=>x.classList.toggle("active", x===el));
  };
});

/* ---------- 数据流 ---------- */
fetch("/api/state").then(r=>r.json()).then(d=>{render(d); renderPaipan(d.paipan); syncShichen(d.paipan);}).catch(()=>{});
fetchFullPaipan();
setInterval(fetchFullPaipan, 60000);  // 排盘按时辰变化, 每分钟拉一次即可
const es = new EventSource("/api/stream");
es.onmessage = e => { try { const d = JSON.parse(e.data); render(d); renderPaipan(d.paipan); syncShichen(d.paipan); } catch(_){} };
es.onerror = () => {
  setLed("ledSys", "bad", "断线");
  setTimeout(()=>{ if (es.readyState === EventSource.CLOSED) location.reload(); }, 3000);
};
setInterval(()=>{ fetch("/api/state").then(r=>r.json()).then(d=>{render(d); renderPaipan(d.paipan); syncShichen(d.paipan);}).catch(()=>{}); }, 5000);

function syncShichen(pp){
  const sc = pp && pp.shichen;
  if (!sc || !sc.name) return;
  if (sc.name !== shichenName){
    shichenName = sc.name;
    $("scGz").textContent = sc.name;
  }
  shichenRemain = sc.remain_s || 0;
}
</script>
</body>
</html>
"""


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _tail(path: Path, n: int) -> List[str]:
    """取文件尾部 n 行：从文件末尾按块回扫，避免整读大日志。"""
    try:
        size = path.stat().st_size
        if size <= 0:
            return []
        chunk = 64 * 1024
        tail_bytes = b""
        with path.open("rb") as fh:
            remaining = size
            while remaining > 0 and tail_bytes.count(b"\n") <= n:
                read_size = min(chunk, remaining)
                remaining -= read_size
                fh.seek(remaining)
                tail_bytes = fh.read(read_size) + tail_bytes
        lines = tail_bytes.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


class Snapshot:
    def __init__(self, state_dir: Path, method_weights: Optional[Mapping[str, float]] = None):
        self.state_dir = Path(state_dir)
        # 十项古法历史权重（strategy.weights：历史地位 + 择时实战记录）。
        self.method_weights: Dict[str, float] = dict(
            method_weights or {
                "奇门": 0.17, "六壬": 0.15, "易经": 0.14, "八字": 0.11, "八卦": 0.10,
                "太乙": 0.08, "梅花": 0.08, "紫微": 0.07, "四柱": 0.06, "风水": 0.04,
            }
        )
        self.state_path = self.state_dir / "state.json"
        self.health_path = self.state_dir / "health.json"
        self.equity_path = self.state_dir / "equity.jsonl"
        self.orders_path = self.state_dir / "orders.audit.jsonl"
        self.log_path = self.state_dir / "gufa_quant.jsonl"
        self._log_cache: List[str] = []
        self._log_size = -1
        self._equity_cache: List[float] = []
        self._equity_size = -1
        self._equity_pts_cache: List[tuple] = []   # [(aware datetime, equity)]
        self._equity_pts_size = -1
        self._equity_pts_offset = 0

    # ---------- 带缓存的增量读取 ----------
    def _read_equity(self) -> List[float]:
        """权益曲线数据：复用 _read_equity_points 的增量缓存（9.0 性能修复）。"""
        size = self.equity_path.stat().st_size if self.equity_path.exists() else -1
        if size == self._equity_size and self._equity_cache:
            return self._equity_cache
        pts = self._read_equity_points()
        out = [eq for _dt, eq in pts][-EQUITY_MAX_POINTS:]
        self._equity_cache = out
        self._equity_size = size
        return out

    # ---------- 权益区间统计（今日/昨日/3天/7天，北京时区） ----------
    def _read_equity_points(self) -> List[tuple]:
        """增量读取 equity.jsonl → [(aware datetime, equity)]，按文件追加缓存。"""
        try:
            size = self.equity_path.stat().st_size
        except OSError:
            return self._equity_pts_cache
        if size == self._equity_pts_size and self._equity_pts_cache:
            return self._equity_pts_cache
        pts = self._equity_pts_cache
        if pts and size > self._equity_pts_offset:
            # 追加模式：只解析新增字节
            try:
                with self.equity_path.open("r", encoding="utf-8") as fh:
                    fh.seek(self._equity_pts_offset)
                    for line in fh:
                        try:
                            j = json.loads(line)
                            dt = datetime.fromisoformat(str(j.get("ts", "")))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            eq = float(j.get("equity", 0.0))
                            if eq > 0:
                                pts.append((dt, eq))
                        except Exception:
                            continue
                self._equity_pts_offset = size
                self._equity_pts_size = size
                return pts
            except Exception:
                pass
        # 全量重读（首次 / 文件轮转截断）
        pts = []
        try:
            for line in self.equity_path.read_text(encoding="utf-8").splitlines():
                try:
                    j = json.loads(line)
                    dt = datetime.fromisoformat(str(j.get("ts", "")))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    eq = float(j.get("equity", 0.0))
                    if eq > 0:
                        pts.append((dt, eq))
                except Exception:
                    continue
        except Exception:
            pass
        self._equity_pts_cache = pts
        self._equity_pts_offset = size
        self._equity_pts_size = size
        return pts

    @staticmethod
    def _window_stat(pts: List[tuple], start: datetime, end: datetime) -> Optional[Dict[str, float]]:
        """区间 [start, end) 权益统计：起始/结束权益、盈亏、收益率。

        9.0 性能修复：点集按时间有序，用 bisect 定位窗口（O(log n)），
        替代两次全表线性扫描（25 万点 × 8 窗口曾拖慢 build 到 ~4s）。
        """
        import bisect

        if not pts:
            return None
        times = [t for t, _e in pts]  # 有序：_read_equity_points 追加保证
        lo = bisect.bisect_left(times, start)
        hi = bisect.bisect_left(times, end)
        if hi > lo:
            start_eq = pts[lo][1]
            end_eq = pts[hi - 1][1]
        elif lo > 0:
            start_eq = end_eq = pts[lo - 1][1]
        else:
            return None
        pnl = end_eq - start_eq
        pnl_pct = (pnl / start_eq * 100.0) if start_eq else 0.0
        return {"start_eq": start_eq, "end_eq": end_eq, "pnl": pnl, "pnl_pct": pnl_pct}

    def _equity_windows(self) -> Dict[str, Dict[str, Any]]:
        """按北京时区计算 今日/昨日/3天/7天 权益区间表现 + 较前期对比（百分点）。"""
        pts = self._read_equity_points()
        now = datetime.now(LOCAL_TZ)
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        d = timedelta(days=1)
        specs = [
            ("today", today0, now, "今日", "今日初", "当前", "较前1日"),
            ("yday",  today0 - d, today0, "昨日", "昨日起", "昨日末", "较前1日"),
            ("3d",    today0 - 3 * d, now, "3天", "3日前", "当前", "较前3天"),
            ("7d",    today0 - 7 * d, now, "7天", "7日前", "当前", "较前7天"),
        ]
        prev_specs = {
            "today": (today0 - d, today0),
            "yday":  (today0 - 2 * d, today0 - d),
            "3d":    (today0 - 6 * d, today0 - 3 * d),
            "7d":    (today0 - 14 * d, today0 - 7 * d),
        }
        out: Dict[str, Dict[str, Any]] = {}
        for key, start, end, label, sl, el, cl in specs:
            st = self._window_stat(pts, start, end)
            prev = self._window_stat(pts, *prev_specs[key])
            item: Dict[str, Any] = {
                "label": label, "start_label": sl, "end_label": el, "cmp_label": cl,
                "start_eq": None, "end_eq": None, "pnl": None, "pnl_pct": None, "cmp_pp": None,
            }
            if st:
                item.update(st)
                if prev:
                    item["cmp_pp"] = st["pnl_pct"] - prev["pnl_pct"]
            out[key] = item
        return out

    def _trade_history(self, limit: int = 40) -> List[Dict[str, Any]]:
        """AI 历史买卖动作：trigger_entry(买入) / trigger_exit(卖出) / order_fill(成交)。"""
        def _num(v: Any, default: float = 0.0) -> float:
            try:
                return float(v or default)
            except (TypeError, ValueError):
                return default
        out: List[Dict[str, Any]] = []
        for line in reversed(_tail(self.orders_path, 500)):
            try:
                o = json.loads(line)
                ev = str(o.get("event", ""))
                if ev not in TRADE_EVENTS:
                    continue
                is_canceled = ev in ("trigger_entry_skip", "depth_blocked")
                plan = o.get("plan") or {}
                sym = plan.get("symbol") or o.get("symbol") or ""
                if not sym:
                    continue
                side = str(plan.get("side") or o.get("side")
                           or ("sell" if ev == "trigger_exit" else "buy"))
                amount = _num(plan.get("amount"))
                price = _num(plan.get("reference_price") or o.get("reference_price") or o.get("price"))
                value = _num(plan.get("estimated_quote"))
                cond = o.get("condition")
                note = str(cond.get("note", "")) if isinstance(cond, dict) else ""
                if is_canceled:
                    note = str(o.get("reason") or o.get("error") or note)[:80]
                elif not note:
                    note = str(plan.get("reason") or o.get("reason") or "")[:80]
                if len(note) > 70:
                    note = note[:70] + "…"
                label = TRADE_EVENTS[ev]
                # trigger_entry 但订单被取消/零成交 → 不算“买入”
                if ev == "trigger_entry":
                    fill = o.get("fill") if isinstance(o.get("fill"), dict) else None
                    if fill and (fill.get("status") == "canceled" or float(fill.get("filled_amount", 0) or 0) <= 0):
                        label = "✕ 未成"
                        is_canceled = True
                out.append({
                    "ts": str(o.get("ts", "")),
                    "time": _local_hhmmss(str(o.get("ts", ""))),
                    "date": _local_mmdd(str(o.get("ts", ""))),
                    "label": label,
                    "sym": sym,
                    "side": side,
                    "amount": amount,
                    "price": price,
                    "value": value,
                    "note": note,
                    "canceled": is_canceled,
                })
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    # ---------- AI 步骤解析（最近一个周期内） ----------
    def _ai_steps_and_status(self, health: Dict[str, Any], mode: str = "") -> Dict[str, Any]:
        """解析 AI 十项解读的每步状态。

        周期模式：以「最近周期完成之后」的日志为准，统计拆分解读失败；
        信号模式：无周期概念，直接按日志尾部统计 AI-2 入场决策成败 /
                  AI-3 出场监控 / 空响应重试，反映真实 AI 健康度。
        """
        methods = ["奇门", "六壬", "太乙", "易经", "风水", "八字", "梅花", "紫微", "八卦", "四柱"]
        failed: set[str] = set()
        agg_failed = False
        relay_error = False

        # ---- 信号模式：真实统计 ----
        if mode == "signal":
            ok1 = fail1 = empty = 0
            for line in _tail(self.log_path, 300):
                try:
                    j = json.loads(line)
                    msg = str(j.get("message", ""))
                except Exception:
                    continue
                if "AI-2 入场决策失败" in msg:
                    fail1 += 1
                elif "AI-2 入场决策" in msg and "decision=" in msg:
                    ok1 += 1
                elif "AI 响应为空或请求失败" in msg or "AI 中转站错误" in msg:
                    empty += 1
            total = ok1 + fail1
            if total == 0:
                status = "idle"
            elif fail1 > 0 and ok1 == 0:
                status = "down"
            elif fail1 > 0:
                status = "degraded"
            else:
                status = "ready"
            steps = [
                {"name": f"AI-2 古法入场 · 成功 {ok1} · 失败 {fail1}",
                 "status": "ok" if ok1 >= fail1 else "error"},
                {"name": "AI-3 出场条件监控 · 就绪", "status": "ok"},
                {"name": "AI 请求正常" if empty == 0 else f"AI 空响应/中转站错误 {empty} 次",
                 "status": "ok" if empty == 0 else "fallback"},
            ]
            return {"status": status, "steps": steps,
                    "agg_failed": False, "failed": sorted(failed)}

        lines = _tail(self.log_path, 200)
        # 从尾部往前收集最近一个周期内的 AI 失败。
        # 周期完成/失败日志是边界标记：跳过第一个（最近周期的结尾），
        # 遇到第二个边界（最近周期的开头）才停止。
        boundary_seen = 0
        for line in reversed(lines):
            try:
                j = json.loads(line)
                msg = str(j.get("message", ""))
                lvl = str(j.get("level", ""))
            except Exception:
                continue
            if "周期完成" in msg or "周期失败" in msg or "运行周期失败" in msg:
                boundary_seen += 1
                if boundary_seen >= 2:
                    break
                continue
            if "AI 拆分解读「" in msg and "失败" in msg:
                m = re.search(r"AI 拆分解读「([^」]+)」失败", msg)
                if m:
                    failed.add(m.group(1))
            elif "AI 拆分聚合决策失败" in msg:
                agg_failed = True
            elif "AI 十项古法解读失败" in msg or ("AI 中转站错误" in msg and lvl == "ERROR"):
                relay_error = True

        # 结合 health.decisions 的 fallback 标志（最近已完成周期的结果）
        decisions = (health.get("decisions") or {})
        fb_syms = 0
        total_syms = 0
        for sym, info in decisions.items():
            ai_info = info.get("ai") or {}
            if ai_info.get("fallback"):
                fb_syms += 1
            total_syms += 1

        steps = [{"name": n, "status": "fallback" if n in failed else "ok"} for n in methods]
        if relay_error:
            status = "down"
        elif failed or agg_failed:
            status = "degraded"
        elif total_syms and fb_syms == total_syms:
            status = "degraded"  # 最近周期所有决策都是规则兜底
        elif fb_syms and fb_syms < total_syms:
            status = "degraded"
        else:
            status = "ready"
        return {"status": status, "steps": steps, "agg_failed": agg_failed, "failed": sorted(failed)}

    def _read_logs_html(self) -> str:
        """只显示关键步骤摘要（不滚完整日志）：
        周期完成/失败、AI 解读步骤、成交、初选、剔除、网络错误。"""
        size = self.log_path.stat().st_size if self.log_path.exists() else -1
        if size == self._log_size and self._log_cache:
            return "".join(self._log_cache)
        rows: List[str] = []
        dedup_seen: set[str] = set()
        for line in _tail(self.log_path, LOG_TAIL):
            try:
                j = json.loads(line)
                ts = _local_hhmmss(str(j.get("ts", "")))
                lvl = str(j.get("level", ""))
                msg = str(j.get("message", ""))
            except Exception:
                continue
            # 屏蔽心跳刷屏（实时日志只看关键动作）
            if "心跳" in msg:
                continue
            if not self._is_key_event(msg):
                continue
            # 高频刷屏事件合并：AI 错误/空响应/决策失败只保留最新 1 条，
            # 避免日志卡被上游 503 刷屏，挤掉成交/布防等关键事件。
            if ("AI 响应为空或请求失败" in msg or "AI 中转站错误" in msg
                    or "AI-2 入场决策失败" in msg):
                key = "AI_ERR"
                if key in dedup_seen:
                    continue
                dedup_seen.add(key)
            if len(msg) > 130:
                msg = msg[:130] + "…"
            cls = lvl if lvl in ("INFO", "WARNING", "ERROR") else ""
            if "BUY" in msg or "买入" in msg:
                cls = "BUY"
            if "SELL" in msg or "卖出" in msg:
                cls = "SELL"
            rows.append(f'<div><span class="t">[{ts}]</span> <span class="{cls}">{msg}</span></div>')
        rows = rows[-24:]
        self._log_cache = rows
        self._log_size = size
        return "".join(rows)

    @staticmethod
    def _is_key_event(msg: str) -> bool:
        """判断是否关键步骤事件（过滤网络错误刷屏/普通 INFO）。"""
        if any(k in msg for k in (
            "周期完成", "周期失败", "运行周期失败", "交易所已连接",
            "每日古法初选完成", "名字初筛", "无有效行情，当日剔除",
            "AI 拆分解读", "AI 拆分聚合决策失败", "AI 十项古法解读失败",
            "AI 聚合决策", "首次响应结构无效",
            "BUY filled", "SELL filled", "买入", "卖出", "下单",
            "订单", "成交", "仓位", "止损", "止盈",
            # 信号模式关键事件
            "信号触发模式启动", "AI-2 入场决策", "AI-3 出场决策", "AI-2 入场决策失败", "AI-3 出场决策失败", "AI-2 响应结构无效", "AI-3 响应结构无效", "AI-3 补设", "AI-1 断卦", "今日不宜交易",
            "AI 响应为空或请求失败", "AI 中转站错误", "布防", "触发条件",
            "AI 缩量仲裁", "AI 判定放弃", "AI 缩量后仍失败",
        )):
            return True
        # 网络错误合并显示：只保留每种错误最近 1 条
        if "load_markets 网络错误" in msg or "NetworkError" in msg:
            return True
        return False

    # ---------- 聚合快照 ----------
    def build(self) -> Dict[str, Any]:
        state = _load_json(self.state_path) or {}
        health = _load_json(self.health_path) or {}
        equity_hist = self._read_equity()

        # 网络状态：按时间戳判断——最新的成功事件(连接/周期完成/启动) vs 最新网络错误
        net_ok = True
        last_err_ts: Optional[str] = None
        last_ok_ts: Optional[str] = None
        for line in reversed(_tail(self.log_path, 40)):
            try:
                j = json.loads(line)
                ts = str(j.get("ts", ""))
                msg = str(j.get("message", ""))
            except Exception:
                continue
            if "load_markets 网络错误" in msg or "NetworkError" in msg:
                if last_err_ts is None:
                    last_err_ts = ts
            elif ("交易所已连接" in msg or "周期完成" in msg or "启动" in msg) and last_ok_ts is None:
                last_ok_ts = ts
            if last_err_ts is not None and last_ok_ts is not None:
                break
        if last_err_ts and last_ok_ts:
            net_ok = last_ok_ts > last_err_ts
        elif last_err_ts:
            net_ok = False
        ai = self._ai_steps_and_status(health, health.get("mode", ""))
        ai_ok = ai["status"] == "ready"
        ai_busy = False

        # 选股
        scores: Dict[str, float] = state.get("daily_selection_scores") or {}
        picks = []
        for sym in (state.get("daily_selected_symbols") or [])[:12]:
            sc = scores.get(sym, 0.0)
            picks.append({"sym": sym, "score": sc, "action": self._action_for(sc)})
        picks.sort(key=lambda p: p["score"], reverse=True)

        dead = state.get("daily_selection_dead") or {}

        # 决策（health.decisions: {sym: {ai: {...}}})
        verdicts = []
        decisions = health.get("decisions") or {}
        for sym, info in decisions.items():
            ai_info = info.get("ai") or {}
            verdicts.append({
                "sym": sym,
                "action": ai_info.get("action", "?"),
                "confidence": ai_info.get("confidence"),
                "conflicts": (ai_info.get("conflicts") or [""])[0] if ai_info.get("conflicts") else "",
            })
        verdicts.sort(key=lambda v: v["sym"])

        # 持仓明细：state.json 真实持仓 + quotes 实时价 → 市值/浮动盈亏
        positions = []
        state_positions = state.get("positions") or {}
        quotes_map = health.get("quotes") or {}
        for sym, info in state_positions.items():
            if not isinstance(info, dict):
                continue
            try:
                amount = float(info.get("amount") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            try:
                avg = float(info.get("avg_entry") or 0.0)
            except (TypeError, ValueError):
                avg = 0.0
            try:
                price = float((quotes_map.get(sym) or {}).get("price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            value = price * amount
            pnl = (price - avg) * amount if avg > 0 else 0.0
            pnl_pct = (price / avg - 1) * 100 if avg > 0 else 0.0
            positions.append({
                "sym": sym,
                "amount": amount,
                "avg": avg,
                "price": price,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "side": info.get("side", "long"),
            })
        positions.sort(key=lambda p: p["value"], reverse=True)

        # 触发布防（信号模式）：health.triggers: {sym: {entry_conditions, exit_conditions,
        # entry_target, first_trigger_at, ref_price}} → 前端直接渲染列表。
        triggers = []
        for sym, info in (health.get("triggers") or {}).items():
            if not isinstance(info, dict):
                continue
            triggers.append({
                "sym": sym,
                "entry_n": int(info.get("entry_conditions") or 0),
                "exit_n": int(info.get("exit_conditions") or 0),
                "target": info.get("entry_target"),
                "first_at": _local_hhmmss(str(info.get("first_trigger_at") or "")),
                "ref": info.get("ref_price"),
            })
        triggers.sort(key=lambda t: t["sym"])

        # 信号模式：idle（刚启动尚未决策）且已有布防 → 视为就绪；否则以真实统计为准。
        if health.get("mode") == "signal" and ai["status"] == "idle" and triggers:
            ai["status"] = "ready"
            ai_ok = True
            ai_busy = False

        # 雷达：始终显示十项古法（奇门/六壬/太乙/易经/风水/八字/梅花/紫微/八卦/四柱）。
        # 按持仓币种读取 readings（health.decisions = {sym: {readings: {...}}}），
        # 把所有持仓币的 readings 都传给前端，雷达可点击切换币种查看。
        TEN_METHODS = ["奇门", "六壬", "太乙", "易经", "风水", "八字", "梅花", "紫微", "八卦", "四柱"]
        pos_syms = list((state.get("positions") or {}).keys())
        dec = health.get("decisions") or {}
        # 每个持仓币的十项 confidence
        radar_all: Dict[str, List[float]] = {}
        for sym in pos_syms:
            info = dec.get(sym)
            if isinstance(info, dict):
                rd = info.get("readings")
                if isinstance(rd, dict) and rd:
                    # 显示层归一化：轴值 = (权重 × 方向) / 最大权重。
                    # 直接画权重会让所有轴只有半径的 ~17%（权重最大 0.17），
                    # 雷达图缩成一团；归一化后最大权重轴满格，权重比例不变。
                    max_w = max((float(self.method_weights.get(n, 0.0)) for n in TEN_METHODS), default=0.0) or 1.0
                    vals = [0.0] * 10
                    for i, name in enumerate(TEN_METHODS):
                        item = rd.get(name)
                        if isinstance(item, dict):
                            try:
                                b = str(item.get("bias") or "neutral").lower()
                                # 历史权重 × 方向（不再用 LLM 自报 confidence）：
                                # 权重来自 strategy.weights（历史地位/实战记录），
                                # 奇门/六壬等主用之术轴长，风水等短轴，权重差异一目了然。
                                w = float(self.method_weights.get(name, 0.0))
                                s = 1.0 if b == "bullish" else (-1.0 if b == "bearish" else 0.0)
                                vals[i] = w * s / max_w
                            except Exception:
                                pass
                    radar_all[sym] = vals
        # 默认展示第一个有 readings 的币
        radar_symbol: str = ""
        radar_values: List[float] = [0.0] * 10
        for sym in pos_syms:
            if sym in radar_all:
                radar_symbol = sym
                radar_values = radar_all[sym]
                break

        # 十项解读全文（供"AI 十项解读"卡片展示，与雷达币种联动）：
        # {sym: {方法名: {bias, confidence, reading}}}，缺项留空 dict。
        radar_readings: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for sym in pos_syms:
            info = dec.get(sym)
            if isinstance(info, dict):
                rd = info.get("readings")
                if isinstance(rd, dict):
                    radar_readings[sym] = {
                        k: {**v, "weight": float(self.method_weights.get(k, 0.0))}
                        for k, v in rd.items() if isinstance(v, dict)
                    }

        # 订单审计尾部（最近 3 条）
        orders = []
        for line in _tail(self.orders_path, 3):
            try:
                o = json.loads(line)
                plan = o.get("plan") or {}
                orders.append({
                    "event": o.get("event", ""),
                    "sym": plan.get("symbol", ""),
                    "side": plan.get("side", ""),
                    "ts": _local_hhmmss(str(o.get("ts", ""))),
                })
            except Exception:
                continue

        return {
            "ts": time.time(),
            "status": health.get("status") or state.get("halted_reason") and "halted" or "unknown",
            "mode": health.get("mode", ""),
            "sandbox": health.get("sandbox", True),
            "equity": health.get("equity"),
            "peak": state.get("peak_equity"),
            "day_start": state.get("day_start_equity"),
            "trades_today": state.get("trades_today"),
            "cycle_seconds": health.get("cycle_seconds"),
            "next_review_seconds": health.get("next_review_seconds"),
            "fills": health.get("fills"),
            "position_count": len(state.get("positions") or {}),
            "quotes": health.get("quotes") or {},
            "live_updated_at": _local_iso(str(health.get("live_updated_at") or "")),
            "candidates": len((health.get("daily_selection") or {}).get("candidates") or [])
                          or len(state.get("daily_selection_candidates") or []),
            "selection_date": state.get("daily_selection_date"),
            "picks": picks,
            "dead": dead,
            "verdicts": verdicts,
            "positions": positions,
            "triggers": triggers,
            "radar": {"names": list(TEN_METHODS), "values": radar_values, "symbol": radar_symbol, "all": radar_all},
            "radar_readings": radar_readings,
            "equity_hist": equity_hist,
            "equity_windows": self._equity_windows(),
            "trade_history": self._trade_history(40),
            "logs": self._read_logs_html(),
            "orders": orders,
            "net_ok": net_ok,
            "ai_ok": ai_ok,
            "ai_busy": ai_busy,
            "ai_status": ai["status"],
            "ai_steps": ai["steps"],
            "ai_agg_failed": ai["agg_failed"],
            "paipan": self._paipan_snapshot(),
        }

    @staticmethod
    def _action_for(score: float) -> str:
        if score >= 0.55:
            return "FULL"
        if score >= 0.47:
            return "HALF"
        return "WATCH"

    def _paipan_snapshot(self) -> dict:
        """当前时辰十项古法排盘摘要（8.9 排盘标签页数据源）。

        读 runtime/paipan_audit.jsonl 最后一行（审计日志）＋实时排盘。
        主进程运行时 audit 每 tick 更新；离线时退化为静态展示。
        """
        try:
            lines = self._tail_jsonl(Path("runtime") / "paipan_audit.jsonl", 1)
            last = lines[-1] if lines else {}
        except Exception:  # noqa: BLE001
            last = {}
        # 9.0：当前时辰与下一时辰切换时刻（供顶栏时辰卡/倒计时）
        shichen = self._shichen_info()
        return {
            "updated": last.get("ts", ""),
            "symbol": last.get("symbol", ""),
            "ganzhi": last.get("ganzhi", ""),
            "jieqi": last.get("jieqi", ""),
            "qimen": last.get("qimen", {}),
            "liuren": last.get("liuren", {}),
            "bazi": last.get("bazi", {}),
            "scores": last.get("scores", {}),
            "errors": last.get("errors", {}),
            "shichen": shichen,
        }

    @staticmethod
    def _shichen_info() -> dict:
        """当前时辰（干支+名）与下一时辰切换倒计时秒数。

        时辰按东八区地方时划分：23:00-01:00 子时，此后每 2 小时一辰。
        """
        try:
            from datetime import datetime, timedelta, timezone

            from lunar_python import Solar

            tz8 = timezone(timedelta(hours=8))
            now = datetime.now(tz8)
            # 时柱直接取 lunar_python（23 点起按次日子时，与排盘主程序一致）
            lunar = Solar.fromYmdHms(now.year, now.month, now.day,
                                     now.hour, now.minute, now.second).getLunar()
            time_gz = lunar.getTimeInGanZhi()
            zhi = time_gz[1]
            # 下一时辰切换：奇数整点（1,3,5...）为时辰界（子时跨 23-1）
            eff = now + timedelta(hours=1)
            slot_start = eff.replace(minute=0, second=0, microsecond=0) - timedelta(hours=eff.hour % 2)
            next_at = slot_start + timedelta(hours=1)
            remain = int((next_at - now).total_seconds())
            return {"name": time_gz + "时", "zhi": zhi, "remain_s": max(0, remain),
                    "next_at": next_at.isoformat(timespec="minutes")}
        except Exception:  # noqa: BLE001
            return {"name": "", "zhi": "", "remain_s": 0, "next_at": ""}

    @staticmethod
    def _tail_jsonl(path: Path, n: int) -> list:
        try:
            if not path.exists():
                return []
            data = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            out = []
            for line in data[-n:]:
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
            return out
        except Exception:  # noqa: BLE001
            return []


class LivePaipan:
    """大屏独立实时排盘器（不拉交易主引擎）。

    直接复用 gufa_calendar + 十项排盘器子模块；只排当前时辰的时空盘，
    结果按（日柱+时柱）缓存，时辰内复用。十项全排实测 ~10ms。
    """

    def __init__(self, paipan_cfg: Optional[Mapping[str, Any]] = None):
        c = dict(paipan_cfg or {})
        self.config = SimpleNamespace(
            enabled=bool(c.get("enabled", True)),
            true_solar_time=bool(c.get("true_solar_time", True)),
            longitude=float(c.get("longitude", 116.4074)),
            latitude=float(c.get("latitude", 39.9042)),
            listing_time_source=str(c.get("listing_time_source", "ohlcv")),
            listing_times=dict(c.get("listing_times") or {}),
            exchange_timezone=str(c.get("exchange_timezone", "UTC")),
        )
        self._cal = None
        self._panzers = None
        self._cache_key = ""
        self._cache: Dict[str, Any] = {}
        self._cache_at = 0.0

    def _ensure(self):
        """懒加载排盘器（首帧后才 import，避免影响启动速度）。"""
        if self._panzers is not None:
            return
        from gufa_calendar import CalendarService
        from gufa_paipan_bazi import BaziPaipan, SizhuPaipan
        from gufa_paipan_liuren import LiurenPaipan
        from gufa_paipan_qimen import QimenPaipan
        from gufa_paipan_taiyi import TaiyiPaipan
        from gufa_paipan_yijing import BaguaPaipan, FengshuiPaipan, MeihuaPaipan, YijingPaipan
        from gufa_paipan_ziwei import ZiweiPaipan

        self._cal = CalendarService(self.config)
        self._panzers = [
            QimenPaipan(), LiurenPaipan(), TaiyiPaipan(), YijingPaipan(),
            FengshuiPaipan(), BaziPaipan(), MeihuaPaipan(), ZiweiPaipan(),
            BaguaPaipan(), SizhuPaipan(),
        ]

    def snapshot(self) -> Dict[str, Any]:
        """当前时辰十项时空盘完整数据（含历法元信息）。"""
        try:
            self._ensure()
            ctx = self._cal.context(datetime.now(timezone.utc))
            key = ctx.day_gz + ctx.time_gz
            now = time.time()
            if key != self._cache_key:
                charts: Dict[str, Any] = {}
                errors: Dict[str, str] = {}
                for p in self._panzers:
                    try:
                        charts[p.method] = p.current(ctx).to_dict()
                    except Exception as exc:  # noqa: BLE001
                        charts[p.method] = {"error": f"{type(exc).__name__}: {exc}"}
                        errors[p.method] = str(exc)
                # 实时十盘断卦分（确定性规则，与主引擎同一套）
                scores: Dict[str, float] = {}
                try:
                    from gufa_paipan_signal import paipan_signals
                    scores = paipan_signals({"current": charts, "natal": {}})
                except Exception:  # noqa: BLE001
                    pass
                self._cache = {
                    "ganzhi": ctx.ganzhi_full,
                    "lunar": ctx.lunar_text,
                    "jieqi": ctx.jieqi,
                    "next_jieqi": ctx.next_jieqi,
                    "xun_kong": ctx.xun_kong,
                    "shichen": ctx.shichen_name,
                    "solar_iso": ctx.solar_iso,
                    "charts": charts,
                    "scores": scores,
                    "errors": errors,
                }
                self._cache_key = key
                self._cache_at = now
            return self._cache
        except Exception as exc:  # noqa: BLE001
            return {"charts": {}, "scores": {}, "errors": {"_live": str(exc)},
                    "ganzhi": "", "lunar": "", "jieqi": None,
                    "next_jieqi": None, "xun_kong": None, "shichen": "",
                    "solar_iso": ""}


class Handler(BaseHTTPRequestHandler):
    snap: Snapshot = None  # type: ignore[assignment]
    live: LivePaipan = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(self.snap.build()).encode("utf-8"), "application/json; charset=utf-8")
            elif path == "/api/stream":
                self._stream()
            elif path == "/api/paipan":
                self._send(200, json.dumps(self.snap._paipan_snapshot()).encode("utf-8"), "application/json; charset=utf-8")
            elif path == "/api/paipan/full":
                self._send(200, json.dumps(self.live.snapshot(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_sig = ""
        while True:
            try:
                data = self.snap.build()
                sig = str(data.get("ts", 0))
                if sig != last_sig or True:
                    self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_sig = sig
                time.sleep(POLL_INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                time.sleep(POLL_INTERVAL)


def main() -> int:
    ap = argparse.ArgumentParser(description="GuFaQuant-Pro 科幻实时监控大屏")
    ap.add_argument("--config", default="config.json", help="config.json 路径")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8601)
    args = ap.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[{APP_NAME}] 无法读取配置 {cfg_path}: {exc}", file=sys.stderr)
        return 1
    state_dir = Path(cfg.get("runtime", {}).get("state_dir") or "runtime").expanduser().resolve()
    if not state_dir.exists():
        print(f"[{APP_NAME}] state_dir 不存在: {state_dir}", file=sys.stderr)
        return 1

    Handler.snap = Snapshot(state_dir, method_weights=cfg.get("strategy", {}).get("weights") or {})
    Handler.live = LivePaipan(cfg.get("paipan") or {})
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{APP_NAME}] 科幻监控大屏已启动: http://{args.host}:{args.port}  (数据源: {state_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{APP_NAME}] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
