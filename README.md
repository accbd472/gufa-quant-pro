# GuFaQuant-Pro 8.8.0

> 🤖 **代码生成声明：本仓库代码由 AI 编写（模型：`deepseek-v4-flash-0731`）。** 使用前请自行审查代码，作者不对代码正确性、安全性或交易结果作任何担保。

默认安全的 CCXT 现货 + 永续合约、多标的、单向做多量化交易服务。8.0 起十大古法信号来自**真实排盘**（时家奇门、大六壬、太乙神数、周易卦辞、玄空飞星、子平八字、梅花易数、紫微斗数、京房八卦、四柱神煞），由代码按公开规则排盘，由大语言模型作为**断卦师**解读完整盘面后转译为受约束的交易动作。断卦解读由 LLM 生成（默认模型 `deepseek-v4-flash-0731`，可通过 `config.json` 的 `ai.model` 更换）。排盘过程真实可复现，但**预测准确性不保证**，不构成投资建议。

> ⚠️ **交易所兼容性声明：本项目仅实测过 OKX（现货 + 沙盒永续）。** 基于 CCXT 的其他交易所（Binance、Bybit、Bitget、Gate 等）未经验收，可能因市场符号、订单参数、限频策略差异无法直接运行；跨交易所使用需自行按「尚需按目标交易所验收」核对。

## 安全边界

- 不提供本地纸面账户；余额、仓位、订单、成交和手续费全部以交易所 API 回报为准。
- 默认 `exchange.sandbox=true`，应连接交易所 Sandbox/Testnet/Demo 模拟盘。
- 当前支持 CCXT 现货（spot）与 OKX 永续合约（swap，仅 sandbox 可用；`allowed_markets` 含 swap 且非沙箱会直接报错），单向做多，杠杆上限 `risk.max_leverage`；不支持股票、做空。
- **仅实测过 OKX**：`exchange.id` 只填过 `okx`；其余 CCXT 交易所未经任何测试，订单字段/市场/限频均可能不兼容，请勿在未验收前用于其他交易所。
- 股票需要独立行情源和券商交易网关；不得把 AAPL、贵州茅台等股票代码放进 OKX `runtime.symbols`。
- `once` 和 `run` 可能向交易所提交订单，即使当前使用的是交易所模拟盘。
- AI 负责断卦与动作转译：`decision_mode=bounded` 时 BUY 不得突破规则仓位上限；`decision_mode=full` 时 AI 全权决定动作/仓位（无止损止盈、无账户熔断、无单币/总仓位上限），仍保留单笔金额上限、现金留存与订单状态硬停等交易所安全阀。
- 排盘与断卦规则全部在代码中披露（见 `gufa_paipan_*.py` 与 `gufa_paipan_signal.py`）；太乙神数等流派算法分歧大的项，盘面标注所用简式与来源，不冒充权威。
- AI 结构错误或调用失败且 `fail_closed=true` 时，不允许增加风险。
- API 必须关闭提现权限，建议启用 IP 白名单；不要把任何 API Key 提交到仓库或发送到聊天中。

## 8.8 信号触发模式（默认）：双 AI 分工 + 条件监听

8.8.0 起默认不再按固定周期全量重算，改为**信号触发**：由 AI 预设触发条件，监听循环按最小间隔轮询，**条件命中才下单**。

- **双 AI 分工**：AI-1（入场决策）按古法判断当前是否交易，并把入场时机翻译成触发条件（`price_above/below`、相对买入价或现价的涨跌幅 `change_pct_above/below`、`rsi_above/below`、`volume_surge` 放量、`time_after` 到点）；AI-2（出场决策）以**买入成交均价为基准**设定卖出条件（止盈/止损/时间兜底，任一命中即平仓）。
- **古法择时**：AI-1 给出的入场条件生效前，系统扫描当天剩余时辰的排盘，取盘面最完整的档位作为**当日首次触发时刻**；未到该时刻只监听不入场。
- **最小间隔**：监听循环按 `runtime.trigger_poll_seconds`（默认 2 秒，OKX 批量 tickers 限频 20req/2s）轮询；RSI/放量指标带 60 秒缓存，避免 K 线接口超限频。
- **防反复唤醒**：AI-1 判定"今日不宜交易"冷却到次日 UTC 零点；AI 调用失败退避 10 分钟；入场条件超过 `trigger_max_wait_hours`（默认 24h）自动唤醒 AI-1 重新评估。
- **重启恢复**：已有持仓但无出场条件时，以持仓成本价为基准唤醒 AI-2 补设。
- **开关**：`runtime.trigger_mode="signal"`（默认）为信号触发；改回 `"cycle"` 恢复旧周期模式（每日初选 + 周期精筛 + AI 十项解读）。信号模式下 `once` / `validate` 等命令仍按周期模式执行。

## 8.0 新功能：真实古法排盘 + AI 断卦师

- 十大古法全部由代码按公开规则排盘：奇门（时家转盘）、六壬（月将加时起课）、太乙（积年简式）、易经（时间起卦+卦辞）、风水（玄空飞星）、八字（子平）、梅花（体用）、紫微（三合派安星）、八卦（京房八宫）、四柱（宫位神煞）。
- 每个标的生成两套盘：**本命盘**（以最早 K 线时间 = 上市时间为基准）与**时空盘**（以最新闭合 K 线时间为基准，按配置经纬度做真太阳时修正）。
- 排盘结果经确定性简化断卦规则映射为 [0,1] 置信度供风控管线使用；LLM 断卦师（默认 `deepseek-v4-flash-0731`，`config.json` 的 `ai.model` 可配置）读取完整盘面（卦象/四课三传/九宫/星盘/飞星）做综合解读。
- `paipan.enabled=false` 时回退到 7.x 的技术因子模式，便于回滚；`listing_time_source` 支持 `ohlcv`（默认）与 `manual`（人工指定上市时间）。
- 新增依赖：`lunar_python`（历法数据与八字基础，MIT）。

## 8.1 功能：断卦要点 + 排盘报告 + 古法信号回测

- **确定性断卦要点**（`paipan_verdicts`）：每项古法给出人话结论与依据（如"初传巳火生日干辛金，气助日干"、"值符天柱临惊门（凶门）"），与置信度同源、完全可复现，供审计与报告。
- **`paipan-report`**：离线生成完整盘面 + 信号 + 断卦要点报告（JSON/Markdown），不连接交易所、不下单；适合排盘校验与人工审计。`--now` 可指定任意历史时刻复盘。
- **`backtest-paipan`**：逐日排盘回测——对历史日线每天生成时空盘信号，统计十项古法与未来 N 日收益的 Pearson 相关、多头/空头命中率与平均收益；支持 `--ohlcv-file` 用本地 CSV 离线回测（列：`symbol,date,open,high,low,close[,volume]`）。统计结果仅供观察关联，不构成预测保证。
- 新命令均为**只读**：不需要交易所凭据、不经过 InstanceLock、不下单。
- **`export-ohlcv`**：导出公开 K 线为 CSV（列 `symbol,date,open,high,low,close,volume`，与 `backtest-paipan --ohlcv-file` 兼容），无需凭据，形成"导出→回测"数据闭环。
- **暂停开关**：`pause` / `resume` 命令创建/移除 `state_dir/pause` 标记。暂停时**不开新仓**，但存量仓位管理与保护性退出（硬止损/回撤/日内亏损熔断）照常运行——运维应急不停进程。
- **Webhook 事件通知**（可选）：`runtime.webhook_url` 配置端点后，成交（`order_fill`）、订单不确定（`order_uncertain`）、组合熔断（`halted`）事件会推送 JSON（含 sandbox 标志）；留空禁用、失败不影响交易主流程。
- **权益曲线历史**：每个周期追加一条 `state_dir/equity.jsonl`（时间/权益/可用计价币/暂停/成交数），不覆盖、可回放。
- **`stats` 汇总**（只读）：汇总权益曲线与成交审计——周期数、区间收益、**最大回撤**、按标的分组成交数、订单错误/不确定次数，适合运维巡检与观察期评估。

## 7.5.0 功能：每日初选 + 逐个精筛

- `runtime.symbols` 是人工允许的币种候选白名单；程序和 AI 都不能扩展到白名单之外。
- 第一阶段每天按 UTC 日期读取所有候选币的闭合 `1d` K 线，用同一套十项技术因子计算日线综合分并排序。
- 只选择达到 `selection.min_score` 的前 `selection.top_n` 个币种，随后按 `runtime.timeframe` 逐个精筛与 AI 解读。
- 日线扫描结果缓存到状态文件；同一天且筛选配置未变化时复用，减少行情请求。
- 任一候选的日线扫描失败时，本周期标记为 `degraded`、入选集置空并禁止新开仓；已有仓位仍继续原周期精筛和硬止损管理。
- 未入选但已有仓位的币种不调用远程 AI，且目标仓位不得高于当前仓位，只允许持有、减仓或保护性退出。
- 健康报告新增 `daily_selection`、`new_entry_symbols`、`managed_position_symbols` 和 `fine_screen_symbols`。

现有 7.4.0 配置即使没有 `selection` 段也可读取，并会采用安全默认值：启用、`1d`、250 根 K 线、最多 3 个、最低分 0.55。需要自定义时，可在现有 `config.json` 顶层加入：

```json
{
  "runtime": {
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "timeframe": "1h"
  },
  "selection": {
    "enabled": true,
    "timeframe": "1d",
    "ohlcv_limit": 250,
    "top_n": 2,
    "min_score": 0.55
  }
}
```

这里只展示相关字段，不是完整配置文件。候选项必须是目标交易所实际支持、计价币一致且基础资产不重复的 CCXT 现货交易对。

## 7.4.0 功能

- `setup` 首次配置向导：一次填写交易所和 AI 凭据，后续自动读取。
- 密钥不写入 `config.json`，而是写入独立凭据文件；目录尝试使用 `0700`、文件使用 `0600`。
- 环境变量仍可临时覆盖本地凭据，适用于 systemd、Docker 或临时切换。
- `model show/list/set`：查看、查询和切换第三方中转站模型。
- `ai-check`：只测试 AI 中转站和严格 JSON Schema，不连接交易所、不读取账户、不下单。
- 正式 AI 提示词使用完整十项 JSON 契约；首次结构校验失败时只请求一次格式修复，仍失败则按既有 `fail_closed` 策略安全回退。
- 状态身份加入交易所 API Key 的不可逆摘要，降低切换账户后误用旧状态的风险。

> 本地凭据文件只是“明文 + 文件权限”保护，不是加密保险库。同一系统用户或 root 仍可能读取它。

## 安装

建议使用 Python 3.11 或 3.12：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

开发检查依赖：

```bash
python -m pip install -r requirements-dev.txt
python -m py_compile gufa_quant_pro.py tests/test_gufa_quant_pro.py
ruff check gufa_quant_pro.py tests
pytest -q
```

## 快速开始（交易所模拟盘）

### 1. 创建配置

已有 `config.json` 可直接保留；首次安装可复制示例：

```bash
cp config.example.json config.json
```

也可以生成安全默认配置（拒绝覆盖已有文件）：

```bash
python gufa_quant_pro.py --config config.json init-config
```

> **国内网络访问 OKX 需要代理时**（可选）：在 `config.json` 的 `exchange` 段设置
> `"proxy_url": "http://127.0.0.1:7890"`（以你的 Clash/V2Ray 本地混合端口为准）。
> 代理只作用于本应用发出的请求，不影响系统或其他程序的网络；留空或删掉该字段即不走代理。

### 2. 首次配置向导

```bash
python gufa_quant_pro.py --config config.json setup
```

向导会询问交易所 CCXT ID、交易所凭据、是否启用 AI、中转站 Base URL、模型 ID 和 AI Key。已有密钥时直接回车会保留；输入新值会覆盖；可选凭据输入 `-` 可清除。日常运行不再要求每次 `export`。

默认凭据路径：

```text
~/.config/gufa-quant-pro/credentials-<配置路径摘要>.json
```

可以用 `GUFA_CREDENTIALS_FILE=/绝对路径/credentials.json` 覆盖。读取优先级为：同名环境变量优先，本地凭据文件其次。

### 3. 安全测试 AI

```bash
python gufa_quant_pro.py --config config.json ai-check
```

成功时末尾输出：

```text
AI_SCHEMA_TEST=PASS
```

该命令只向 AI 发送 `SCHEMA/TEST` 合成数据，执行路径不会构造交易控制器或交易所网关；不会查询 OKX 余额、不会读取真实行情，也不可能下单。`format_repair_used=true` 表示首次结构不合格但一次修复成功；若经常如此，建议换用结构化输出更稳定的模型。

### 4. 校验模拟盘

保持：

```json
{
  "exchange": {"sandbox": true},
  "ai": {"fail_closed": true}
}
```

然后执行：

```bash
python gufa_quant_pro.py --config config.json validate
```

`validate` 会连接交易所并检查市场和行情，但不会进入完整调仓周期。

### 5. 执行周期

```bash
python gufa_quant_pro.py --config config.json once
python gufa_quant_pro.py --config config.json run
```

警告：`once` 与 `run` 都可能向交易所模拟盘提交远程订单，不是无副作用测试命令。测试 AI 应使用 `ai-check`。

### 6. Web 控制台（小白 / 手机端）

本机启动（默认仅本机可访问）：

```bash
python gufa_quant_pro.py --config config.json console
```

手机同一局域网访问（暴露到局域网，需令牌保护）：

```bash
python gufa_quant_pro.py --config config.json console --host 0.0.0.0
```

- 控制台启动时会打印**访问令牌**（也可用 `--token` 或环境变量 `GUFA_CONSOLE_TOKEN` 固定）；
  所有 `/api/*` 接口都需要该令牌。
- 功能：一键设置向导（交易所凭据 / AI 断卦师 / 代理与交易对）、一键 启动/停止/暂停/恢复、
  只读看板（状态、权益曲线、持仓、成交、日志）、一键校验。
- **自助选择交易标的**：设置向导第 3 步可点击从 OKX 实时合约列表（或内置主流币）中勾选要交易的币种，
  保存后点「▶ 启动交易」即可自己运行。换标的会自动备份旧交易状态（权益曲线等），不丢失历史。
- 凭据只写入本地凭据文件（不经过网络、不回显）；控制台默认只监听 `127.0.0.1`。
- 停止控制台（Ctrl+C）**不会**停止已启动的交易进程；交易进程由控制台托管，可在页面停止。

## 第三方 OpenAI 兼容中转站

通过 `setup` 修改最安全；也可以手工修改 `config.json` 中的非敏感字段：

```json
{
  "ai": {
    "enabled": true,
    "model": "中转站提供的实际模型ID",
    "api_key_env": "OPENAI_API_KEY",
    "base_url": "https://relay.example.com/v1",
    "timeout_seconds": 20,
    "fail_closed": true,
    "minimum_allow_confidence": 0.6,
    "decision_mode": "bounded",
    "max_output_tokens": 1200
  }
}
```

不要把 API Key 写入 JSON。Base URL 通常应指向兼容 `/v1` 根地址，不要重复拼接 `/chat/completions`。

模型管理：

```bash
python gufa_quant_pro.py --config config.json model show
python gufa_quant_pro.py --config config.json model list
python gufa_quant_pro.py --config config.json model set MODEL_ID
```

`model list` 依赖中转站实现 `/v1/models`。不支持时直接执行 `model set MODEL_ID`。

## AI 输出与安全回退

AI 顶层必须且只能包含 `action`、`target_level`、`confidence`、`summary`、`readings`、`conflicts`、`risk_notes`。其中：

- `action` 只能是字符串 `BUY`、`SELL`、`HOLD`；
- `target_level` 只能是字符串 `FLAT`、`HALF`、`FULL`、`UNCHANGED`；
- `confidence` 必须在 0 到 1；
- `readings` 必须完整且只包含十项，每项必须包含 `bias`、`confidence`、`reading`；
- `conflicts` 与 `risk_notes` 必须是字符串数组。

程序不会因为返回“合法 JSON”就放宽字段结构校验。首次结构错误时只发送一次格式修复请求；网络错误不会在该逻辑中盲目重试。修复仍失败且 `fail_closed=true` 时，目标不得高于当前仓位，不增加风险。

默认 `decision_mode=bounded`：BUY 永远不能超过确定性规则上限，SELL 可降低风险，低于 `minimum_allow_confidence` 时按 HOLD 处理。如只希望 AI 解释，可设置 `decision_mode=explain_only`。

`decision_mode=full`（8.7.0 起，AI 全权）：AI 自主决定 BUY/SELL/HOLD、仓位档位（FLAT/HALF/FULL）、市场（现货/合约）与杠杆；不受规则分数封顶、置信度门槛、账户级熔断与单币/总仓位上限约束，保护性止损/止盈/移动止损不启用。系统仅保留交易所安全阀：单笔金额上限 `risk.max_order_quote`、现金留存 `cash_reserve_pct`、订单状态不确定硬停（ORDER_UNCERTAIN）、每日初选门控（候选池外不开新仓）。**高风险模式，建议先在 sandbox 验证。**

## Termux + Ubuntu Proot

推荐把源码放在 Ubuntu 私有目录，不要长期直接从 Android 公共下载目录运行。公共存储对 Unix 权限、文件锁和原子替换的支持较弱。

Termux 中首次安装：

```bash
termux-setup-storage
pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
```

Ubuntu 中：

```bash
apt update
apt install -y python3 python3-venv python3-pip build-essential
mkdir -p /root/apps/GuFaQuant-Pro
cp -a /sdcard/Download/GuFaQuant-Pro/. /root/apps/GuFaQuant-Pro/
cd /root/apps/GuFaQuant-Pro
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

`.venv` 只存 Python 解释器与依赖，项目源码不应复制进 `.venv`。

### 从下载目录同步新版源码

在 Ubuntu Proot 内执行：

```bash
cd /root/apps/GuFaQuant-Pro
cp -p gufa_quant_pro.py "gufa_quant_pro.py.bak-$(date +%Y%m%d-%H%M%S)"
cp /sdcard/Download/GuFaQuant-Pro/gufa_quant_pro.py ./gufa_quant_pro.py
cp /sdcard/Download/GuFaQuant-Pro/README.md ./README.md
cp /sdcard/Download/GuFaQuant-Pro/config.example.json ./config.example.json
cp /sdcard/Download/GuFaQuant-Pro/tests/test_gufa_quant_pro.py ./tests/test_gufa_quant_pro.py
. .venv/bin/activate
python -m py_compile gufa_quant_pro.py tests/test_gufa_quant_pro.py
python gufa_quant_pro.py --config config.json ai-check
```

不要覆盖 Ubuntu 项目目录中的 `config.json`、`runtime/` 或凭据。凭据默认与 Ubuntu 内配置文件的绝对路径绑定；仅复制源码不会改变凭据位置。

## 常用命令

```bash
python gufa_quant_pro.py --config config.json setup
python gufa_quant_pro.py --config config.json model show
python gufa_quant_pro.py --config config.json model list
python gufa_quant_pro.py --config config.json model set MODEL_ID
python gufa_quant_pro.py --config config.json ai-check
python gufa_quant_pro.py --config config.json validate
python gufa_quant_pro.py --config config.json once
python gufa_quant_pro.py --config config.json run
python gufa_quant_pro.py --config config.json status
python gufa_quant_pro.py --config config.json export-weights
python gufa_quant_pro.py --config config.json version
python gufa_quant_pro.py --config config.json paipan-report --format markdown --output paipan-report.md
python gufa_quant_pro.py --config config.json backtest-paipan --bars 240 --days 1 --format markdown
python gufa_quant_pro.py --config config.json backtest-paipan --ohlcv-file history.csv --format json
python gufa_quant_pro.py --config config.json export-ohlcv --timeframe 1d --bars 250 --output history.csv
python gufa_quant_pro.py --config config.json pause
python gufa_quant_pro.py --config config.json resume
python gufa_quant_pro.py --config config.json stats
```

## 状态与升级

默认运行状态写入 `./runtime/`。7.5.0 继续使用配置版本 `2`、状态版本 `4`、凭据版本 `1`；仅新增 `selection` 不要求重建 7.4.0 状态。旧状态版本 3 仍不自动迁移。

注意：状态身份会绑定 `runtime.symbols`。如果把单币种白名单扩展为多币种，旧 `state.json` 会被安全拒绝，不能直接删除后盲目启动。应先停止旧进程并完整备份状态：

```bash
cd /root/apps/GuFaQuant-Pro
mv runtime "runtime.backup-$(date +%Y%m%d-%H%M%S)"
mkdir runtime
```

然后核对新白名单内各资产的交易所真实余额。若账户已有现货余额，系统默认可能拒绝自动接管；必须人工核对成本价并显式执行：

```bash
python gufa_quant_pro.py --config config.json adopt-positions \
  --entry BTC/USDT=65000
```

## Sandbox 与正式盘

正式盘需要同时满足：

1. `exchange.sandbox=false`；
2. `risk.live_trading_ack` 精确等于 `I_UNDERSTAND_LIVE_TRADING_RISK`；
3. 换用正式盘密钥；
4. 使用与模拟盘不同的 `runtime.state_dir` 和凭据文件；
5. API 禁止提现并建议启用 IP 白名单；
6. 从极小额度开始人工核对订单、成交、手续费、余额和审计日志。

当前阶段建议继续保持交易所模拟盘。模拟盘与正式盘不得复用 API 密钥或 `state.json`。

## 生产部署（8.1.0）

面向生产环境提供了完整的部署与运维资产：

- **Docker Compose 部署**（推荐）：非 root 容器、只读根文件系统、`no-new-privileges`、
  凭据走环境变量注入、`status` 健康检查、优雅停机——见 `deploy/docker-compose.example.yml`。
- **systemd 服务**：加固版服务单元（`ProtectSystem=strict`、`NoNewPrivileges`、`UMask=0077`、
  自动拉起）——见 `deploy/gufa-quant-pro.service.example`。
- **凭据模板**：`deploy/gufa-quant-pro.env.example`。
- **生产部署指南**：`deploy/README.md`，含逐项**上线检查清单**（模拟盘全流程 →
  `ai-check`/`validate` 冒烟 → 限额收紧 → 观察期 → 正式盘 ack 确认 → 备份与监控）、
  升级/回滚步骤、日常运维与安全清单。

新命令：`python gufa_quant_pro.py --config ./config.json version` 可快速确认版本。

## 订单不确定状态

若出现 `ORDER_UNCERTAIN`：不要删除 `pending_orders` 后直接重启；应按 client order id、时间、交易对和方向查询交易所订单，核对成交、开放订单及余额，并备份 `state.json` 与 `orders.audit.jsonl` 后人工处理。CLI 不提供自动清理命令，这是有意的安全限制。

## 尚需按目标交易所验收（仅 OKX 实测过）

本项目只在 OKX 沙盒环境实测（现货 + 永续）。切换到其他交易所前，必须逐一确认目标交易所的：sandbox URL、市场 symbol、最小数量/金额、手续费币种、market buy 参数、订单状态字段、client order id 参数、`fetch_order` 支持情况、限频策略与合约市场规则（`gufa_quant_pro.py` 中 swap 路径按 OKX 永续语义实现，其他交易所需重写验收）。