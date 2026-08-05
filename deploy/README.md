# GuFaQuant-Pro 生产部署指南

本文档面向"生产环境可用"目标：如何把 8.1.0 的十项真实古法排盘 + AI 断卦师策略，
以安全、可运维、可回滚的方式部署到服务器并长期运行。

> 边界提醒：本程序保证的是**排盘与断卦过程真实可复现**，不保证预测准确性。
> 硬风控（仓位上限、止损、单日亏损熔断、fail_closed）不可关闭。
> 默认 `exchange.sandbox=true` 连接交易所模拟盘；正式盘必须显式确认（见上线检查清单第 6 步）。

---

## 1. 部署方式总览

| 方式 | 适用场景 | 资产 |
|---|---|---|
| Docker Compose | 服务器、云主机，隔离性好、升级回滚简单 | `docker-compose.example.yml` + `Dockerfile` |
| systemd 服务 | 原生 Linux 主机，资源占用最低 | `gufa-quant-pro.service.example` |

两种方式共用同一套配置与状态目录约定：**配置在 `config.json`，运行状态在 `runtime/`，
凭据走环境变量**（`deploy/gufa-quant-pro.env.example` 模板）。

---

## 2. 共同前置（两种方式都要做）

在开发机或部署机上：

```powershell
# 1) 生成安全默认配置（拒绝覆盖已有文件）
python gufa_quant_pro.py --config ./config.json init-config
```

按需修改 `config.json`（交易所、`runtime.symbols`、`risk` 限额、`paipan` 经纬度/上市时间、
`ai.enabled` 等）。**上线前保持 `exchange.sandbox=true`。**

```powershell
# 2) 准备凭据（二选一：环境变量或本地凭据库）
#    推荐环境变量：复制 deploy/gufa-quant-pro.env.example 为 deploy/gufa-quant-pro.env 并填入。
#    交互式机器也可用：python gufa_quant_pro.py --config ./config.json setup

# 3) 本地冒烟（不连交易所、不下单）
python gufa_quant_pro.py --config ./config.json version
python gufa_quant_pro.py --config ./config.json ai-check   # 仅当 ai.enabled=true
python gufa_quant_pro.py --config ./config.json validate    # 校验交易所连接与行情
```

`validate` 通过后，先 `once` 跑一个完整周期观察信号与决策输出（模拟盘不会真下单）。

---

## 3. Docker Compose 部署（推荐）

```bash
cd deploy
cp gufa-quant-pro.env.example gufa-quant-pro.env   # 填入真实凭据，chmod 600
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml ps     # 观察健康状态
docker compose -f docker-compose.example.yml logs -f gufa-quant-pro
```

要点：
- 容器以 **uid 10001 非 root** 运行，`read_only` 根文件系统、`tmpfs /tmp`、
  `no-new-privileges`，凭据只存在于 env 注入，不落盘。
- `config.json` 以只读卷挂载到 `/config/config.json`；`runtime/` 挂载为数据卷持久化状态。
- 健康检查执行 `status` 命令：`health.json` 正常即 healthy，异常时容器显示 unhealthy，
  便于接入探活/告警。首周期约一个 `poll_interval_seconds` 后才有健康文件。

### 升级与回滚

```bash
# 升级：备份状态 → 拉新镜像 → 重启
docker compose -f deploy/docker-compose.example.yml down
cp -r runtime runtime.bak-$(date +%F)
git pull   # 或替换代码
docker compose -f deploy/docker-compose.example.yml up -d --build
docker compose -f deploy/docker-compose.example.yml logs -f gufa-quant-pro

# 回滚：恢复旧镜像与状态
docker compose -f deploy/docker-compose.example.yml down
docker compose -f deploy/docker-compose.example.yml up -d   # 换回旧 image 标签
rm -rf runtime && mv runtime.bak-$(date +%F) runtime
```

---

## 4. systemd 部署（原生 Linux）

```bash
sudo mkdir -p /opt/gufa-quant-pro /etc/gufa-quant-pro /var/lib/gufa-quant-pro
sudo cp gufa_quant_pro.py gufa_calendar.py gufa_paipan*.py gufa_yijing_data.py \
        config.example.json /opt/gufa-quant-pro/
# 在 /opt/gufa-quant-pro 建 .venv 并 pip install -r requirements.txt（需 requirements.txt）
sudo cp deploy/gufa-quant-pro.service.example /etc/systemd/system/gufa-quant-pro.service
sudo cp deploy/gufa-quant-pro.env.example /etc/gufa-quant-pro.env   # 填凭据，chmod 600
# 修改 config.json 的 state_dir 指向 /var/lib/gufa-quant-pro
sudo systemctl daemon-reload && sudo systemctl enable --now gufa-quant-pro
sudo systemctl status gufa-quant-pro
```

要点：
- 服务单元已启用 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、
  `UMask=0077`，仅 `/var/lib/gufa-quant-pro`（状态目录）可写。
- `Restart=on-failure` + `RestartSec=15` 自动拉起；`TimeoutStopSec=45` 配合 SIGTERM 优雅退出。

---

## 5. 上线检查清单（逐项确认，缺一不可）

1. [ ] **模拟盘全流程**：`exchange.sandbox=true`，`validate` 通过，`once` 输出正常，
       状态目录出现 `health.json`。
2. [ ] **排盘与断卦冒烟**：`ai-check` 通过（ai.enabled=true 时）；`once` 输出含
       `paipan_charts`（本命盘 + 时空盘十项）与 AI 解读。
3. [ ] **风控限额收紧**：`max_order_quote` / `max_total_allocation` 用你能承受的最小值，
       `max_daily_loss_pct` 保留默认熔断。
4. [ ] **观察期**：模拟盘连续运行 ≥ 数个交易日，确认信号分布、调仓频率、日志无异常。
5. [ ] **小仓位真盘试运行**（可选但强烈建议）：sandbox 保持 true 期间用交易所
       Demo/测试网账户验证订单流。
6. [ ] **正式盘确认**：仅在充分验证后，设置 `exchange.sandbox=false` 且
       `risk.live_trading_ack="I_UNDERSTAND_LIVE_TRADING_RISK"`（必须精确一致，否则启动拒绝）。
       已有持仓用 `adopt-positions` 显式接管，绝不让程序"发现即接管"。
7. [ ] **备份**：`runtime/`（状态+成交记录）与 `config.json` 定期备份；凭据文件不入库。
8. [ ] **监控**：探活 `status` 命令/容器健康检查；日志轮转已启用（默认 10MB×10）。

---

## 6. 日常运维

- **状态查询**：`python gufa_quant_pro.py --config ./config.json status`
  输出 `health.json`（版本、交易所、模式 sandbox/production、最近周期、错误计数等）。
- **日志**：`runtime/gufa_quant.log`（RotatingFileHandler 轮转）+ stdout（容器日志）。
- **单实例保护**：`runtime/gufa_quant.lock` 跨平台文件锁，防止并发跑多个实例重复下单。
- **故障处理**：
  - 连续周期错误达到 `max_consecutive_cycle_errors`（默认 5）会熔断并写错误健康状态；
  - AI 结构错误且 `fail_closed=true` 时只允许保持/降仓，绝不加仓；
  - 挂单对账失败时**不会盲目重下**，保留记录并提示人工确认（见日志中"人工确认"字样）。
- **关键命令**：`version` / `validate` / `once` / `run` / `status` /
  `export-weights` / `adopt-positions` / `ai-check`。

---

## 7. 安全清单

- [ ] `config.json`、`credentials*.json`、`*.env` 不入 git（`.gitignore` 已覆盖）；
      生产机凭据文件权限 600。
- [ ] 镜像/服务以非 root 运行，最小写权限（只读根文件系统或 ProtectSystem=strict）。
- [ ] 不使用未经验证的外部"排盘库"；排盘规则全部在代码内披露（`gufa_paipan_*.py`），
      太乙等流派分歧大的项已标注简式与来源。
- [ ] 正式盘 ack 字符串精确匹配；任何绕过 ack 的配置修改都会被启动校验拒绝。

---

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `status` 无健康文件 | 首个周期未完成；确认 `run` 在运行且 `poll_interval_seconds` 合理 |
| 容器 unhealthy | `docker compose logs` 查看错误；多数为凭据/网络/AI 配置问题 |
| 缺凭据报错 | 检查 env 文件键名与 `config.json` 的 `*_env` 键是否一致 |
| 正式盘启动被拒 | 未设置或拼错 `live_trading_ack`；这是安全门，不要绕过 |
| 时区/排盘偏差 | 确认 `paipan.longitude/latitude` 与 `exchange_timezone` 配置正确 |
