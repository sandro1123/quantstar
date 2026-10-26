# Changelog

All notable changes to QuantStar will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
