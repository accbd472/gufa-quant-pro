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
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_NAME = "GuFaQuant-DASHBOARD"
POLL_INTERVAL = 2.0          # SSE 推送间隔（秒）
EQUITY_MAX_POINTS = 720      # 权益曲线最大点数（2s 采样 → 24h）
LOG_TAIL = 120               # 日志尾部行数

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
<title>GUFA QUANT PROTOCOL // 实时监控</title>
<style>
  :root {
    --cy: #00f0ff; --pu: #a855f7; --gr: #34ffb0; --rd: #ff3b6b; --am: #ffd166;
    --bg: #05070f; --panel: rgba(10, 18, 34, .72); --line: rgba(0, 240, 255, .25);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    background:
      radial-gradient(1200px 700px at 75% -10%, rgba(168, 85, 247, .14), transparent 60%),
      radial-gradient(900px 600px at 10% 110%, rgba(0, 240, 255, .10), transparent 60%),
      var(--bg);
    color: #d7e6ff; font-family: "Consolas", "JetBrains Mono", "Microsoft YaHei", monospace;
    overflow: hidden;
  }
  /* 网格 */
  .grid { position: fixed; inset: 0; pointer-events: none; opacity: .55;
    background-image:
      linear-gradient(rgba(0,240,255,.05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,240,255,.05) 1px, transparent 1px);
    background-size: 42px 42px; z-index: 0; }
  /* 扫描线 */
  .scan { position: fixed; left: 0; right: 0; height: 140px; z-index: 1; pointer-events: none;
    background: linear-gradient(180deg, transparent, rgba(0,240,255,.05), transparent);
    animation: scan 7s linear infinite; }
  @keyframes scan { 0% { top: -160px; } 100% { top: 110%; } }
  /* 粒子 */
  #stars { position: fixed; inset: 0; z-index: 0; pointer-events: none; }
  #app { position: relative; z-index: 2; height: 100vh; display: flex; flex-direction: column; padding: 10px 14px 6px; gap: 8px; }

  /* ---------- 顶栏 ---------- */
  header { display: flex; align-items: center; gap: 14px; padding: 6px 14px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 0 24px rgba(0,240,255,.08) inset, 0 0 18px rgba(0,240,255,.06); }
  .logo { font-size: 20px; font-weight: 700; letter-spacing: 3px; color: var(--cy);
    text-shadow: 0 0 12px rgba(0,240,255,.8); white-space: nowrap; }
  .logo small { font-size: 11px; color: var(--pu); letter-spacing: 2px; display: block; margin-top: 2px;
    text-shadow: 0 0 8px rgba(168,85,247,.8); }
  .st { display: flex; align-items: center; gap: 8px; font-size: 12px; letter-spacing: 1px; }
  .led { width: 10px; height: 10px; border-radius: 50%; background: var(--am);
    box-shadow: 0 0 10px var(--am); animation: pulse 1.6s infinite; }
  .led.ok { background: var(--gr); box-shadow: 0 0 12px var(--gr); }
  .led.warn { background: var(--am); box-shadow: 0 0 12px var(--am); }
  .led.busy { background: var(--cy); box-shadow: 0 0 12px var(--cy); }
  .led.bad { background: var(--rd); box-shadow: 0 0 12px var(--rd); animation: pulse .6s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  .sp { flex: 1; }
  .clock { font-size: 14px; color: var(--cy); letter-spacing: 2px; text-shadow: 0 0 8px rgba(0,240,255,.6); }
  .tag { font-size: 11px; padding: 3px 9px; border-radius: 20px; border: 1px solid var(--line); color: var(--am); }
  .tag.sb { color: var(--pu); border-color: rgba(168,85,247,.5); }

  /* ---------- 布局 ---------- */
  main { flex: 1; display: grid; grid-template-columns: 1.55fr 1fr; grid-template-rows: 0.9fr 1.35fr .75fr; gap: 8px; min-height: 0; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
    display: flex; flex-direction: column; min-height: 0; position: relative; overflow: hidden;
    backdrop-filter: blur(6px); }
  .card::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--cy), transparent); opacity: .6; }
  .card h3 { font-size: 10px; letter-spacing: 2px; color: var(--pu); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
  .card h3::after { content: ""; flex: 1; height: 1px; background: linear-gradient(90deg, var(--line), transparent); }
  .chart { flex: 1; min-height: 0; }
  canvas { width: 100%; height: 100%; display: block; }

  /* ---------- 指标卡 ---------- */
  .metrics { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; }
  .m { text-align: center; padding: 3px 2px; border: 1px solid var(--line); border-radius: 6px;
    background: rgba(0, 20, 40, .4); }
  .m .v { font-size: 15px; color: var(--cy); text-shadow: 0 0 10px rgba(0,240,255,.5); letter-spacing: 1px; }
  .m .l { font-size: 9px; color: #8fb4d8; margin-top: 2px; letter-spacing: 1px; }
  .m .v.green { color: var(--gr); text-shadow: 0 0 10px rgba(52,255,176,.5); }
  .m .v.red { color: var(--rd); text-shadow: 0 0 10px rgba(255,59,107,.5); }
  .m .v.amber { color: var(--am); }

  /* ---------- 选股分数条 ---------- */
  .picks { flex: 1; display: flex; flex-direction: column; gap: 7px; overflow-y: auto; }
  .pick { display: flex; align-items: center; gap: 10px; font-size: 12px; }
  .pick .sym { width: 92px; color: var(--cy); letter-spacing: 1px; white-space: nowrap; }
  .pick .bar { flex: 1; height: 12px; background: rgba(0,240,255,.08); border: 1px solid rgba(0,240,255,.2); border-radius: 4px; position: relative; overflow: hidden; }
  .pick .fill { height: 100%; background: linear-gradient(90deg, rgba(0,240,255,.25), var(--cy));
    box-shadow: 0 0 8px rgba(0,240,255,.6); transition: width .8s ease; }
  .pick .sc { width: 46px; text-align: right; color: var(--gr); }
  .pick .st2 { width: 64px; text-align: right; font-size: 10px; color: #8fb4d8; }
  .dead { color: var(--rd); font-size: 11px; opacity: .85; }
  .dead b { color: var(--rd); }

  /* ---------- 日志 ---------- */
  .logbox { flex: 1; overflow-y: auto; font-size: 10px; line-height: 1.45; }
  .logbox .t { color: #5f7fa0; }
  .logbox .WARNING { color: var(--am); }
  .logbox .ERROR { color: var(--rd); }
  .logbox .INFO { color: #9fd0ff; }
  .logbox .BUY { color: var(--gr); font-weight: 700; }
  .logbox .SELL { color: var(--rd); font-weight: 700; }

  /* ---------- 决策灯 ---------- */
  .quotes { display: flex; flex-direction: column; gap: 3px; overflow-y: auto; flex: 0 0 auto;
    border: 1px solid rgba(0,240,255,.15); border-radius: 6px; padding: 4px 8px; margin-bottom: 5px; }
  .quote { display: flex; align-items: center; gap: 8px; font-size: 11px; }
  .quote.held { background: rgba(0,240,255,.06); border-left: 2px solid var(--cy); padding-left: 4px; }
  .quote .sym { width: 76px; color: var(--cy); white-space: nowrap; }
  .quote .qp { flex: 1; color: #e8f6ff; text-align: right; }
  .quote .qch { width: 62px; text-align: right; }
  .quote .qch.up { color: var(--gr); }
  .quote .qch.down { color: var(--rd); }
  .quote .qv { width: 84px; text-align: right; color: #8fb4d8; }
  .verdicts { display: flex; flex-direction: column; gap: 5px; overflow-y: auto; flex: 0 1 auto; min-height: 0; max-height: 56px; }
  .verdict { display: flex; align-items: center; gap: 8px; font-size: 11px; }
  .verdict .sym { width: 88px; color: var(--cy); }
  .vbadge { padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
  .vbadge.BUY { background: rgba(52,255,176,.12); color: var(--gr); border: 1px solid rgba(52,255,176,.5); }
  .vbadge.SELL { background: rgba(255,59,107,.12); color: var(--rd); border: 1px solid rgba(255,59,107,.5); }
  .vbadge.HOLD { background: rgba(255,209,102,.10); color: var(--am); border: 1px solid rgba(255,209,102,.4); }
  .verdict .cf { flex: 1; font-size: 10px; color: #8fb4d8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .verdict .cn { font-size: 11px; color: var(--pu); }

  /* ---------- AI 十项解读步骤 ---------- */
  .aisteps { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 5px;
    margin-top: 5px; flex: 1 1 0; min-height: 90px; align-content: start; overflow-y: auto; }
  .aistep { text-align: center; font-size: 10px; padding: 3px 4px; border-radius: 5px; word-break: break-word;
    border: 1px solid var(--line); background: rgba(0,20,40,.35); color: #8fb4d8; letter-spacing: 1px; }
  .aistep.ok { border-color: rgba(52,255,176,.45); color: var(--gr); box-shadow: 0 0 6px rgba(52,255,176,.15); }
  .aistep.fallback { border-color: rgba(255,209,102,.45); color: var(--am); box-shadow: 0 0 6px rgba(255,209,102,.12); }
  .aistep.fail { border-color: rgba(255,59,107,.5); color: var(--rd); box-shadow: 0 0 6px rgba(255,59,107,.15); }
  .aistep .st { font-size: 11px; }

  .empty { color: #4a6a8a; font-size: 12px; text-align: center; padding: 12px 0; }
  footer { font-size: 9px; color: #3d5a78; text-align: center; letter-spacing: 2px; }
  @media (max-width: 1300px){
    .metrics { grid-template-columns: repeat(4, 1fr); }
    .m .v { font-size: 13px; }
  }
  @media (max-height: 820px){
    .logo { font-size: 16px; }
    .logo small { font-size: 10px; }
    .m .v { font-size: 12px; }
    .quote { font-size: 10px; }
    .aistep { font-size: 9px; }
    .chart { min-height: 56px; }
  }

  /* ===== 竖屏适配 ===== */
  /* 手机/窄竖屏（宽<=820 且高>宽）：单列流式，优先级 遥测→权益→决策→初选→日志 */
  @media (orientation: portrait) and (max-width: 820px){
    #app { padding: 6px 8px 4px; gap: 6px; }
    header { flex-wrap: wrap; gap: 4px 12px; padding: 4px 8px; }
    .logo { font-size: 14px; letter-spacing: 2px; }
    .logo small { font-size: 8px; letter-spacing: 1px; margin-top: 1px; }
    .st { font-size: 10px; gap: 4px; }
    .led { width: 8px; height: 8px; }
    .clock { font-size: 12px; letter-spacing: 1px; }
    .tag { font-size: 9px; padding: 2px 6px; }
    main {
      grid-template-columns: 1fr;
      grid-template-rows: none;
      overflow-y: auto;
    }
    .card { grid-column: auto !important; grid-row: auto !important; padding: 6px 8px; border-radius: 8px; }
    .card h3 { font-size: 9px; letter-spacing: 1px; margin-bottom: 4px; gap: 6px; }
    .c-tele { order: 1; height: min(40vh, 340px); }
    .c-eq   { order: 2; height: min(30vh, 270px); }
    .c-dec  { order: 3; height: min(36vh, 330px); }
    .c-sel  { order: 4; height: min(30vh, 300px); }
    .c-log  { order: 5; height: min(28vh, 260px); }
    .metrics { grid-template-columns: repeat(4, 1fr); gap: 4px; }
    .m .v { font-size: 13px; }
    .m .l { font-size: 8px; margin-top: 1px; }
    .chart { min-height: 110px !important; }
    .quotes { max-height: 64px; }
    .aisteps { grid-template-columns: repeat(5, 1fr); gap: 3px; }
    .aistep { font-size: 8px; padding: 2px 0; }
    .aistep .st { font-size: 9px; }
    .quote { font-size: 10px; }
    .quote .sym { width: 64px; }
    .quote .qv { width: 72px; }
    .quote .qch { width: 54px; }
    .verdict { font-size: 10px; }
    .verdict .sym { width: 72px; }
    .pick { font-size: 11px; gap: 8px; }
    .pick .sym { width: 78px; }
    .logbox { font-size: 9px; }
    .dead { max-height: 36px; }
    footer { font-size: 8px; letter-spacing: 1px; }
  }
  /* 竖屏大屏/平板（宽>820 的竖屏显示器）：保持双列，微调行高与字号 */
  @media (orientation: portrait) and (min-width: 821px){
    .logo { font-size: 22px; }
    .m .v { font-size: 16px; }
    .quote { font-size: 12px; }
    .verdict { font-size: 12px; }
    .logbox { font-size: 11px; }
    main { grid-template-rows: 1.1fr 1.2fr 1fr; }
  }
  ::-webkit-scrollbar { width: 5px; } ::-webkit-scrollbar-thumb { background: rgba(0,240,255,.25); border-radius: 3px; }
</style>
</head>
<body>
<div class="grid"></div><div class="scan"></div><canvas id="stars"></canvas>
<div id="app">
  <header>
    <div class="logo">GUFA QUANT <small>PROTOCOL // 古法量化决策中枢</small></div>
    <div class="st">SYSTEM <span class="led" id="ledSys"></span><span id="sysTxt">BOOT</span></div>
    <div class="st">NET <span class="led" id="ledNet"></span><span id="netTxt">--</span></div>
    <div class="st">AI <span class="led" id="ledAI"></span><span id="aiTxt">--</span></div>
    <div class="sp"></div>
    <span class="tag" id="tagMode">--</span>
    <span class="tag sb" id="tagSandbox">--</span>
    <div class="clock" id="clock">--:--:--</div>
  </header>

  <main>
    <!-- 左上：权益曲线 -->
    <div class="card c-eq" style="grid-column:1; grid-row:1">
      <h3>EQUITY // 权益曲线</h3>
      <div class="chart"><canvas id="eqChart"></canvas></div>
    </div>
    <!-- 右上：指标 -->
    <div class="card c-tele" style="grid-column:2; grid-row:1">
      <h3>TELEMETRY // 遥测</h3>
      <div class="metrics">
        <div class="m"><div class="v" id="mEquity">--</div><div class="l">权益</div></div>
        <div class="m"><div class="v green" id="mPeak">--</div><div class="l">峰值</div></div>
        <div class="m"><div class="v amber" id="mTrades">--</div><div class="l">今日成交</div></div>
        <div class="m"><div class="v" id="mCycle">--</div><div class="l">周期s</div></div>
        <div class="m"><div class="v green" id="mFills">--</div><div class="l">持仓</div></div>
        <div class="m"><div class="v" id="mReview">--</div><div class="l">复查</div></div>
        <div class="m"><div class="v" id="mCand">--</div><div class="l">候选</div></div>
        <div class="m"><div class="v" id="mDayStart">--</div><div class="l">日初</div></div>
      </div>
      <!-- 雷达图：十项古法 -->
      <h3 style="margin-top:6px">ANCIENT METHODS // 十项古法信号</h3>
      <div class="chart" style="flex:1 1 auto; min-height:70px"><canvas id="radar"></canvas></div>
    </div>
    <!-- 左下：选股 -->
    <div class="card c-sel" style="grid-column:1; grid-row:2">
      <h3>SELECTION // 每日古法初选 <span style="color:#5f7fa0;font-size:10px" id="selDate"></span></h3>
      <div class="picks" id="picks"><div class="empty">等待初选数据…</div></div>
      <h3 style="margin-top:6px">EXCLUDED // 当日剔除</h3>
      <div class="dead" id="dead" style="font-size:10px; max-height:44px; overflow-y:auto"></div>
    </div>
    <!-- 右下：决策 -->
    <div class="card c-dec" style="grid-column:2; grid-row:2">
      <h3>LIVE QUOTES // 实时行情 <span style="color:#5f7fa0;font-size:10px" id="liveTs"></span></h3>
      <div class="quotes" id="liveQuotes" style="font-size:11px; max-height:24px; overflow-y:auto"><div class="empty">等待行情…</div></div>
      <h3 style="margin-top:4px">POSITIONS // 持仓 <span style="color:#5f7fa0;font-size:10px" id="posSum"></span></h3>
      <div class="quotes" id="positions" style="font-size:11px; max-height:48px; overflow-y:auto"><div class="empty">无持仓</div></div>
      <div class="verdicts" id="verdicts" style="flex:0 1 auto; max-height:56px"><div class="empty">等待决策…</div></div>
      <h3 style="margin-top:4px">AI 十项解读 <span id="aiStepSum" style="color:#5f7fa0;font-size:10px"></span></h3>
      <div class="aisteps" id="aisteps" style="flex:1 1 0; min-height:90px; overflow-y:auto"><div class="empty">—</div></div>
    </div>
    <!-- 底部通栏：日志 -->
    <div class="card c-log" style="grid-column:1 / span 2; grid-row:3">
      <h3>STREAM // 实时日志</h3>
      <div class="logbox" id="log"></div>
    </div>
  </main>
  <footer>GUFA QUANT PROTOCOL v1.0 // 数据源: runtime/state+health+equity // SSE 实时推送</footer>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
let eqHist = [], radarNames = [], radarVals = [], currentMode = "";
let curTriggers = 0, curPositions = 0;

/* ---------- 星空粒子 ---------- */
(function stars(){
  const c = $("stars"), ctx = c.getContext("2d");
  let W, H, pts = [];
  function resize(){ W = c.width = innerWidth; H = c.height = innerHeight; }
  function init(){ resize(); pts = Array.from({length: 90}, () => ({x: Math.random()*W, y: Math.random()*H, r: Math.random()*1.3+.3, v: Math.random()*.25+.05})); }
  function draw(){
    ctx.clearRect(0,0,W,H);
    for (const p of pts){
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 7);
      ctx.fillStyle = "rgba(140,220,255,"+(.25+Math.random()*.35)+")"; ctx.fill();
      p.y -= p.v; if (p.y < -4){ p.y = H+4; p.x = Math.random()*W; }
    }
    requestAnimationFrame(draw);
  }
  addEventListener("resize", init); init(); draw();
})();

/* ---------- 权益曲线 ---------- */
function drawEq(){
  const c = $("eqChart"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth, H = c.height = c.clientHeight;
  ctx.clearRect(0,0,W,H);
  // 网格
  ctx.strokeStyle = "rgba(0,240,255,.08)"; ctx.lineWidth = 1;
  for (let i=1;i<6;i++){ const y=H*i/6; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }
  for (let i=1;i<12;i++){ const x=W*i/12; ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,H); ctx.stroke(); }
  if (!eqHist.length){ ctx.fillStyle="#4a6a8a"; ctx.font="12px Consolas"; ctx.fillText("NO DATA", W/2-28, H/2); return; }
  let min=Infinity, max=-Infinity;
  for (const p of eqHist){ if(p<min)min=p; if(p>max)max=p; }
  if (max-min < 1e-9){ max+=1; min-=1; }
  const pad = 8;
  const X = i => pad + (W-2*pad) * i / Math.max(1, eqHist.length-1);
  const Y = v => pad + (H-2*pad) * (1 - (v-min)/(max-min));
  // 面积渐变
  const g = ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0, "rgba(0,240,255,.32)"); g.addColorStop(1, "rgba(0,240,255,0)");
  ctx.beginPath();
  eqHist.forEach((v,i)=> i? ctx.lineTo(X(i),Y(v)) : ctx.moveTo(X(i),Y(v)));
  ctx.lineTo(X(eqHist.length-1), H); ctx.lineTo(X(0), H); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
  // 线
  ctx.beginPath();
  eqHist.forEach((v,i)=> i? ctx.lineTo(X(i),Y(v)) : ctx.moveTo(X(i),Y(v)));
  ctx.strokeStyle = "#00f0ff"; ctx.lineWidth = 1.6; ctx.shadowColor = "#00f0ff"; ctx.shadowBlur = 8; ctx.stroke();
  ctx.shadowBlur = 0;
  // 端点
  const lx=X(eqHist.length-1), ly=Y(eqHist[eqHist.length-1]);
  ctx.beginPath(); ctx.arc(lx, ly, 3, 0, 7); ctx.fillStyle = "#fff"; ctx.shadowColor="#00f0ff"; ctx.shadowBlur=12; ctx.fill();
  ctx.font = "10px Consolas"; ctx.fillStyle = "rgba(0,240,255,.8)";
  ctx.fillText(max.toFixed(1), 4, 12); ctx.fillText(min.toFixed(1), 4, H-4);
}

/* ---------- 雷达图 ---------- */
function drawRadar(){
  const c = $("radar"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth, H = c.height = c.clientHeight;
  ctx.clearRect(0,0,W,H);
  const N = radarNames.length, cx = W/2, cy = H/2, R = Math.min(W,H)/2 - 26;
  if (!N) { ctx.fillStyle="#4a6a8a"; ctx.font="12px Consolas";
    ctx.fillText(currentMode==="signal"
      ? "TRIGGER WATCH · "+curTriggers+" 布防 / "+curPositions+" 持仓"
      : "NO SIGNAL", cx-74, cy); return; }
  const ang = i => -Math.PI/2 + i*2*Math.PI/N;
  // 网格环
  for (let ring=1; ring<=4; ring++){
    ctx.beginPath();
    for (let i=0;i<=N;i++){ const a=ang(i%N); const r=R*ring/4; i? ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)) : ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); }
    ctx.strokeStyle = "rgba(0,240,255,.10)"; ctx.stroke();
  }
  // 轴线
  for (let i=0;i<N;i++){ const a=ang(i); ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+R*Math.cos(a), cy+R*Math.sin(a)); ctx.strokeStyle="rgba(0,240,255,.10)"; ctx.stroke(); }
  // 数值面
  ctx.beginPath();
  for (let i=0;i<=N;i++){ const a=ang(i%N); const v=Math.max(0,Math.min(1,radarVals[i%N]||0)); const r=R*v; i? ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)) : ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); }
  ctx.closePath();
  const g = ctx.createRadialGradient(cx,cy,10,cx,cy,R);
  g.addColorStop(0,"rgba(168,85,247,.55)"); g.addColorStop(1,"rgba(0,240,255,.15)");
  ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = "#a855f7"; ctx.lineWidth = 1.4; ctx.shadowColor="#a855f7"; ctx.shadowBlur=10; ctx.stroke(); ctx.shadowBlur=0;
  // 标签
  ctx.font = "10px Consolas"; ctx.fillStyle = "#9fd0ff"; ctx.textAlign = "center";
  for (let i=0;i<N;i++){ const a=ang(i); const tx=cx+(R+14)*Math.cos(a), ty=cy+(R+14)*Math.sin(a);
    ctx.fillText(radarNames[i], tx, ty+3); }
}

/* ---------- 渲染 ---------- */
function setLed(id, cls, txt){ const l=$(id); if(!l) return; l.className="led "+(cls||""); const t=$(id.slice(3).toLowerCase()+"Txt"); if(t) t.textContent=txt; }

function render(d){
  currentMode = d.mode||"";
  curTriggers = (d.triggers&&d.triggers.length)||0;
  curPositions = d.position_count||0;
  // 状态灯
  setLed("ledSys", d.status==="ok"?"ok":(d.status==="degraded"?"":"bad"), (d.status||"?").toUpperCase());
  setLed("ledNet", d.net_ok?"ok":"bad", d.net_ok?"LINK":"DOWN");
  const aiCls = d.ai_status==="ready"?"ok":(d.ai_status==="degraded"?"warn":(d.ai_busy?"busy":"bad"));
  const aiTxt = d.ai_status==="ready"?"READY":(d.ai_status==="degraded"?"DEGRADED":(d.ai_busy?"BUSY":"DOWN"));
  setLed("ledAI", aiCls, aiTxt);
  $("tagMode").textContent = d.mode||"--";
  $("tagSandbox").textContent = d.sandbox ? "SANDBOX" : "PRODUCTION";
  // 遥测
  if (d.equity!=null){ $("mEquity").textContent = d.equity.toFixed(2); }
  $("mPeak").textContent = d.peak!=null ? d.peak.toFixed(2) : "--";
  $("mTrades").textContent = d.trades_today!=null ? d.trades_today : "--";
  $("mCycle").textContent = d.cycle_seconds!=null ? Math.round(d.cycle_seconds)
    : (d.mode==="signal" ? "布防"+(d.triggers?d.triggers.length:0) : "--");
  $("mFills").textContent = d.fills!=null ? d.fills
    : (d.mode==="signal" ? d.position_count : "--");
  $("mReview").textContent = d.next_review_seconds!=null ? (d.next_review_seconds/60).toFixed(0)+"m"
    : (d.mode==="signal" ? "实时" : "--");
  $("mCand").textContent = d.candidates!=null ? d.candidates : "--";
  $("mDayStart").textContent = d.day_start!=null ? d.day_start.toFixed(2) : "--";
  // 选股
  if (d.selection_date) $("selDate").textContent = "// " + d.selection_date;
  if (d.picks && d.picks.length){
    const picks = d.picks.map(p=>`<div class="pick"><span class="sym">${p.sym.replace('/USDT','')}</span>
      <div class="bar"><div class="fill" style="width:${(p.score*100).toFixed(1)}%"></div></div>
      <span class="sc">${p.score.toFixed(3)}</span><span class="st2">${p.action||''}</span></div>`).join("");
    $("picks").innerHTML = picks;
  }
  $("dead").innerHTML = d.dead && Object.keys(d.dead).length
    ? Object.entries(d.dead).map(([k,v])=>`<span>✕ ${k.replace('/USDT','')}: ${v}</span>&nbsp;&nbsp;`).join("") : "<span style='color:#4a6a8a'>无剔除</span>";
  // 决策
  // 实时行情列表：显示全部监控币行情（持仓 value>0 高亮标注），
  // 空仓时也持续滚动价格，避免看起来"价格不更新"。
  const quoteEntries = d.quotes && Object.keys(d.quotes).length
    ? Object.entries(d.quotes) : [];
  if (quoteEntries.length){
    if (d.live_updated_at) $("liveTs").textContent = "// " + d.live_updated_at.slice(11,19) + " 实时";
    const shortName = (sym)=>{
      const m = String(sym).match(/^(?:swap:)?([^/]+)\//);
      return (m?m[1]:sym) + (String(sym).startsWith("swap:")?"◆":"");
    };
    const qs = quoteEntries.map(([sym,q])=>{
      const held = q && Number(q.value)>0;
      const pct = q && q.change_pct!=null ? q.change_pct : 0;
      const cls = pct>0.0001 ? "up" : (pct<-0.0001 ? "down" : "");
      const arrow = pct>0.0001 ? "▲" : (pct<-0.0001 ? "▼" : "•");
      return `<div class="quote${held?" held":""}"><span class="sym">${shortName(sym)}${held?"◆":""}</span>
        <span class="qp">${q?Number(q.price).toPrecision(6):"--"}</span>
        <span class="qch ${cls}">${arrow}${Math.abs(pct).toFixed(2)}%</span>
        <span class="qv">${held?Number(q.value).toFixed(2)+"U":"--"}</span></div>`;
    }).join("");
    $("liveQuotes").innerHTML = qs;
  } else { $("liveQuotes").innerHTML = '<div class="empty">等待行情…</div>'; }
  // 持仓列表：state.positions 真实持仓 + 实时价（市值/浮动盈亏）
  if (d.positions && d.positions.length){
    const ps = d.positions.map(p=>{
      const pnlCls = p.pnl_pct>0.0001 ? "up" : (p.pnl_pct<-0.0001 ? "down" : "");
      const arrow = p.pnl_pct>0.0001 ? "▲" : (p.pnl_pct<-0.0001 ? "▼" : "•");
      return `<div class="quote held"><span class="sym">${String(p.sym).replace('/USDT','')}◆</span>
        <span class="qp">${Number(p.price).toPrecision(6)}</span>
        <span class="qch ${pnlCls}">${arrow}${Math.abs(p.pnl_pct).toFixed(2)}%</span>
        <span class="qv">${Number(p.value).toFixed(0)}U</span></div>`;
    }).join("");
    $("positions").innerHTML = ps;
    $("posSum").textContent = "// " + d.positions.length + " 个 · " + d.positions.reduce((s,p)=>s+p.value,0).toFixed(0) + "U";
  } else {
    $("positions").innerHTML = '<div class="empty">无持仓</div>';
    $("posSum").textContent = "";
  }
  if (d.mode==="signal" && d.triggers && d.triggers.length){
    const ts = d.triggers.map(t=>`<div class="verdict"><span class="sym">${t.sym.replace('/USDT','')}</span>
      <span class="vbadge hold">入${t.entry_n}</span>
      <span class="cn">出${t.exit_n}</span>
      <span class="cf">${t.target!=null?(t.target*100).toFixed(0)+"%仓":""}${t.first_at?" · "+String(t.first_at).slice(11,16):""}</span></div>`).join("");
    $("verdicts").innerHTML = ts;
  } else if (d.verdicts && d.verdicts.length){
    const vs = d.verdicts.map(v=>`<div class="verdict"><span class="sym">${v.sym.replace('/USDT','')}</span>
      <span class="vbadge ${v.action}">${v.action}</span>
      <span class="cn">${v.confidence!=null?v.confidence.toFixed(2):"--"}</span>
      <span class="cf">${v.conflicts||""}</span></div>`).join("");
    $("verdicts").innerHTML = vs;
  } else if (d.verdicts && d.verdicts.length===0) { $("verdicts").innerHTML = '<div class="empty">无持仓/无新决策</div>'; }
  else if (d.mode==="signal") { $("verdicts").innerHTML = '<div class="empty">未布防触发条件</div>'; }
  // AI 十项解读步骤
  if (d.ai_steps && d.ai_steps.length){
    const nOk = d.ai_steps.filter(s=>s.status==="ok").length;
    const nFb = d.ai_steps.filter(s=>s.status==="fallback").length;
    $("aiStepSum").textContent = d.mode==="signal"
      ? "// 信号模式 · AI 实时决策"
      : `// ${nOk}✓ ${nFb}⚠ 规则兜底` + (d.ai_agg_failed ? " · 聚合兜底" : "");
    $("aisteps").innerHTML = d.ai_steps.map(s=>{
      const mark = s.status==="ok"?"✓":(s.status==="fallback"?"⚠":"✗");
      return `<div class="aistep ${s.status}"><span class="st">${mark}</span> ${s.name}</div>`;
    }).join("");
  } else if (d.mode==="signal"){
    const n = d.triggers ? d.triggers.length : 0;
    $("aiStepSum").textContent = "// 信号模式 · AI-1 布防";
    $("aisteps").innerHTML = `<div class="aistep ok"><span class="st">✓</span> AI-1 布防 ${n} 标的</div>` +
      `<div class="aistep ok"><span class="st">✓</span> 轮询 2s · 触发即执行</div>`;
  }
  // 权益曲线
  if (d.equity_hist) eqHist = d.equity_hist;
  drawEq();
  // 雷达
  if (d.radar){ radarNames = d.radar.names; radarVals = d.radar.values; drawRadar(); }
  // 日志
  if (d.logs){ $("log").innerHTML = d.logs; }
}

/* ---------- 时钟 ---------- */
setInterval(()=>{ const t=new Date(); const p=n=>String(n).padStart(2,"0");
  $("clock").textContent = p(t.getHours())+":"+p(t.getMinutes())+":"+p(t.getSeconds()); }, 1000);

/* ---------- 数据流 ---------- */
fetch("/api/state").then(r=>r.json()).then(render).catch(()=>{});
const es = new EventSource("/api/stream");
es.onmessage = e => { try { render(JSON.parse(e.data)); } catch(_){} };
es.onerror = () => setLed("ledSys", "bad", "LOST");
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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []


class Snapshot:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.health_path = self.state_dir / "health.json"
        self.equity_path = self.state_dir / "equity.jsonl"
        self.orders_path = self.state_dir / "orders.audit.jsonl"
        self.log_path = self.state_dir / "gufa_quant.jsonl"
        self._log_cache: List[str] = []
        self._log_size = -1
        self._equity_cache: List[float] = []
        self._equity_size = -1

    # ---------- 带缓存的增量读取 ----------
    def _read_equity(self) -> List[float]:
        size = self.equity_path.stat().st_size if self.equity_path.exists() else -1
        if size == self._equity_size and self._equity_cache:
            return self._equity_cache
        out: List[float] = []
        try:
            for line in self.equity_path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(float(json.loads(line).get("equity", 0.0)))
                except Exception:
                    continue
        except Exception:
            pass
        out = out[-EQUITY_MAX_POINTS:]
        self._equity_cache = out
        self._equity_size = size
        return out

    # ---------- AI 步骤解析（最近一个周期内） ----------
    def _ai_steps_and_status(self, health: Dict[str, Any], mode: str = "") -> Dict[str, Any]:
        """解析 AI 十项解读的每步状态。

        周期模式：以「最近周期完成之后」的日志为准，统计拆分解读失败；
        信号模式：无周期概念，直接按日志尾部统计 AI-1 入场决策成败 /
                  AI-2 出场监控 / 空响应重试，反映真实 AI 健康度。
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
                if "AI-1 入场决策失败" in msg:
                    fail1 += 1
                elif "AI-1 入场决策" in msg and "decision=" in msg:
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
                {"name": f"AI-1 古法入场 · 成功 {ok1} · 失败 {fail1}",
                 "status": "ok" if ok1 >= fail1 else "error"},
                {"name": "AI-2 出场条件监控 · 就绪", "status": "ok"},
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
            if not self._is_key_event(msg):
                continue
            # 高频刷屏事件合并：AI 错误/空响应/决策失败只保留最新 1 条，
            # 避免日志卡被上游 503 刷屏，挤掉成交/布防等关键事件。
            if ("AI 响应为空或请求失败" in msg or "AI 中转站错误" in msg
                    or "AI-1 入场决策失败" in msg):
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
            "信号触发模式启动", "AI-1 入场决策", "AI-2 出场",
            "AI 响应为空或请求失败", "AI 中转站错误", "布防", "触发条件",
            "AI-1 响应结构无效", "AI 缩量仲裁", "AI 判定放弃", "AI 缩量后仍失败",
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
                "first_at": info.get("first_trigger_at"),
                "ref": info.get("ref_price"),
            })
        triggers.sort(key=lambda t: t["sym"])

        # 信号模式：idle（刚启动尚未决策）且已有布防 → 视为就绪；否则以真实统计为准。
        if health.get("mode") == "signal" and ai["status"] == "idle" and triggers:
            ai["status"] = "ready"
            ai_ok = True
            ai_busy = False

        # 雷达：周期模式取最后一个决策的十项 readings；
        # 信号模式无 readings，用系统状态指标填充雷达（布防/持仓/行情/成交/AI/权益）。
        radar_names: List[str] = []
        radar_values: List[float] = []
        if health.get("mode") == "signal":
            pos_count = len(state.get("positions") or {})
            trigger_count = len(health.get("triggers") or {})
            quote_count = len(health.get("quotes") or {})
            trades = state.get("trades_today") or 0
            blocked = len(state.get("depth_blocked") or {})
            radar_names = ["布防", "持仓", "行情", "成交", "屏蔽", "AI"]
            radar_values = [
                min(1.0, trigger_count / 54.0),
                min(1.0, pos_count / 10.0),
                min(1.0, quote_count / 54.0),
                min(1.0, trades / 10.0),
                min(1.0, blocked / 20.0),
                1.0 if ai_ok else 0.4,
            ]
        else:
            last_readings = None
            for sym, info in reversed(list(decisions.items())):
                readings = info.get("readings") or info.get("ai", {}).get("readings")
                if isinstance(readings, dict) and readings:
                    last_readings = readings
                    break
            if isinstance(last_readings, dict):
                for name, rd in list(last_readings.items())[:10]:
                    radar_names.append(str(name))
                    try:
                        radar_values.append(float(rd.get("confidence", 0.0)))
                    except Exception:
                        radar_values.append(0.0)

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
            "radar": {"names": radar_names, "values": radar_values},
            "equity_hist": equity_hist,
            "logs": self._read_logs_html(),
            "orders": orders,
            "net_ok": net_ok,
            "ai_ok": ai_ok,
            "ai_busy": ai_busy,
            "ai_status": ai["status"],
            "ai_steps": ai["steps"],
            "ai_agg_failed": ai["agg_failed"],
        }

    @staticmethod
    def _action_for(score: float) -> str:
        if score >= 0.55:
            return "FULL"
        if score >= 0.47:
            return "HALF"
        return "WATCH"


class Handler(BaseHTTPRequestHandler):
    snap: Snapshot = None  # type: ignore[assignment]

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

    Handler.snap = Snapshot(state_dir)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{APP_NAME}] 科幻监控大屏已启动: http://{args.host}:{args.port}  (数据源: {state_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{APP_NAME}] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
