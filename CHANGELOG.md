# Changelog

All notable changes to QuantStar will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] - 2026-11-11

### P1-a / P2-b / P2-c 收口 (分支 `fix/p1a-p2b-p2c-alert-quality-freshness`)

全部结论来自**生产库只读实测** (`/home/sandro1123/qs/data/qs.db`, 16.1 GB, `mode=ro`),
不是代码阅读推断。

#### Fixed — P1-a 告警自愈解冻 (#192)

- **`consecutive_failures` 自愈链路解冻**: `PipelineOrchestrator._run_anomaly_detection`
  此前只检测→告警, `AutoHealer` 全部 runbook 永不触发 ⇒ 生产 alerts 里
  「异常检测: consecutive_failures」自 **2026-10-26 10:00** 创建后一直 `open`。
  新增 `_dispatch_autonomous_heal()`, 按**白名单**只放行
  `{consecutive_failures, pipeline_failure}` 两族 (非破坏性 failover);
  `switch_source` / `purge_old_data` / `reset_baseline` 等破坏性 runbook
  **仍保持 D-30 冻结**, 人工调用。
- **陈旧告警按证据收敛**: 原 `scripts/cleanup_stale_alerts.py` 是孤儿工具
  (无 crontab、无调用点), 生产 15 条 open 中最早追到 **2026-09-01**(71 天未解)。
  新增 `_reap_resolved_stale_alerts()` 挂入 `health_check`(不新增 cron):
  **只在「告警宿主管道此后已有 success 运行」时才关闭**, 并把恢复时间写进
  `action` 留痕; 无恢复证据的一律保持 open —— 拒绝「超时就关」式假绿。
- 测试: `tests/unit/ops_layer/test_p1a_alert_selfheal.py` (10 项)。

#### Fixed — P2-b 非法 OHLC 存量口径裁定

- 生产 `quality_checks` 自 **2026-10-05 起连续 30+ 天每日 `fail`**, 每天 2~8 条
  「非法 OHLC」。逐行取证 (`daily_klines` 2026-10-20~11-06) 后全部是三类
  **可解释事实**, 非坏数据:
  1. 停牌占位行 — `open/high/low/volume/amount = 0`, 仅 `close` 保留停牌前真实价
     (688496=0.58、688121=0.40 连续多日不变);
  2. 已退市极低价真实成交 (002731 萃华退 `close=0.22`);
  3. 显式登记在 `suspended_codes` 的停牌期 (000016 / 002717)。
- 原 SQL 以 `open <= 0` 一票判死 ⇒ 每天必然命中 ⇒ **判据恒红 = 信号常量化**
  (判例 #5), 真坏数据反被淹没。
- 裁定分层 (`src/data_layer/health/quality.py`):
  - `close` 必须有效且 `> 0` —— **不放宽**;
  - `open/high/low` 任一 `<= 0` 且 `volume = 0` ⇒ 无成交日, 计入新指标
    `suspended_rows`, 不再算非法;
  - 有成交量但 OHLC 缺失、或 `close` 越界 ⇒ **仍判非法**。
- 测试: `tests/unit/data_layer/test_p2b_ohlc_suspended_exemption.py` (8 项, 含变异验证)。

#### Fixed — P2-c freshness_audit 判定时点错位

- 生产 `freshness_audit` 逐日史: **2026-10-27 / 10-30 / 11-02 / 11-03** 均有
  **同样这 5 张表**判 `missing` (limit_up_ladder / limit_break_pool /
  hot_stock_history / skyrocket_list / stock_anomaly_reasons)。
- 关键复核: 用**生产同一份代码**对**同一份库**重跑
  `check_collect_freshness("2026-11-02")`, 这 5 张表**全部 `fresh=True`**
  (355 / 28 / 30 / 30 / 630 行)。⇒ 不是没数据、也不是判据写错, 是
  **16:22 的审计判了 16:40 才补的东西** (判例 #11: 判据执行时点 ≠ 数据到达时点)。
- 修法 (三态, 不假绿也不假红):
  - `DataReadiness.RECOLLECT_BACKED_TABLES` + `RECOLLECT_WINDOW_START` —
    仅当**审计的是当日且当前时刻早于补采轮计划时点**才记 pending;
    历史日/补采窗之后再查一律按原 `missing` 判 ⇒
    「交易日 0 行必须显形」这条硬约束不被洗掉;
  - 新状态 `delayed_by_recollect` 不入 missing 告警链;
  - `FreshnessAuditPipeline.reconcile_recollect_backed()` 在补采轮成功后
    按同一判据复核, 收敛为 `ok` / `missing` (补采也空 ⇒ 真断供必须显形);
    orchestrator `_run_shortterm_recollect` 已接线。
- 测试: `tests/unit/data_layer/test_p2c_recollect_timing.py` (9 项, 含变异验证)。

#### Verification

- 新增测试 27 项全绿; 关联回归 (freshness / quality / alert 共 122 项) 全绿;
  `ruff check src/` 全过。
- 生产库全程 `mode=ro`, 未写入任何数据; crontab 未改动。

---

## [v0.20.0-beta.1] - 2026-10-26

### Initial Open-Source Beta Release

#### Core Framework Architecture
- **Ten-Layer Decoupled Design**: Complete decoupling across Data, Strategy, Agent, Execution, Backtest, Report, Delivery, Ops, Config, and Memory layers with strict unidirectional dependencies.
- **Multi-Source Data Ingestion**: Automated fallback across multiple market data sources (AkShare, Tushare, Baostock, Easyquotation) with point-in-time (PIT) financial announcement alignment (`announced_at`).
- **Factor Pipeline Engine**: Cross-sectional MAD outlier clipping, OLS industry and market-cap neutralization, and Z-score standardization with pluggable `BaseFactor` interface.
- **Adaptive Weighting (`ic_feedback`)**: Daily tracking of rolling Spearman Rank IC and IC-IR to dynamically adjust factor weights and penalize deteriorating metrics.
- **Multi-Agent Decision Committee**: Multi-agent adversarial debate (Bull vs. Bear vs. Judge) with `reasoning_floor` CoT token budget insulation for reasoning models (DeepSeek-R1, hy3).
- **Execution State Machine**: Strict single-direction transition pipeline (`watch` -> `pending_entry` -> `active` -> `exit_pending` -> `exited`) protected by three entry gates (Age gate, Price reality gate, Risk-Reward `min_rr_ratio >= 1.45` gate).
- **Five-Tier Exit Engine**: Stepped protective stops (Hard stop, Trailing stop, ATR volatility stop, Technical death cross, and Time decay timeout) plus portfolio hysteresis drawdown circuit breaker (-8% stop open / -5% resume).
- **Walk-Forward Optimization (WFO)**: Out-of-sample forward walk backtest with Purged Cross-Validation to eliminate sample lookahead and forward-testing graduation gate.

#### Security & Operational Hardening (Hark Audit Fixes)
- **Dashboard Authentication Hardening**:
  - The auth proxy (`scripts/qs_dash_auth_proxy.py`) no longer supports short single-line passwords. Passwords now require either `user:at_least_12_chars` or `user:scrypt$salt$hash`.
  - For HTTP deployments behind internal reverse proxies, set `QS_DASH_COOKIE_SECURE=0` to permit unencrypted cookie transit.
  - Added strict session checks for all POST endpoints to prevent unauthenticated bypasses.
- **Docker Compose & API Token**:
  - When running via Docker Compose, `QS_API_TOKEN` must be configured in `.env` to prevent open API exposure behind reverse proxies.
- **HITL Approver Verification**:
  - Recommend configuring `HITL_APPROVER_OPEN_IDS` in `.env` to enforce approval authorization when interacting via Feishu/Lark action cards.
- **Backtest Fill Price Protocol**:
  - By default, backtests now simulate fills at next-day open price (`fill_price: next_open`) to eliminate same-day close forward bias. Set `fill_price: same_close` if needed to reproduce legacy baseline results.
- **Zero Secrets & Pure History**:
  - Complete history sanitized with Gitleaks (0 leaks verified) and all historical personal paths generalized.
