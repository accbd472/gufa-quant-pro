# GuFaQuant-Pro 8.1.0

默认安全的 CCXT 现货、多标的、单向做多量化交易服务。8.0 起十大古法信号来自**真实排盘**（时家奇门、大六壬、太乙神数、周易卦辞、玄空飞星、子平八字、梅花易数、紫微斗数、京房八卦、四柱神煞），由代码按公开规则排盘，AI 作为**断卦师**解读完整盘面后转译为受约束的交易动作。排盘过程真实可复现，但**预测准确性不保证**，不构成投资建议。

## 安全边界

- 不提供本地纸面账户；余额、仓位、订单、成交和手续费全部以交易所 API 回报为准。
- 默认 `exchange.sandbox=true`，应连接交易所 Sandbox/Testnet/Demo 模拟盘。
- 当前仅支持 CCXT 现货币种、单向做多；不支持股票、杠杆、合约、永续或做空。
- 股票需要独立行情源和券商交易网关；不得把 AAPL、贵州茅台等股票代码放进 OKX `runtime.symbols`。
- `once` 和 `run` 可能向交易所提交订单，即使当前使用的是交易所模拟盘。
- AI 只负责解释与受约束的动作转译；BUY 不得突破规则仓位上限，硬风控始终优先。
- 排盘与断卦规则全部在代码中披露（见 `gufa_paipan_*.py` 与 `gufa_paipan_signal.py`）；太乙神数等流派算法分歧大的项，盘面标注所用简式与来源，不冒充权威。
- AI 结构错误或调用失败且 `fail_closed=true` 时，不允许增加风险。
- API 必须关闭提现权限，建议启用 IP 白名单；不要把任何 API Key 提交到仓库或发送到聊天中。

## 8.0 新功能：真实古法排盘 + AI 断卦师

- 十大古法全部由代码按公开规则排盘：奇门（时家转盘）、六壬（月将加时起课）、太乙（积年简式）、易经（时间起卦+卦辞）、风水（玄空飞星）、八字（子平）、梅花（体用）、紫微（三合派安星）、八卦（京房八宫）、四柱（宫位神煞）。
- 每个标的生成两套盘：**本命盘**（以最早 K 线时间 = 上市时间为基准）与**时空盘**（以最新闭合 K 线时间为基准，按配置经纬度做真太阳时修正）。
- 排盘结果经确定性简化断卦规则映射为 [0,1] 置信度供风控管线使用；AI 断卦师读取完整盘面（卦象/四课三传/九宫/星盘/飞星）做综合解读。
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

## 尚需按目标交易所验收

上线前必须确认目标交易所的 sandbox URL、市场 symbol、最小数量/金额、手续费币种、market buy 参数、订单状态字段、client order id 参数、`fetch_order` 支持情况和限频策略。