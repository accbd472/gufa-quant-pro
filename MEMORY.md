# MEMORY.md

## 项目：GuFaQuant-Pro 8.0 改造（十项真实古法排盘 + AI 断卦师）

- 8.0 目标：把 10 个"古法标签"（实为技术指标）替换为真实古法排盘 + 断卦。
- 用户已确认：全部十项一次到位；代码排盘 + AI 当断卦师；当前时辰时盘为主 + 标的上市时间做本命盘。
- 真实性边界：保证"过程真实"（排盘/断卦严格按流派 + 标准盘校验），不保证预测准确；README 免责与硬风控保留。
- 流派默认：奇门=转盘拆补法、六壬=大六壬九宗门、易经=金钱卦（可换大衍）、紫微=三合派、六爻=京房纳甲、梅花=邵康节时间起卦、八字/四柱=子平、风水=玄空飞星、太乙=积年主客算。
- 历法地基：`lunar_python` 1.4.8（仅含干支/节气/农历/八字四柱十神/大运流年/命宫身宫/旬空/九星）；六爻/奇门/六壬/太乙/紫微/梅花/玄空/易经卦均需自研 + 标准盘校验（详见 scripts/lunar_coverage_check.py）。
- 代占可复现：随机起卦用 sha256(币种+日期时辰) 作种子。
- 起盘时间细节：23:00 子时换日用 lunar 的 `getDayInGanZhiExact()`；`NineStar.getNumber()` 返回中文数字字符串；`EightChar.getYun('男'|'女')` 才有大运，`getDaYun()[0]` 是起运前占位段。

## 本机环境注意事项（Windows / Python 3.10）

- Python 解释器用 `py -3.10`（`C:\Users\32130\AppData\Local\Programs\Python\Python310\python.exe`）；`python` 命令指向 system32 存根，不可用。
- 本机 setuptools 的 `_distutils_hack` 已损坏（`import setuptools` 直接 AssertionError）。
  任何需要构建 wheel 的 pip 安装必须先在 PowerShell 设置：`$env:SETUPTOOLS_USE_DISTUTILS="stdlib"`，
  否则报 `AssertionError: ...\distutils\core.py`（lunar_python 安装即为此法解决）。
- 若问题复发，用 `py -3.10 -c "import setuptools"` 自检。

## 生产化状态（8.1.0，2026-08-06）

- 已 git init 并首次提交（commit a958bed）；git_commit 工具内部错误不可用，用 `git -c user.name=... commit` 命令行提交。
- 部署资产：deploy/（compose 示例、env 模板、生产部署指南）、Dockerfile（HEALTHCHECK/STOPSIGNAL）、.dockerignore。
- Docker Desktop 已装但引擎常不运行；构建前需先启动 Docker Desktop 等引擎就绪。
- `gufa_divination/` 是旧版平行包（仅被自身测试引用），主程序用顶层模块；保留勿删。
- 正式盘安全门：`exchange.sandbox=false` 必须同时设置 `risk.live_trading_ack="I_UNDERSTAND_LIVE_TRADING_RISK"`。

