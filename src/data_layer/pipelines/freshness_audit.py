"""pipelines/freshness_audit.py — DL-12 数据新鲜度审计管线

接线 DataReadiness (F-07/F-15/F-16): 把 check()/check_collect_freshness() 结果
落 freshness_audit 表, 供运维日报消费 (D98-D100)。

freshness_audit(date, table, expected_by, actual_at, delay_min, status)
- expected_by: 审计日当日 (交易日数据应当日到位)
- actual_at:   该表实际最新日期 (MAX(date)), 无数据为 NULL
- delay_min:   落后审计日的分钟数 (0=当日到位, >0=滞后, NULL=无数据)
- status:      ok / delayed / missing
"""

from __future__ import annotations

from datetime import datetime

from common.logging import get_logger
from data_layer.health.readiness import DataReadiness
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.freshness_audit")

# N4 甲-1: 告警标题跨数量稳定 —— 同名 open 抑制靠它去重
ALERT_TITLE = "数据新鲜度: 当日有表缺数"

# 哨兵④ (09-05): stock_financials 公告新鲜度规则默认阈值。
# 生产值从 config/defaults/ops.yaml freshness_audit 专段读 (独立块,
# 避免与其他分支的 ops.yaml hunk 冲突); 此处仅 no-config 兜底。
_ANNOUNCE_TABLE = "stock_financials_announce"
_DEFAULT_ANNOUNCE_CFG = {
    # 默认=统筹示例值 (no-config 兜底)。故意与 ops.yaml 生产值 [1,4,7,8,10]
    # (A 股中报窗含 8 月) 保持分叉 — 两值不同是 test_yaml_config_loaded 的
    # 通电证明锚 (默认回落时 8 月走非财报季腿, 测试翻转), 同步改动须带测试。
    "fiscal_season_months": [1, 4, 7, 10],
    "in_season_stale_days": 3,   # 财报季公告停滞 >3 自然日 → delayed (RED 级)
    "off_season_stale_days": 45, # 非财报季 >45 天 → delayed (WARN 级)
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS freshness_audit (
    date TEXT NOT NULL,
    table_name TEXT NOT NULL,
    expected_by TEXT NOT NULL DEFAULT '',
    actual_at TEXT,
    delay_min INTEGER,
    status TEXT NOT NULL DEFAULT 'missing',
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (date, table_name)
);
"""


def _days_behind(audit_date: str, actual: str | None) -> int | None:
    """actual 落后 audit_date 的天数; actual 为空/非法返回 None。"""
    if not actual:
        return None
    try:
        a = datetime.strptime(audit_date, "%Y-%m-%d").date()
        b = datetime.strptime(actual[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return max(0, (a - b).days)


class FreshnessAuditPipeline(BasePipeline):
    """数据新鲜度审计 — 每日管线末端执行, 结果落 freshness_audit 表。"""

    name = "freshness_audit"
    requires = []
    produces = ["freshness_audit"]

    def __init__(self, db=None):
        self.db = db
        # 哨兵④: 公告新鲜度配置 (ops.yaml freshness_audit.announce_* 优先,
        # 未配置回落默认)。测试可注入 announce_cfg 覆盖。
        self.announce_cfg = dict(_DEFAULT_ANNOUNCE_CFG)
        self._load_announce_cfg()

    def _load_announce_cfg(self):
        """从 config/defaults/ops.yaml 读 ops.pipelines.freshness_audit.announce_*。

        铁律 #12: 加载失败不静默 — 首版病灶: 模块名误写 common.config (不存在)
        + 段路径少 ops.pipelines 两层, except 吞掉 → yaml 值永不被消费的
        「建了没通电」形态 (probe37~41 取证)。失败必须 warn 可见后回落默认。
        """
        try:
            from config_layer import get_config
            cfg = get_config()
            if not cfg._loaded:
                cfg.load()
            ops = cfg.get_section("ops.pipelines.freshness_audit") or {}
        except Exception as e:
            logger.warning(
                "freshness_audit announce 配置加载失败, 回落默认 %s: %s",
                dict(_DEFAULT_ANNOUNCE_CFG), e)
            return
        if not isinstance(ops, dict):
            return
        # yaml 键带 announce_ 前缀 (专段独立块), announce_cfg 键不带
        for ykey, ckey in (
                ("announce_fiscal_season_months", "fiscal_season_months"),
                ("announce_in_season_stale_days", "in_season_stale_days"),
                ("announce_off_season_stale_days", "off_season_stale_days")):
            if ykey in ops:
                self.announce_cfg[ckey] = ops[ykey]

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        conn.executescript(_SCHEMA)

        readiness = DataReadiness(db)
        errors: list[str] = []
        written = 0

        # 主链路必需表 (readiness.check)
        report = readiness.check(input.date)
        for table, ts in report.tables.items():
            written += self._write_row(
                conn, input.date, table, ts.last_date, ts.has_data)

        # 冷采集表 (check_collect_freshness): 只有当日行数, 无 MAX(date),
        # 用"当日有行=ok, 无行=missing"简化 (冷采集不进选股链路, 宽松)。
        # 卡组二刀1 跨接线修复 (2026-10-20): readiness 侧假日守卫返回
        # {"fresh": None, "skipped": "holiday"} — 必须在此显式识别并穿透,
        # 否则 bool(None)=False → status='missing' 照写照告警, 守卫被
        # 消费层一笔抹掉 (判例#150 族「守卫没跨接线」, 10-24 假日翻车现场)。
        collect = readiness.check_collect_freshness(input.date)
        for table, info in collect.items():
            skipped = bool(info.get("skipped"))
            fresh = bool(info.get("fresh"))
            actual = input.date if fresh else None
            written += self._write_row(conn, input.date, table, actual, fresh,
                                       skipped=skipped)

        # 哨兵④ (09-05): stock_financials 公告新鲜度 — 独立键, 与上面按
        # date (报告期末日) 的旧行 (恒滞后假警报 95,040min 同族) 区分。
        # 语义: 财务数据"新鲜"的口径是公告日推进, 不是报告期末日。
        written += self._write_announce_row(conn, input.date)

        conn.commit()

        # N4 甲-1: 判了 missing 必须广播。此前 freshness 判定正确、落库正确,
        # 但没接告警链 —— 09-08 判了 lhb_daily missing 而当天零告警,
        # 断采连续 4 日无人知。与 AUD-022 同形状: 有判定, 没广播。
        self._alert_missing(conn, input.date)

        status = "success" if not errors else "partial"
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=written, errors=errors,
            metadata={"tables_audited": written},
        )


    def _alert_missing(self, conn, audit_date: str) -> int:
        """当日判为 missing 的表聚合成**一条**告警。返回 missing 表数。

        两条硬约束:
        1. **一天一条, 不逐表轰炸** —— 表名进 detail 不进 title。
           title 跨数量稳定, 让 AlertManager 的同名 open 抑制真能去重;
           title 里塞数量 = 每天一条新 open = 积压天天涨 (AUD-019 的成因:
           告警没人收等于没有, 而刷屏是"没人收"最快的成因)。
        2. **fail-safe 但不静默** —— 告警失败只 warning 不阻断审计,
           但必须留痕 (同 AUD-005 `_alert_settlement_deferred` 既定样板)。
        """
        rows = conn.execute(
            "SELECT table_name FROM freshness_audit "
            "WHERE date = ? AND status = 'missing' ORDER BY table_name",
            (audit_date,)).fetchall()
        tables = [r[0] for r in rows]
        if not tables:
            return 0
        logger.warning(
            f"数据新鲜度: {audit_date} 有 {len(tables)} 张表当日缺数 -> {tables}")
        try:
            from ops_layer.alert_manager import AlertManager
            from ops_layer.core.models import Alert
            AlertManager(self.db).send(Alert(
                severity="warning",
                layer="data",
                title=ALERT_TITLE,               # 跨数量稳定, 供同名 open 去重
                detail=(f"审计日 {audit_date}: {len(tables)} 张表当日无数据 -> "
                        f"{', '.join(tables)}"),
                action="查对应采集腿当日跑账 (pipeline_runs) 与源可用性; "
                       "确认属「源侧无数据」还是「腿没跑到」再决定补采",
            ))
        except Exception as e:                    # noqa: BLE001 — 告警不阻断审计
            logger.warning(
                f"数据新鲜度 missing 告警发送失败 (审计结果已落库, 不阻断): {e}")
        return len(tables)

    # QS 卡 financials-q3-window-guard 件③ (2026-10-02): 季报表宽容窗口。
    # stock_financials 的 date=报告期末日 (季度语义), 对「当日」判恒滞后 91 天
    # (delay=131040min 恒假警报)。季表判据 = actual 为最近季度末 且
    # today <= 季末 + 披露窗宽 (45 天, 与哨兵④ announce_off_season_stale_days
    # 同源) → ok; 超窗 → delayed (真断血显形)。
    _FISCAL_TABLES = {"stock_financials"}
    _FISCAL_GRACE_DAYS = 45

    @staticmethod
    def _fiscal_window_ok(audit_date: str, actual: str) -> bool:
        """季表窗口判: actual 是否仍是「最新应有报告期」。

        语义: actual 所在季的**下一季披露窗未开** → actual 仍是最新应有 → True
        (表级滞后=季度语义, 非断血)。下一季披露窗起点 = 季末次月起 + 宽限
        (A 股披露窗: 季报季末后 1~2 个月内集中披露, 与哨兵④ announce_off_
        season_stale_days=45 同源 — 窗口=季末 + 45 天后下一季才「应有」)。
        """
        from datetime import date as _d
        try:
            a = _d.fromisoformat(actual[:10])
            t = _d.fromisoformat(audit_date[:10])
        except ValueError:
            return False
        # actual 所在季的季末
        qm = ((a.month - 1) // 3 + 1) * 3
        # qend_day = {3: 31, 6: 30, 9: 30, 12: 31}[qm]
        # qend = _d(a.year, qm, qend_day)
        # 下一季「应有」的终点 = 下一季季末 + 宽限 45 天 (下一季披露集中期止)。
        # Q2 (06-30) 行在 Q3 季末 09-30 + 45 = 11-14 前都是「最新应有」—
        # Q3 披露窗 10 月开但集中披露 10 月底才收官, 期间 Q2 行非断血。
        from datetime import timedelta as _td
        nqm, ny = (qm + 3, a.year) if qm < 12 else (3, a.year + 1)
        nqend = _d(ny, nqm, {3: 31, 6: 30, 9: 30, 12: 31}[nqm])
        next_due = nqend + _td(days=45)
        # audit 日在下一季披露收官之前 → actual 仍是最新应有 → 窗口内
        return t < next_due

    def _write_row(self, conn, audit_date: str, table: str,
                   actual: str | None, has_data: bool,
                   skipped: bool = False) -> int:
        if skipped:
            # 假日工件表 (卡组二刀1): 落 status='skipped' 显形 —
            # 「假日缺」不是「无信息」 (铁律#12)。delay=None (无滞后语义),
            # 不入 missing → 告警链 (status='missing' 过滤) 天然豁免。
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO freshness_audit "
                    "(date, table_name, expected_by, actual_at, delay_min, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (audit_date, table, audit_date, None, None, "skipped"),
                )
                return 1
            except Exception as e:
                logger.warning(f"freshness_audit write failed for {table}: {e}")
                return 0
        behind = _days_behind(audit_date, actual)
        if behind is None:
            if has_data:
                # 有数据但无日期维度 (如 stock_info 静态表) → 视为 ok
                status, delay = "ok", 0
            else:
                status, delay = "missing", None
        elif behind == 0:
            status, delay = "ok", 0
        elif table in self._FISCAL_TABLES and actual and self._fiscal_window_ok(audit_date, actual):
            # 件③: 季表窗口内 → ok (delay 仍如实记滞后分钟, 状态不打红)
            status, delay = "ok", behind * 1440
        else:
            status, delay = "delayed", behind * 1440
        try:
            conn.execute(
                "INSERT OR REPLACE INTO freshness_audit "
                "(date, table_name, expected_by, actual_at, delay_min, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (audit_date, table, audit_date, actual, delay, status),
            )
            return 1
        except Exception as e:
            logger.warning(f"freshness_audit write failed for {table}: {e}")
            return 0

    def _write_announce_row(self, conn, audit_date: str) -> int:
        """哨兵④: MAX(announce_date) 停滞审计 — 财报季 3d / 非财报季 45d。

        status 语义与旧规则对齐 (ok / delayed / missing), 但阈值独立:
        - 财报季 (fiscal_season_months): 停滞 > in_season_stale_days → delayed
        - 非财报季: 停滞 > off_season_stale_days → delayed
        - announce_date 全空 → missing (表在但公告列无数据)
        - 表不存在 → missing (管线未首跑, 静默不炸)
        """
        cfg = self.announce_cfg
        try:
            row = conn.execute(
                "SELECT MAX(announce_date) FROM stock_financials").fetchone()
            actual = row[0] if row else None
        except Exception as e:
            logger.debug(f"stock_financials/announce_date 探测失败: {e}")
            actual = None
            row = None

        if actual:
            behind = _days_behind(audit_date, actual)
        else:
            behind = None

        if row is not None and actual:
            month = int(audit_date[5:7])
            in_season = month in tuple(cfg.get("fiscal_season_months", ()))
            limit = (cfg.get("in_season_stale_days")
                     if in_season else cfg.get("off_season_stale_days"))
            if behind is not None and behind > int(limit):
                status, delay = "delayed", behind * 1440
            else:
                status, delay = "ok", 0
        elif row is not None:
            # 表在但 announce_date 全 NULL → missing (列无数据, 非表缺)
            status, delay = "missing", None
        else:
            status, delay = "missing", None

        try:
            conn.execute(
                "INSERT OR REPLACE INTO freshness_audit "
                "(date, table_name, expected_by, actual_at, delay_min, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (audit_date, _ANNOUNCE_TABLE, audit_date, actual, delay, status),
            )
            return 1
        except Exception as e:
            logger.warning(f"freshness_audit write failed for "
                           f"{_ANNOUNCE_TABLE}: {e}")
            return 0


# N4 甲-1 对账守卫: 把「判了没广播」做成可断言的对账。
# 不做成断言的话, 下次断线又是几个月无人知 —— lhb 这次就是 (09-07 起连续缺,
# freshness 每天判 missing, 告警面一条没有, 直到 C 席读数才发现)。
def reconcile_missing_alerts(conn, audit_date: str) -> dict:
    """当日有 missing 行就必须有对应告警。返回 {missing, tables, alerted}。"""
    rows = conn.execute(
        "SELECT table_name FROM freshness_audit "
        "WHERE date = ? AND status = 'missing' ORDER BY table_name",
        (audit_date,)).fetchall()
    tables = [r[0] for r in rows]
    try:
        n_alert = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE title = ? AND detail LIKE ?",
            (ALERT_TITLE, f"%{audit_date}%")).fetchone()[0]
    except Exception:
        n_alert = 0
    return {"date": audit_date, "missing": len(tables), "tables": tables,
            "alerted": bool(n_alert) if tables else True}
