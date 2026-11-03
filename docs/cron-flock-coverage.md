# cron flock 覆盖判定表 (#16)

> 判定口径: **该 cron 行最终是否会写 qs.db**。写库必须走 `flock_guard.sh`；纯只读/纯文件操作不需要。
> 本表是单一事实源 —— 新增 cron 行时先在此登记理由，再由测试 `test_cron_flock_coverage.py` 钉住。

## 一、需要加锁（当前缺失，本卡补齐）

| 管道 | 触发 | 写库路径 | 依据 |
|---|---|---|---|
| `morning_brief` | 08:00 | delivery_log（推送落痕） | `_run_morning_brief` → `send` → `log_store.INSERT INTO delivery_log` |
| `morning_review` | 09:35 | delivery_log | 同上 |
| `midday_brief` | 12:00 | delivery_log | 同上 |
| `weekly_summary` | 周六 08:00 | delivery_log | 同上 |
| `monthly_report` | 月1日 22:30 | delivery_log | 同上 |
| `auction_parse` | 09:25 | stock_pool（竞价结果回写） | `_run_auction_parse` → `run_pipeline("auction_collect")` + `get_by_status` |
| `param_tuning` | 周日 22:00 | strategy_params（门控开启时） | `_persist_strategy_params` → UPDATE/INSERT |
| `wfo_gate` | 周日 20:00 | backtest_runs / strategy_params | `_apply_wfo_tier` + `evaluate` |
| `model_retrain` | 月1日 22:00 | model registry（文件+表） | `ModelRegistry` 注册 |

## 二、不需要加锁（只读 / 纯文件）

| 管道 | 触发 | 为什么不加 | 硬证据 |
|---|---|---|---|
| `pre_market_refresh` | 09:00 | 只做数据就绪检查，不落库 | `_run_pre_market_refresh` 体仅 `DataReadiness(...).check()` + `HandlerResult`，AST 无 INSERT/UPDATE/DELETE/commit |
| `build_morning_brief` | 08:05 | `--dry-run`，只读 | 命令行带 `--dry-run` |
| `build_midday_brief` | 12:05 | `--dry-run`，只读 | 同上 |
| `e2e_verification` | 09:45 / 18:30(线上20:30) | 只读验证，写 jsonl 日志非 DB | 直接跑 `scripts/e2e_verification.py`，不产 pipeline_runs 行 |
| `log_rotation` | 23:30 | 纯文件操作 | `_run_log_rotation` AST 无任何 DB 调用 |
| `disk_check` | 每小时 | 只读 `os.statvfs` + 恢复时 resolve 告警 | 告警 resolve 是短 UPDATE，且自带去重，不加锁（见备注） |
| `api_health_check` | 每5分钟 | 只读 HTTP 探活 + jsonl | 高频，加锁反而自伤 |

**备注（disk_check 与 api_health_check 的例外理由必须写清，否则后人会当成漏网）**：
- `disk_check` 唯一的写是「磁盘恢复时 resolve 告警」，属**幂等重置**，且 AlertManager 自身有状态机去重；每小时一次的频率不可能与业务腿形成持锁竞争。加锁收益为零、成本是每小时多一次锁文件操作。
- `api_health_check` 每 5 分钟一次，加锁会与 `intraday_scan`（同为 */5）在同一分钟抢锁，反而制造 skip。

## 三、判定方法（可复核）

```bash
# 1) 列出所有未走 flock 的行
awk '$0 !~ /^#/ && $0 !~ /^$/ && $0 !~ /flock_guard/' deploy/quantstar-crontab

# 2) 对每条，AST 查 handler 是否触碰 DB
python -c "import ast,sys; ..."   # 见 tests/unit/ops_layer/test_cron_flock_coverage.py

# 3) 判据: 命中 INSERT/UPDATE/DELETE/commit/executemany → 必须加锁
```

## 四、维护纪律

1. 新增 cron 行 → 先在此表登记，再改 `deploy/quantstar-crontab`；
2. `test_cron_flock_coverage.py` 会扫描 crontab，任何**未登记**的裸行都会报红；
3. 「不需要加锁」必须附理由 + 硬证据，**不得只写"只读"两字**（那是断言不是证据）。
