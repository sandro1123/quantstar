# -*- coding: utf-8 -*-
"""QS 卡 holiday-early-exit-gate — 假日 gate 判定模块 (Day2, 2026-10-07)。

病: run_pipeline 入口无 is_trading_day gate — 国庆假日 (10-02/05/06) DDP
照跑, 快照类源把假日键贴满 5 表 (10695 行假日假数据, 判例「快照类源假日
贴键两型」: moneyflow 型贴请求日键有害)。

法: 入口级 early-return。TradingCalendar.is_trading_day(today)=False 且
管道不在 ops.holiday_exempt_pipelines 白名单 → skipped 落库 (reason 含
'non-trading day' + 当日日期 — T1 硬约束: 假日 skip 须可见痕迹, 判例
#147 反面「假日绿≠源挂绿」)。

豁免名单消费 ops.yaml (声明式, 判例 #159 豁免须受管+理由在册):
  db_backup / incremental_backup / restore_drill / disk_check /
  log_rotation / marimo_daily — 运维面假日照跑。
fail-open (判例 #147 第三形态): 日历故障放行不拦正常跑 — 假日误跑
(快照贴键, 有清理面) 好过交易日误拦 (断数据, 无自愈)。
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("data.holiday_gate")

from common.calendar import TradingCalendar  # noqa: E402

# 豁免名单 (声明式单一事实源; 与 ops.yaml ops.holiday_exempt_pipelines
# 同步 — 代码侧默认值兜底 no-config 场, yaml 优先消费)
HOLIDAY_EXEMPT_PIPELINES = frozenset({
    "db_backup", "incremental_backup", "restore_drill",
    "disk_check", "log_rotation", "marimo_daily",
})

_SKIPPED_REASON = "non-trading day (holiday early-exit gate)"


def _exempt_from_config(name: str) -> bool:
    """读 ops.yaml ops.holiday_exempt_pipelines (声明式消费面)。

    失败回落代码侧默认名单 (不静默 — warn 留痕, 铁律 #12)。
    """
    try:
        from config_layer import get_config
        cfg = get_config()
        if not cfg._loaded:
            cfg.load()
        listed = cfg.get("ops.holiday_exempt_pipelines")
        if isinstance(listed, (list, tuple)) and listed:
            return name in set(listed)
    except Exception as e:  # noqa: BLE001 — 配置缺失回落默认
        logger.warning("holiday_gate exempt 配置读取失败, 回落默认名单: %s", e)
    return name in HOLIDAY_EXEMPT_PIPELINES


def should_holiday_skip(db, name: str, today: str) -> bool:
    """假日早退判定 (T1/T2/T3 判定面)。

    返回 True = 该管道今日应早退 (假日+非白名单)。
    False = 照常跑 (交易日 / 白名单 / 日历故障 fail-open)。
    """
    # T3: 白名单先判 (豁免不受日历故障影响 — 运维面任何日子都该跑)
    if _exempt_from_config(name):
        return False
    try:
        cal = TradingCalendar(db)
        is_trading = cal.is_trading_day(today)
    except Exception as e:  # noqa: BLE001 — T3c fail-open 带痕
        logger.warning(
            "holiday_gate 日历故障 fail-open (放行 %s @ %s): %s: %s",
            name, today, type(e).__name__, e)
        # 判例 #153 告警须可见载体: fail-open 落 alerts 一行 (直 INSERT
        # fail-safe, 与 Day3 卡二同款 — 告警失败只 warning 不阻断判定)。
        try:
            conn = db.get_connection()
            conn.execute(
                "INSERT OR IGNORE INTO alerts "
                "(severity, layer, title, detail, status) "
                "VALUES ('warning', 'data', '假日gate日历故障放行', ?, 'open')",
                (f"holiday_gate fail-open: {name} @ {today} "
                 f"{type(e).__name__}: {e} — 日历故障放行 (假日误跑有清理面,"
                 f" 交易日误拦无自愈); 请查 TradingCalendar",))
            conn.commit()
        except Exception as ae:  # noqa: BLE001 — 告警不阻断判定
            logger.warning("holiday_gate fail-open alerts 落行失败: %s", ae)
        return False
    return not is_trading


def skipped_reason(today: str) -> str:
    """T1 落库面 reason 文案 (须含 'non-trading day' + 当日日期)。"""
    return f"{_SKIPPED_REASON} @ {today}"
