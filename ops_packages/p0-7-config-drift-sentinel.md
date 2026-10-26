# P0-7 配置漂移哨兵 — 排程接入包（只出包，不应用）

> 状态：**待统筹 22:05 窗口应用**。本文件是接入说明 + 待粘贴片段，不含任何已生效变更。
> 哨兵本体 = `scripts/config_drift_sentinel.py`（已随分支提交）。

## 一、哨兵行为回顾
- 每日对比**生效配置**（defaults→settings→profile→local 逐层合并，与 loader.py 同序）vs 基线快照
- 漂移 → 落档 `data/config_snapshots/drift_<ts>.json` + 飞书告警 + 退出码 1
- 合法变更后 `--rebase` 重打基线
- **不碰 qs.db**（只读 config yaml + 写 JSON 快照），故无需 flock_guard 写锁守卫

## 二、crontab 待加行（1 行）
排程时刻建议 **07:30**（早于 08:00 morning_brief，捕捉隔夜未授权改动；22:05 合法窗口变更后由统筹 rebase）。

```cron
30 7 * * * cd /path/to/qs && PYTHONPATH=src CONFIG_ENV=prod scripts/cron_watchdog.sh config_drift_sentinel /path/to/qs/.venv/bin/python scripts/config_drift_sentinel.py >> /path/to/qs/logs/config_drift.log 2>&1
```

说明：
- 用 `cron_watchdog.sh config_drift_sentinel` 包装（预算键见下），**不加** flock_guard（哨兵不写 qs.db）
- 日志落 `logs/config_drift.log`（logs/ 已 gitignored）

## 三、ops.yaml 待加预算键（cron_watchdog.overrides 段）
`config/defaults/ops.yaml` 的 `ops.cron_watchdog.overrides:` 下加一行：

```yaml
      config_drift_sentinel: 120
```

（哨兵秒级完成，120s 预算充裕；与 intraday_scan 同档）

## 四、应用后首跑验收
1. 应用 crontab + ops.yaml 后，手动跑一次确认无漂移：
   `cd /path/to/qs && PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/config_drift_sentinel.py --no-alert`
   期望：`无漂移` 或首次自动打基线，rc=0
2. **22:05 窗口改完 ops.yaml/local.yaml 后必须 rebase**：
   `cd /path/to/qs && PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/config_drift_sentinel.py --rebase`
3. 次日 07:30 观察 `logs/config_drift.log` 与飞书（无告警=正常）

## 五、告警语义
- 飞书卡片标题 `⚠️ QS 配置漂移`，force_channel=feishu_dm（与审批卡同锁，防进群误触）
- 卡片列出差异类别 + 前 5 个键路径 + 详情文件路径 + rebase 指引
- 告警发送失败静默降级（哨兵不因告警失败而哑），但漂移报告文件必落档
