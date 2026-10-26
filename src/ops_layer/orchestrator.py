"""管道编排 — 管道定义加载 + DAG 依赖检查 + 超时控制 + 重试 + 告警"""

from __future__ import annotations

import os
import random
import sqlite3
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

from common.database import Database
from common.logging import get_logger
from common.utils import bj_date, bj_now, validate_identifier
from config_layer import Config, cfg_get
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import HandlerResult, PipelineInfo, PipelineRunResult
from ops_layer.ghost_registry import GhostRegistry
from ops_layer.pipeline_logger import PipelineLogger

logger = get_logger("ops.orchestrator")

# DL-51: daily_data_pipeline 的子管线清单 — 模块级常量, 供 _run_data_pipeline
# 与 schedule-check 第三方对账 (注册表 vs ops.yaml) 共用同一事实源。
# 对账逻辑: data_layer 注册的管线, 要么是 ops.yaml 顶层管道, 要么是本清单的
# 子管线 — 两者都不是即"孤立节点" (DL-33 病灶: 注册了但 ops 层不知道)。
# DDP c-1 (#81 两连超时修复, 统筹 sm-6e53bd96df784290): analyst_forecasts
# 拆出日管道串行链 → 独立 16:55 顶层管道 (dispatch 见 _execute_pipeline,
# 排程见 ops.yaml)。DDP 串行需求立降 ~1830s: 2738→908s / 3223→1393s,
# 三天实测全回 2700 预算内。下游零硬依赖 (设计稿 §2: 唯一实质消费者
# AnalystConsensusFactor 用 date <= ? 降序取, 缺当日=沿用旧值不崩;
# 16:20/16:22 两 DL-33 门禁对 analyst 均零依赖)。
DAILY_SUB_PIPELINES = [
    "stock_list", "kline", "fuyao_financial", "valuation", "moneyflow",
    "concept_daily", "lhb_daily", "market_sentiment",
    "stock_news", "stock_limit_up_down",
]
# 条件追加项 (未注册则跳过, 但纳入对账白名单 — 它们有合法宿主 daily_data_pipeline)
DAILY_SUB_PIPELINES_CONDITIONAL = ["shortterm_collect", "index_daily"]
# 由其他顶层 handler 内部调用的管线 (非顶层、非 daily 子管线) — 对账白名单。
# auction_collect: 由 _run_auction_parse (DL-22 竞价冷采集) 调用。
HANDLER_HOSTED_PIPELINES = ["auction_collect"]

# 交易日全市场 K 线最低覆盖阈值: 低于此值说明当日行情采集严重残缺,
# 不应降级为 partial success 而掩盖失败 (e.g. 只抓到 100/5540 只)。
MIN_KLINE_ROWS = 4000

# B4 (2026-10-21): DDP T+1 腿豁免注册表 —— 与
# data_layer/health/readiness.py::T1_COLLECT_TABLES 同源
# (index_daily/lhb_daily 由 akshare_sina 次日 ~08:03 落库, DDP 15:30 跑时
# 当日行必然未落)。判据与 freshness_audit 同口径: 查「上一交易日」
# (daily_klines MAX(date)<date) 而非当日 —— 上一交易日有行=源履约(豁免),
# 上一交易日无行=源该发未发(真缺, 不豁免), 无日历锚=保守不豁免(哨兵不哑)。
# 边界: 只动 DDP best_effort 链内的这两腿; :785 双腿直调 handler 不动;
# 其余 9 条 best_effort 腿维持现状不扩大; 只豁免 partial 形态
# (源干净返回空=尚未发布的 benign 语义), failed/0-records 维持现状。
DDP_T1_EXEMPT_LEGS = ("lhb_daily", "index_daily")


def _t1_leg_on_track(db, table: str, date: str) -> bool:
    """T+1 腿上一交易日有行即履约。db 为 data db (可为 None)。"""
    if table not in DDP_T1_EXEMPT_LEGS:
        return False
    try:
        from common.utils import validate_identifier
        if not validate_identifier(table, "table"):
            return False
        if db is None:
            return False
        conn = db.get_connection()
        row = conn.execute(
            "SELECT MAX(date) FROM daily_klines WHERE date < ?", (date,)
        ).fetchone()
        prev = row[0] if row else None
        if not prev:
            return False
        cnt = conn.execute(
            "SELECT COUNT(*) FROM %s WHERE date = ?" % table, (prev,)
        ).fetchone()
        return bool(cnt and cnt[0] > 0)
    except Exception:
        return False

# ST-31 (F-38): 专家因子权重健康阈值 —— factor_weights 中 20 个专家标定
# 因子 (DEFAULT_WEIGHTS) 的权重合计低于此值, 说明 IC 衰减已把专家先验
# 稀释 (实测事故: 稀释到 46.7%), 权重虽照写但必须告警。
MIN_EXPERT_WEIGHT_SUM = 0.9
# AUD-011: 上面这个常量是「20 因子时代」的不变式 —— DEFAULT_WEIGHTS 自身合计 1.0,
# 故当年"专家因子应占 >=90% 权重"成立。生产已扩到 89 因子并整体归一化, 20 个专家
# 因子的**均匀份额**只有 20/89 = 0.2247, 阈值 0.9 等于要求 20/89 个因子独占 90%
# 权重 —— 口径错配, 导致 ic_feedback 连续 14+ 交易日恒 partial + 每日假降级告警
# (2026-09-11 实测: 89 因子 sum=1.0, 专家 sum=0.2086 = 均匀份额的 93%, 健康)。
# 正确不变式 = 专家因子权重和 / 其均匀份额 (与因子总数无关, 跨代稳定)。
# 旧常量保留: 既有 ST-31 测试 import 它来记录"这个夹具是稀释形态"的语义。
MIN_EXPERT_SHARE_RATIO = 0.7

# Q4 IC 质量闸门守卫: 近 30 日真 IC 证据 (factor_ic 行数, 含当日 record_ic
# 累计) 低于此值时, ic_decay 直接 no-op 不稀释 + warning 留痕。ST-30 教训:
# 证据不足时的 decay 稀释 = 噪声写库 (88 行脏权重事故)。与 ic_tuner 内部
# 单因子 len(ics)<5 保持现权重同阈值, 这里是 handler 级总闸。
MIN_IC_EVIDENCE = 5


def _child_timing_jsonl_path() -> str:
    """子管道计时事件 jsonl 路径 (QS_CHILD_PIPELINE_TIMING 可覆盖, 供测试)。"""
    override = os.environ.get("QS_CHILD_PIPELINE_TIMING")
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "logs", "child_pipeline_timing.jsonl")


def _emit_child_pipeline_event(event: dict) -> None:
    """子管道计时/结果事件落 jsonl (铁律 #12 可观测不可静默)。

    事件名 child_pipeline_start / child_pipeline_end — 独立命名, 不占用
    D flock 等待方案预留的 waited_lock/acquired_after_wait/wait_timeout。
    写失败降级 stderr, 绝不影响管道主流程。
    """
    import json
    try:
        path = _child_timing_jsonl_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[child_pipeline_timing][EVENT-FAILED] 无法写 jsonl: {e}; event={event}",
              file=sys.stderr, flush=True)


def _run_age_s(start_time: str | None, now_str: str) -> float | None:
    """running 行龄(秒); start_time 缺失/不可解析返回 None (调用方保守处理)。"""
    if not start_time:
        return None
    try:
        start = datetime.strptime(str(start_time), "%Y-%m-%d %H:%M:%S")
        now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (now - start).total_seconds()


def _rationale_id_for_code(db, code: str, date: str) -> str | None:
    """R-2 方案② (10-19): 按 (code, rec_date) 取 rationale_id, chairman 行优先。

    口径与消费面 load_argument_from_rationale (report_layer/advisor/rationale_join.py:77-84)
    逐位同序: ORDER BY CASE WHEN expert_name='chairman' THEN 0 ELSE 1 END,
    expert_name, rowid LIMIT 1。两处读同一行的同一行 → JOIN 锚一致。

    失败语义: 表缺/查不到/异常 → None (调用方填空串 + log 显形, 不阻塞
    tracking — 铁律#12; 「读不到」≠「无依据」由调用方话术分层)。
    """
    if db is None or not code:
        return None
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT rationale_id FROM recommendation_rationale "
            "WHERE code=? AND rec_date=? "
            "ORDER BY CASE WHEN expert_name='chairman' THEN 0 ELSE 1 END, "
            "expert_name, rowid LIMIT 1",
            (code, date),
        ).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def _resolve_stock_name(code: str) -> str:
    """从 stock_info 表查询股票名称；查不到时返回 [code] 兜底"""
    try:
        conn = Database().get_connection()
        r = conn.execute("SELECT name FROM stock_info WHERE code=?", (code,)).fetchone()
        if r and r[0]:
            return r[0]
        logger.warning(f"stock name not found: {code}")
        return f"[{code}]"
    except Exception:
        logger.warning(f"stock name lookup failed: {code}")
        return f"[{code}]"


def _load_ref_factor_values(conn, ref_dates) -> dict:
    """AUD-021: 一次装载参考截面, 按因子名分桶。

    病根 (漂移哨兵旧实现): `for fname in 因子` 内套 `for rd in 参考日` 内
    `SELECT factor_scores FROM factor_daily WHERE date=?` —— 同一批参考日数据
    被**逐因子重复查询 + 重复 JSON 反序列化**。量级: 89 因子 x ref_window(默认 10)
    x 约 5,500 行 = 约 490 万次 JSON 解析 (ref_window=20 时约 980 万次)。
    该段当前被 factor_drift.enabled=False 挡着 (休眠雷), 但该开关自述
    "通电=纯配置翻转", 一行翻开即引爆 —— 故通电前先修。

    修法: 参考日循环提到外层, 每个参考日只查一次、每行只解析一次, 按因子名分桶。
    查询次数从 因子数 x 参考日数 降为 参考日数 (约 89 倍)。
    容错语义与旧实现一致: 坏 JSON 行跳过不致命, None 值不入桶。
    """
    import json as _json

    out: dict[str, list[float]] = {}
    for rd in ref_dates:
        rows = conn.execute(
            "SELECT factor_scores FROM factor_daily "
            "WHERE date = ? AND factor_scores IS NOT NULL",
            (rd,)).fetchall()
        for (fs_json,) in rows:
            try:
                fs = _json.loads(fs_json) if isinstance(fs_json, str) else {}
            except (ValueError, TypeError):
                continue
            if not isinstance(fs, dict):
                continue
            for fname, v in fs.items():
                if v is not None:
                    out.setdefault(fname, []).append(float(v))
    return out



def _resolve_ic_target_date(db, lag_days: int) -> str:
    """ST-31 (F-38): 从今天(北京时区)往回数 lag_days 个「交易日」。

    旧实现按自然日减 (now - timedelta(days=lag_days)), 周末/节假日落在
    非交易日 → factor_daily 该日无行 → IC 环当天静默跳过, 连续多日无人知。
    TradingCalendar 有 db 时从 daily_klines 加载真实交易日集合, 否则退化
    为周末+内置节假日判断。
    """
    from common.calendar import TradingCalendar

    cal = TradingCalendar(db=db)
    d = bj_date()
    for _ in range(max(int(lag_days), 0)):
        d = cal.prev_trading_day(d)
    return d


# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
class PipelineOrchestrator:
    def __init__(self, db: Database, pipeline_logger: PipelineLogger, alert_manager: AlertManager, config: Config | None = None):
        self.db = db
        self.pipeline_logger = pipeline_logger
        self.alert_manager = alert_manager
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=8)
        self._pipeline_configs: dict[str, dict] = {}
        self._app = None
        self._consecutive_failures: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        # P0-7 方向C: 超时 ghost 登记表 (只观测不杀线程, 铁律 #12)
        self.ghosts = GhostRegistry()

    def set_app(self, app):
        self._app = app

    def _min_kline_rows(self) -> int:
        try:
            if self.config is not None:
                v = self.config.get("ops.min_kline_rows")
                if v is not None:
                    return int(v)
            from config_layer import cfg_get
            v = cfg_get("ops.min_kline_rows")
            if v is not None:
                return int(v)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"min_kline_rows config fallback: {e}")
        return MIN_KLINE_ROWS

    def _get_app(self):
        if self._app:
            return self._app
        from bootstrap import bootstrap_app
        self._app = bootstrap_app()
        return self._app

    def load_pipelines(self, config: dict):
        """从 ops.yaml 加载管道定义"""
        self._pipeline_configs = config.get_section("ops.pipelines") if hasattr(config, "get_section") else config.get("ops", {}).get("pipelines", {})
        logger.info(f"Loaded {len(self._pipeline_configs)} pipeline definitions")

    def get_pipeline_config(self, name: str) -> dict | None:
        return self._pipeline_configs.get(name)

    def list_pipelines(self) -> list[PipelineInfo]:
        return [
            PipelineInfo(
                name=name,
                schedule=cfg.get("schedule", ""),
                timeout_s=cfg.get("timeout_s", 300),
                max_retries=cfg.get("max_retries", 1),
                depends_on=cfg.get("depends_on", []),
                extra={k: v for k, v in cfg.items() if k not in ("schedule", "timeout_s", "max_retries", "depends_on")},
            )
            for name, cfg in self._pipeline_configs.items()
        ]

    def _dep_data_probe_enabled(self) -> bool:
        """OPS-02 数据探针开关 (ops.dep_data_probe.enabled, 默认开)。"""
        try:
            if self.config is not None:
                v = self.config.get("ops.dep_data_probe.enabled")
                if v is not None:
                    return bool(v)
            from config_layer import cfg_get
            v = cfg_get("ops.dep_data_probe.enabled")
            if v is not None:
                return bool(v)
        except Exception:
            pass
        return True

    def _cfg_all(self, dotted: str, default, cfg: dict | None = None):
        """#108: 全键空间读取 — Config 属主优先, cfg 管道块双形态兜底。

        病理 (D-293 取证): handler 收到的 cfg 是管道块作用域 —
        get_section("ops.pipelines") 把 yaml 扁平成点键后又重建为嵌套
        dict, 所以 flat 点键 (如 backtest.wfo_auto_deploy.enabled) 从
        管道块恒 MISS → 门恒回落默认值 (开关是死的)。local.yaml 的键
        在 registry 顶层可达, 生产构造点 (ops_layer/__init__.py) 也传了
        Config — 本方法让读点先走 Config 全键空间, 再兜底 cfg 块
        (flat→嵌套双形态, 保既有测试直传契约), 最后 default。

        优先级语义对齐 _dep_data_probe_enabled 先例: Config 有键即赢,
        哪怕 cfg 块里也有同键 (Config 是部署面唯一属主)。
        """
        try:
            if self.config is not None:
                v = self.config.get(dotted)
                if v is not None:
                    return v
        except Exception:
            pass
        if cfg:
            val = cfg.get(dotted)
            if val is not None:
                return val
            node = cfg
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node
        return default

    def _daily_data_pipeline_data_probe(self, conn, today: str) -> tuple[bool, str]:
        """OPS-02: daily_data_pipeline failed/timeout 时的"数据在就放行"探针。

        08-18~20 事故: 管道连续 3 天 timeout 判 failed, 但 daily_klines 5541 行
        实际已落库 — 超时误判把下游全链饿死。先核验 kline 行数 (主数据),
        再回退子表覆盖计数。返回 (放行?, 依据)。
        """
        if self._dep_data_probe_enabled():
            try:
                n_kline = conn.execute(
                    "SELECT COUNT(*) FROM daily_klines WHERE date=?", (today,)
                ).fetchone()[0]
            except sqlite3.Error:
                n_kline = 0
            if n_kline >= self._min_kline_rows():
                return True, f"dep_met_via_data_probe (daily_klines {n_kline} 行 >= {self._min_kline_rows()})"
        sub_ok_count = 0
        for t in ['stock_valuation', 'stock_moneyflow', 'concept_daily', 'analyst_forecasts', 'market_sentiment']:
            try:
                n = conn.execute(f"SELECT 1 FROM {t} WHERE date=? LIMIT 1", (today,)).fetchone()
            except sqlite3.Error:
                continue
            if n:
                sub_ok_count += 1
        if sub_ok_count >= 3:
            return True, f"dep_met ({sub_ok_count}/5 sub-tables have today's data)"
        return False, ""

    def can_run(self, pipeline_name: str, depends_on: list[str] | None = None) -> tuple[bool, str]:
        cfg = self._pipeline_configs.get(pipeline_name, {})
        depends_on = depends_on if depends_on is not None else cfg.get("depends_on", [])
        if not depends_on:
            return True, "no dependencies"
        today = bj_now().split(" ")[0]
        conn = self.db.get_connection()
        probe_notes = []
        for dep in depends_on:
            row = conn.execute(
                """SELECT status, start_time FROM pipeline_runs
                   WHERE pipeline_name = ? AND date = ?
                   ORDER BY start_time DESC LIMIT 1""",
                (dep, today),
            ).fetchone()
            if not row:
                return False, f"dependency '{dep}' has not run today"
            status = row["status"]
            # G3 根治: 最新行为 running 且超龄 (> timeout_s x 2, 与三判据 C3 同源) 时,
            # 大概率是 ghost/僵尸行 (进程已死或线程卡死, 收割脚本未及跑)。
            # 不让活体故障堵死依赖门: 回退取该依赖最近的终态行按旧逻辑判定;
            # 无任何终态行 -> 保守拦截。age == x2 整不跳过 (> 才跳过)。
            if status == "running":
                age_s = _run_age_s(row["start_time"], bj_now())
                if age_s is not None:
                    limit_s = int(self._pipeline_configs.get(dep, {}).get("timeout_s", 300)) * 2
                    if age_s > limit_s:
                        term = conn.execute(
                            """SELECT status FROM pipeline_runs
                               WHERE pipeline_name = ? AND date = ? AND status != 'running'
                               ORDER BY start_time DESC LIMIT 1""",
                            (dep, today),
                        ).fetchone()
                        if term is None:
                            logger.warning(
                                f"can_run: '{dep}' latest run stuck-running {age_s:.0f}s "
                                f"(> {limit_s}s) and no terminal history, blocking conservatively")
                            return False, (
                                f"dependency '{dep}' stuck running {age_s:.0f}s "
                                f"(> {limit_s}s) 且无终态历史, 保守拦截")
                        note = (f"{dep}: 最新行 stuck-running({age_s:.0f}s>{limit_s}s), "
                                f"按最近终态 {term['status']} 判定")
                        logger.warning(f"can_run: {note}")
                        probe_notes.append(note)
                        status = term["status"]
            if status == "failed":
                # OPS-02: timeout 同样落 status='failed' (error_msg='timeout after Ns'),
                # 故此分支同时覆盖 failed 与 timeout — "数据在就放行下游",
                # 只放行数据依赖, 管道失败告警照发不掩盖。
                if dep == "daily_data_pipeline":
                    ok, basis = self._daily_data_pipeline_data_probe(conn, today)
                    if ok:
                        logger.info(f"dependency '{dep}' failed but data probe passed: {basis}")
                        probe_notes.append(f"{dep}: {basis}")
                        continue
                return False, f"dependency '{dep}' failed today"
            if status == "running":
                return False, f"dependency '{dep}' is still running"
            # DL-43: partial = 数据已落库但降级, 下游可放行 (与 failed 不同,
            # 无需数据探针 — partial 的产生条件本身就含落库行数核验)。
            if status not in ("success", "partial"):
                return False, f"dependency '{dep}' status is '{status}'"
        if probe_notes:
            return True, "all dependencies met (" + "; ".join(probe_notes) + ")"
        return True, "all dependencies met"

    def _alert_starvation(self, pipeline_name: str, deps_reason: str) -> None:
        """G4: 依赖饿死告警 (规格: ops_layer/g4_starvation_alert.py)。

        仅对 running/not_run/skipped/wait_timeout 四类触发; failed 不触发 (防双发)。
        severity 升级规则: 同日 ≥3 条饿死或饿死链含 factor_update → critical。
        """
        from ops_layer.g4_starvation_alert import build_starvation_alert, parse_starvation

        if parse_starvation(deps_reason) is None:
            return
        starved_today: list[str] = []
        dep_run_info = ""
        try:
            today = bj_now().split(" ")[0]
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT DISTINCT pipeline_name FROM pipeline_runs "
                "WHERE date = ? AND status = 'skipped' AND error_msg LIKE '%depend%'",
                (today,),
            ).fetchall()
            starved_today = [r["pipeline_name"] for r in rows]
            m = re.search(r"dependency '([^']+)'", deps_reason or "")
            if m:
                dep_row = conn.execute(
                    "SELECT run_id, status, start_time FROM pipeline_runs "
                    "WHERE pipeline_name = ? AND date = ? "
                    "ORDER BY start_time DESC LIMIT 1",
                    (m.group(1), today),
                ).fetchone()
                if dep_row:
                    dep_run_info = (
                        f"{dep_row['run_id']} status={dep_row['status']} "
                        f"start={dep_row['start_time']}"
                    )
        except Exception as e:
            logger.debug(f"starvation context query failed (non-blocking): {e}")

        fields = build_starvation_alert(pipeline_name, deps_reason, starved_today, dep_run_info)
        from ops_layer.core.models import Alert
        self.alert_manager.send(Alert(**fields))
        logger.warning(
            f"[G4] starvation alert sent: {fields['title']} severity={fields['severity']}")

    def wait_for_dependencies(self, pipeline_name: str, max_wait_s: int = 900) -> tuple[bool, str]:
        """等待依赖完成，最多等 max_wait_s 秒"""
        cfg = self._pipeline_configs.get(pipeline_name, {})
        depends_on = cfg.get("depends_on", [])
        if not depends_on:
            return True, "no dependencies"

        waited = 0
        check_interval = 60  # 每分钟检查一次
        while waited < max_wait_s:
            ok, reason = self.can_run(pipeline_name)
            if ok:
                return True, reason
            # 如果是"still running"，继续等待
            if "still running" in reason:
                logger.info(f"Waiting for dependencies of {pipeline_name}: {reason} ({waited}s)")
                time.sleep(check_interval)
                waited += check_interval
            else:
                return False, reason

        # Q3: 超时文案带出阻塞依赖名, 使 _DEP_RE 可提取 (告警标题不再"未知上游")
        _m = re.search(r"dependency '([^']+)'", reason)
        _dep_hint = f" (dependency '{_m.group(1)}')" if _m else ""
        return False, f"dependencies not completed within {max_wait_s}s timeout{_dep_hint}"

    def _get_run_metadata(self) -> tuple[str, str]:
        """获取版本号 (git short HEAD) 与配置哈希 — DL-13 溯源补全。

        旧实现读 pyproject 版本号且 config_hash 恒空 (0/1940 填充);
        改为 git short HEAD + config/ yaml 指纹, 与 version_tracker 同源。
        """
        from ops_layer.version_tracker import compute_config_hash, git_head

        version, _branch = git_head()
        try:
            config_hash = compute_config_hash()
        except Exception:
            config_hash = ""
        return version, config_hash

    def _end_run_safe(self, run_id, status, error="", version="", config_hash="", pipeline_name=""):
        """#94-B (铁律 #12): 终态落库撞锁不得让行永久挂 running。

        2026-09-01 实弹: ghost 线程持写锁 (7.06M 行 factor_daily UPDATE),
        end_run 撞 'database is locked' (busy_timeout=5s 单发) → 异常上抛 →
        上层 @catch_quantstar_error 吞成 None → 行永久 running, monthly_report
        依赖饿死 900s。包 OPS-04 retry_on_locked (3×2s) 给锁持有者释放时间;
        P0-5 语义: 耗尽后降级 fallback jsonl (不静默), 行交 OPS-01 僵尸收割
        兜底; 非 locked 错误原样上抛 (不吞真错误)。覆盖全部终态分支
        (skipped/success/partial/failed/timeout/except)。
        """
        from ops_layer.run_guard import retry_on_locked

        retry_on_locked(
            lambda: self.pipeline_logger.end_run(run_id, status, error, version, config_hash),
            label=f"end_run:{pipeline_name}",
            fallback_record={
                "kind": "pipeline_run_end", "name": pipeline_name, "run_id": run_id,
                "status": status, "error": str(error)[:500],
            },
        )

    def run_pipeline(self, pipeline_name: str) -> PipelineRunResult:
        """执行单个管道的完整生命周期（含熔断保护）"""
        cfg = self._pipeline_configs.get(pipeline_name)
        if not cfg:
            return PipelineRunResult(name=pipeline_name, status="failed", error="pipeline not defined")

        version, config_hash = self._get_run_metadata()

        # 0. 熔断检查
        now = time.time()
        if pipeline_name in self._circuit_open_until:
            if now < self._circuit_open_until[pipeline_name]:
                remaining = int(self._circuit_open_until[pipeline_name] - now)
                logger.warning(f"Pipeline {pipeline_name} circuit breaker open, skip ({remaining}s remaining)")
                return PipelineRunResult(name=pipeline_name, status="skipped", error=f"circuit breaker open ({remaining}s)")
            else:
                del self._circuit_open_until[pipeline_name]
                self._consecutive_failures[pipeline_name] = 0
                logger.info(f"Pipeline {pipeline_name} circuit breaker half-open, allowing retry")

        max_retries = cfg.get("max_retries", 1)
        retry_delay = cfg.get("retry_delay_s", 30)
        timeout_s = cfg.get("timeout_s", 300)

        # 1. 等待依赖
        deps_ok, deps_reason = self.wait_for_dependencies(pipeline_name)
        if not deps_ok:
            logger.warning(f"Pipeline {pipeline_name} skipped: {deps_reason}")
            run_id = self.pipeline_logger.start_run(pipeline_name)
            self._end_run_safe(run_id, "skipped", deps_reason, version, config_hash, pipeline_name)
            # G4 (2026-08-24夜): 依赖饿死告警 — can_run 因依赖未完成而 skip 且上游非
            # failed/success 终态。08-24 实测 9 条管道级联全灭且零告警 (铁律 #12)。
            # failed 分支不在此发 (OPS-07「管道失败」已覆盖, 防双发); 规格见
            # ops_layer/g4_starvation_alert.py 与 vault 规格稿。
            try:
                self._alert_starvation(pipeline_name, deps_reason)
            except Exception as _e:  # 告警失败不阻断 skip 返回路径
                logger.warning(f"starvation alert skipped (fail-safe): {_e}")
            return PipelineRunResult(name=pipeline_name, status="skipped", error=deps_reason)

        # 2. 执行（含重试）
        last_error = ""
        for attempt in range(max_retries + 1):
            run_id = self.pipeline_logger.start_run(pipeline_name)
            start = time.time()
            try:
                future = self._executor.submit(self._execute_pipeline, pipeline_name, cfg)
                handler_result = future.result(timeout=timeout_s)
                elapsed = time.time() - start
                # DL-43 三态: success / partial / failed。此前 partial 被
                # success=True 折叠成 "success" 静默入库 (DL-06 假成功病灶:
                # lhb_daily 报 success written=61 但表无数据)。partial 现在
                # 独立落 pipeline_runs.status='partial' 并发 warning 告警 (铁律 #12)。
                final_status = handler_result.final_status
                if final_status in ("success", "partial"):
                    partial_detail = ""
                    if final_status == "partial":
                        _meta = getattr(handler_result, "metadata", None) or {}
                        _perrs = _meta.get("errors") or handler_result.errors or []
                        partial_detail = "; ".join(str(e) for e in _perrs[:5]) or "partial result"
                    self._end_run_safe(
                        run_id, final_status, partial_detail, version, config_hash, pipeline_name)
                    if final_status == "partial":
                        logger.warning(
                            f"Pipeline {pipeline_name} PARTIAL in {elapsed:.1f}s: {partial_detail}")
                        # partial 不 resolve 既有告警 (问题仍在), 且必须新发告警
                        from ops_layer.core.models import Alert
                        try:
                            self.alert_manager.send(Alert(
                                severity="warning", layer="ops",
                                title=f"管道部分成功: {pipeline_name}",
                                detail=(f"数据已落库但降级 (partial): {partial_detail[:500]}"),
                                action="检查该管道当日数据完整性; 若为源端限流可等待下轮自愈",
                            ))
                        except Exception as e:
                            logger.warning(f"partial alert send failed (fail-safe): {e}")
                    else:
                        logger.info(f"Pipeline {pipeline_name} completed in {elapsed:.1f}s")
                        # OPS-07 恢复通知: 管道转 success → 自动 resolve 同管道 open 告警
                        # (fail-safe: 告警闭环失败不影响管道结果)
                        try:
                            self.alert_manager.resolve_pipeline_alerts(pipeline_name)
                        except Exception as e:
                            logger.warning(f"resolve_pipeline_alerts skipped (fail-safe): {e}")
                    # partial 同样重置连续失败计数: 数据已落库, 熔断器只针对硬失败
                    self._consecutive_failures[pipeline_name] = 0
                    return PipelineRunResult(
                        name=pipeline_name, status=final_status,
                        error=partial_detail,
                        elapsed_s=round(elapsed, 1),
                        metadata=getattr(handler_result, "metadata", None) or {},
                    )
                else:
                    last_error = "; ".join(handler_result.errors) if handler_result.errors else "pipeline reported failure"
                    self._end_run_safe(run_id, "failed", last_error, version, config_hash, pipeline_name)
                    logger.warning(f"Pipeline {pipeline_name} reported failure ({attempt + 1}/{max_retries + 1}): {last_error}")

            except FuturesTimeout:
                elapsed = time.time() - start
                last_error = f"timeout after {timeout_s}s"
                self._end_run_safe(run_id, "failed", last_error, version, config_hash, pipeline_name)
                # OPS-01: 超时只放弃等待, 线程杀不掉是 Python 限制 — 诚实记录 ghost,
                # 尝试 cancel (未开始执行时可阻止启动), 不谎称"已终止"。
                cancelled = future.cancel()
                logger.warning(
                    f"Pipeline {pipeline_name} timeout ({attempt + 1}/{max_retries + 1}); "
                    f"ghost thread may still hold resources (cancel()={'ok' if cancelled else 'no-op, already running'})"
                )
                # P0-7 方向C: cancel() 无效时登记 ghost + critical 告警 (铁律 #12 不可静默)。
                # 08-21 事故中 ghost 持锁 6h47m 无人知晓; 登记后 shutdown 可有界等待,
                # 运维可从 logs/ghost_registry.jsonl 与告警面板查到。
                if not cancelled:
                    self.ghosts.register(pipeline_name, future, timeout_s, run_id=run_id)
                    from ops_layer.core.models import Alert
                    self.alert_manager.send(Alert(
                        severity="critical", layer="ops",
                        title=f"Ghost 线程: {pipeline_name}",
                        detail=(f"管道超时 {timeout_s}s 后线程仍在运行 (cancel() 无效), "
                                f"可能持续持有 DB 写锁; 已登记 ghost_registry"),
                        action="检查 logs/ghost_registry.jsonl; 若 DB 被锁考虑重启进程",
                    ))

            except Exception as e:
                elapsed = time.time() - start
                last_error = str(e)
                self._end_run_safe(run_id, "failed", last_error, version, config_hash, pipeline_name)
                # exc_info=True: 完整 traceback 进日志 (formatter 的 error.traceback
                # 字段), 不再只留 message — 2026-08-26 closed-database 事故无栈可查。
                logger.warning(f"Pipeline {pipeline_name} failed ({attempt + 1}/{max_retries + 1}): {e}", exc_info=True)

            if attempt < max_retries:
                wait = min(retry_delay * (2 ** attempt), 300) + random.uniform(0, 5)
                logger.info(f"Retrying {pipeline_name} in {wait:.0f}s...")
                time.sleep(wait)

        # 3. 重试耗尽 → 熔断 + 告警
        consecutive = self._consecutive_failures.get(pipeline_name, 0) + 1
        self._consecutive_failures[pipeline_name] = consecutive
        if consecutive >= 3:
            cooldown = min(300 * (2 ** (consecutive - 3)), 3600)
            self._circuit_open_until[pipeline_name] = time.time() + cooldown
            logger.warning(f"Pipeline {pipeline_name} circuit breaker opened for {cooldown}s ({consecutive} consecutive failures)")
            alert_detail = f"连续 {consecutive} 次失败，熔断 {cooldown}s: {last_error}"
        else:
            alert_detail = f"重试 {max_retries} 次后仍失败: {last_error}"

        from ops_layer.core.models import Alert
        self.alert_manager.send(Alert(
            severity="critical", layer="ops",
            title=f"管道失败: {pipeline_name}",
            detail=alert_detail,
            action="检查管道日志并手动干预",
        ))
        return PipelineRunResult(name=pipeline_name, status="failed", error=last_error)

    def _execute_pipeline(self, name: str, cfg: dict) -> HandlerResult:
        """执行管道的具体业务逻辑，返回 HandlerResult"""
        dispatch = {
            "daily_data_pipeline": self._run_data_pipeline,
            "evening_ingest": self._run_evening_ingest,
            "factor_update": self._run_factor_update,
            "fwd_return_backfill": self._run_fwd_return_backfill,
            "quality_check": self._run_quality_check,
            "rec_tracking_daily": self._run_rec_tracking_daily,
            "freshness_audit": self._run_freshness_audit,
            # P0-5: 源健康第三方对拍 (新浪参照), 抽样核对主源, 差异落表+告警
            "source_crosscheck": self._run_source_crosscheck,
            # DL-51: kline/adjust_factor 顶层管道 (备源补跑 + 复权因子每日增量)
            "kline": self._run_kline,
            "valuation_backfill": self._run_valuation_backfill,
            "adjust_factor": self._run_adjust_factor,
            # R4 concept 断电案 (统筹 sm-39c8b41402a748ee): zzshare 真概念
            # 每日通电 — 独立 16:35 顶层小管道, 不进 DAILY_SUB_PIPELINES
            # (GO-7 硬闸语义保持: registry 单独调起 + 独立排程, 非日管道成员)。
            "concept_plates_daily": self._run_concept_plates_daily,
            # DDP c-1 (#81): analyst_forecasts 独立 16:55 顶层管道 —
            # 拆出 DDP 串行链 (~30min 固定成本), 错峰 16:52 agent_evaluate。
            "analyst_forecasts": self._run_analyst_forecasts,
            # 件2 (09-02 统筹 sm-8683e2dff09a4717): concept_membership 区间
            # 存储通电 — 独立 17:05 顶层小管道 (08-31 调研卡「代码全有库里
            # 全无」收口: DDL 幂等自建表, 首跑即建 concept_membership +
            # plate_daily_rate, 读侧 _loader 探测到表即自动升级区间 PIT 口径)。
            "concept_intervals_daily": self._run_concept_intervals_daily,
            # 刀2 (fix/anomaly-1630-recollect, 派单 sm-7c5089a73bd44eaf):
            # shortterm 快照六项 16:30 补采轮 — anomaly 等端点无日期参数,
            # 上游发布晚于 15:44 首轮窗即永久丢 (DL-07)。独立顶层小管道,
            # 不进 DAILY_SUB_PIPELINES (GO-7 硬闸语义), 排程见 ops.yaml。
            "shortterm_recollect": self._run_shortterm_recollect,
            "daily_backtest": self._run_daily_backtest,
            "evening_report": self._run_evening_report,
            "premarket_brief": self._run_premarket_brief,
            "morning_brief": self._run_morning_brief,
            "morning_review": self._run_morning_review,
            "midday_brief": self._run_midday_brief,
            "tail_advisory": self._run_tail_advisory,
            "pre_market_refresh": self._run_pre_market_refresh,
            "auction_parse": self._run_auction_parse,
            "health_check": self._run_health_check,
            "ic_feedback": self._run_ic_feedback,
            "wfo_gate": self._run_wfo_gate,
            "param_tuning": self._run_param_tuning,
            "end_of_day": self._run_end_of_day,
            "post_market": self._run_post_market,
            "weekly_summary": self._run_weekly_summary,
            "daily_ops_report": self._run_daily_ops_report,
            "dev_verify_report": self._run_dev_verify_report,
            "intraday_monitor": self._run_intraday_monitor,
            "provider_check": self._run_provider_check,
            # #112: recommendation_retrospect 结算通电 — fwd_return_backfill 下游,
            # INSERT 新档案 + UPDATE only-NULL 幂等结算 (AG-01 canonical 口径).
            "retrospect_settlement": self._run_retrospect_settlement,
            "agent_evaluate": self._run_agent_evaluate,
            "agent_discovery": self._run_agent_discovery,
            "agent_cleanup": self._run_agent_cleanup,
            "chart_analysis": self._run_chart_analysis,
            # KLINE-FX 结构修 (统筹 2026-09-07): 17:40 kline 兜底后因子终算 —
            # baostock 日K 17:30 才入库, 16:00 factor_update 只见 15:30 主源部分面
            # (09-07 实证: 523/5548 → #122 宁缺毋假只算 523 只)。18:05 幂等守卫
            # 短路: 因子行 ≥ 当日K线行即 skip (正常日零开销)。
            "fx_settlement": self._run_fx_settlement,
            # AG-NEWS 注册面补齐 (2026-09-13, sm-d7b43861): news_per_stock 独立
            # 17:00 顶层管道 — 候选池 (stock_pool 昨荐+活仓) 经 PipelineInput.symbols
            # 透传 per_stock_em 腿; codes=None 不拉防打爆 (④a 设计).
            "news_per_stock": self._run_news_per_stock,
            # F 席 ST-REGIME 注册面补齐: emotion_cycle 留痕产线 16:35
            # (factor_update 后) — 判定核吃 factor 面, unknown 也落库.
            "emotion_cycle": self._run_emotion_cycle,
            "paper_trading": self._run_paper_trading,
            # W2 补偿腿 (2026-09-19): 16:35 因依赖断链 skip 时的当日补跑。
            # depends_on:[] — DDP/factor 挂掉时本腿仍须能跑 (否则等于没修)。
            "evening_paper_trading": self._run_evening_paper_trading,
            "stats_finalize": self._run_stats_finalize,
            "adata_sentiment": self._run_adata_sentiment,
            "model_retrain": self._run_model_retrain,
            "monthly_report": self._run_monthly_report,
            "db_backup": self._run_db_backup,
            # 增量备份 (v1.0 判据 6, 统筹 09-14 亲接管施工):
            # 每日按表 dump 当日行 — db_backup (周一全备) 的日差件.
            "incremental_backup": self._run_incremental_backup,
            "log_rotation": self._run_log_rotation,
            "disk_check": self._run_disk_check,
        }
        handler = dispatch.get(name)
        if handler is None:
            raise ValueError(f"pipeline '{name}' has no execution handler")
        result = handler(cfg)
        if result is None:
            # 无结构化结果的 handler(运维/报告/回测等副作用管道)视为成功空操作;
            # 数据管道(_run_data_pipeline/_run_factor_update/_run_model_retrain)
            # 已显式返回 HandlerResult，无真实数据产出不会再被误报为成功。
            # (#94 补齐最后缺口: _run_model_retrain except/成功路径均显式返回。)
            logger.warning(f"pipeline '{name}' handler returned no structured result (treated as completed no-op)")
            return HandlerResult(success=True, errors=[], metadata={"noop": True})
        if not isinstance(result, HandlerResult):
            raise TypeError(
                f"pipeline '{name}' returned {type(result).__name__}, expected HandlerResult"
            )
        return result

    def _run_evening_ingest(self, cfg: dict):
        """晚间补采指数/龙虎榜；DDP 失败不能挡住独立补采，腿级失败不吞。"""
        from data_layer.pipelines.base import PipelineInput

        date = cfg.get("date") or bj_date()
        data = getattr(self._get_app(), "data", None)
        pipelines = getattr(data, "pipelines", None) or {}
        errors = []
        statuses = {}
        records = 0
        for name in ("index_daily", "lhb_daily"):
            if pipelines.get(name) is None:
                statuses[name] = "failed"
                errors.append(f"{name}: pipeline not registered")
                continue
            try:
                result = data.run_pipeline(name, PipelineInput(date=date, db=data.db))
                status = getattr(result, "status", "failed")
                if result is None or getattr(result, "date", None) != date:
                    raise ValueError(f"{name}: missing result or business date mismatch ({date})")
                if status not in ("success", "partial", "failed"):
                    raise ValueError(f"{name}: invalid status {status}")
                statuses[name] = status
                records += getattr(result, "records_written", 0) or 0
                if status != "success":
                    detail = "; ".join(getattr(result, "errors", []) or []) or status
                    errors.append(f"{name}: {detail}")
            except Exception as exc:
                statuses[name] = "failed"
                errors.append(f"{name}: {exc}")
        failed = "failed" in statuses.values()
        return HandlerResult(
            success=not failed, errors=errors,
            metadata={"date": date, "statuses": statuses, "records_written": records,
                      "partial": not failed and "partial" in statuses.values()},
        )

    def _run_data_pipeline(self, cfg: dict):
        from common.utils import bj_date

        start_date = cfg.get("start_date", bj_date())
        end_date = cfg.get("end_date", bj_date())

        app = self._get_app()
        if not getattr(app, "data", None):
            raise RuntimeError("data layer is not available")

        from data_layer.pipelines.base import PipelineInput

        overall_errors = []
        total_records = 0
        # fail-fast v1: best-effort 子管道降级 (超时/失败/0行) 清单。
        # 非空时父管道降级 partial (不再静默 success), 但下游不阻塞
        # (can_run 对 partial 放行, orchestrator.py DL-43 三态语义)。
        best_effort_degraded = []
        # 逐日执行；收集所有子管道结果后统一失败，避免顶层假报 success。
        # DL-07 (2026-08-18): 追加 shortterm_collect 冷采集 (fuyao 短线结构/情绪等端点)。
        # 此类数据不可回填, 错过当日永久丢失。管线实现在数据层 fix/data-batch (8b02cbc);
        # 若当前分支尚未注册则跳过并告警 (registry.run 对未注册名抛 ValueError,
        # 直接加入列表会在合并前拖垮整条日管道)。
        # DL-51: 基础清单来自模块级常量 DAILY_SUB_PIPELINES (与 schedule-check
        # 第三方对账共用同一事实源); 条件项 (shortterm_collect/index_daily) 按注册情况追加。
        sub_pipelines = list(DAILY_SUB_PIPELINES)
        # getattr 兜底: 测试 fake (SimpleNamespace) 与旧版 data layer 可能无 pipelines 属性。
        _pipelines = getattr(app.data, "pipelines", None) or {}
        if _pipelines.get("shortterm_collect") is not None:
            sub_pipelines.append("shortterm_collect")
        else:
            logger.warning("shortterm_collect 管线未注册 (数据层 DL-07 未合并), 冷采集跳过")
        # DL-33 (D-33): index_daily 采集类 (与 shortterm_collect 同性质), 并入日管道。
        # 未注册则跳过+告警, 失败 best-effort 降级不阻塞主流程。
        if _pipelines.get("index_daily") is not None:
            sub_pipelines.append("index_daily")
        else:
            logger.warning("index_daily 管线未注册 (数据层 DL-23 未合并), 指数采集跳过")
        current = start_date
        while current <= end_date:
            for name in sub_pipelines:
                pip_input = PipelineInput(
                    date=current,
                    start_date=start_date,
                    end_date=current,
                    db=getattr(app.data, "db", None),
                    config={},
                )
                # moneyflow / 低频数据 (龙虎榜/分析师/概念/情绪/新闻) best-effort:
                # 上游源限流或当日未更新时降级为 warning, 不阻塞核心路径 kline/valuation.
                # 注意: 必须在 result is None 检查之前判定, 否则 best-effort 管道超时
                # (with_timeout 返回 None) 会误把整个 daily pipeline 拖成失败
                # (2026-08-17 事故: analyst_forecasts 60s 超时返回 None, 幽灵线程 11min
                # 后实际写入 2864 行, 但已被误报为失败)。
                # shortterm_collect 为冷采集: 源端点限流/当日未更新时降级为 warning,
                # 不阻塞核心路径 (DL-07)。
                # W2-1 (2026-09-19): fuyao_financial 移入 best_effort — 财务=季报
                # (stock_financials max=2026-06-30), 其每日 partial (indicators 缺失/
                # 部分代码错) 是季报稀疏性非当日故障, 不得拖垮 DDP (照 lhb_daily 先例)。
                best_effort = name in ("stock_list", "moneyflow", "lhb_daily", "analyst_forecasts",
                                        "concept_daily", "market_sentiment", "stock_news",
                                        "stock_limit_up_down", "shortterm_collect", "index_daily",
                                        "fuyao_financial")

                # ①计时埋点 (瘦身前置, 先测量再瘦身): per-sub-pipeline start/end 事件
                # 落 logs/child_pipeline_timing.jsonl, 含 duration_s/row_count。
                # 补铁律#12 盲区 — 此前 pipeline_runs 只记顶层 ddp duration,
                # 11+2 子管道无独立计时, 36.6min 无法归因 (sched-fix-design §3.4)。
                _emit_child_pipeline_event({
                    "event": "child_pipeline_start", "ts": bj_now(),
                    "pipeline": name, "date": current, "best_effort": best_effort,
                })
                _sub_t0 = time.time()
                result = app.data.run_pipeline(name, pip_input)
                _emit_child_pipeline_event({
                    "event": "child_pipeline_end", "ts": bj_now(),
                    "pipeline": name, "date": current,
                    "duration_s": round(time.time() - _sub_t0, 2),
                    "status": getattr(result, "status", None),
                    "records_written": getattr(result, "records_written", 0),
                    "best_effort": best_effort,
                })
                if result is None:
                    msg = f"data pipeline '{name}' returned no result on {current}"
                    if best_effort:
                        logger.warning(f"best-effort {msg} (超时或异常, 不阻塞核心路径)")
                        # ②fail-fast v1: best-effort 超时/无结果 → 记降级,
                        # 父管道不再静默 success (Q-51: _capability_timeout 无 key
                        # →60s 默认→with_timeout 截断仅 warning 的静默失效)。
                        best_effort_degraded.append(f"{name}: no result (超时或异常)")
                    else:
                        logger.error(msg)
                        overall_errors.append(msg)
                    continue

                status = getattr(result, "status", None)
                records = getattr(result, "records_written", 0)
                errors = getattr(result, "errors", []) or []
                total_records += records

                if status not in ("success", "partial"):
                    detail = "; ".join(str(error) for error in errors[:3]) or "no error detail"
                    msg = f"data pipeline '{name}' {status or 'invalid'} on {current}: {detail}"
                    if best_effort:
                        logger.warning(f"best-effort {msg}")
                        # ②fail-fast v1: best-effort 失败 → 记降级 (父管道 partial)。
                        best_effort_degraded.append(f"{name}: {status or 'invalid'} ({detail})")
                    else:
                        logger.error(msg)
                        overall_errors.append(msg)
                elif status == "partial":
                    detail = "; ".join(str(error) for error in errors[:3]) or "partial result"
                    msg = f"data pipeline '{name}' partial on {current}: {detail}"
                    # B4 T+1 豁免: 腿 partial 但上一交易日履约 → 不记降级, info 留痕。
                    # (只动 DDP best_effort 链; :785 直调 handler 不在此链, 不动。)
                    if name in DDP_T1_EXEMPT_LEGS and _t1_leg_on_track(
                            getattr(app.data, 'db', None), name, current):
                        logger.info(f"T+1 leg {name} on {current} prev-day on track; exempt")
                    else:
                        if best_effort:
                            logger.warning(f"best-effort {msg}")
                            # ②fail-fast v1: best-effort partial → 记降级 (父管道 partial)。
                            best_effort_degraded.append(f"{name}: partial ({detail})")
                        else:
                            logger.error(msg)
                            overall_errors.append(msg)
                elif records == 0:
                    # fuyao_financial 为季度数据，以报告期末日期落库，非当日；
                    # 若管道明确跳过（最新报告期已全覆盖），属正常行为。
                    if name == "fuyao_financial" and getattr(result, "metadata", {}).get("skipped"):
                        logger.info(
                            f"Pipeline {name} on {current} skipped: "
                            f"latest report period already covered"
                        )
                        continue
                    conn = app.data.db.get_connection()
                    table_by_pipeline = {
                        "kline": "daily_klines",
                        "fuyao_financial": "stock_financials",
                        "valuation": "stock_valuation",
                        "moneyflow": "stock_moneyflow",
                        "concept_daily": "concept_daily",
                        "lhb_daily": "lhb_daily",
                        "market_sentiment": "market_sentiment",
                        "analyst_forecasts": "analyst_forecasts",
                        "stock_news": "stock_news",
                        "stock_limit_up_down": "stock_limit_up_down",
                    }
                    table = table_by_pipeline.get(name)
                    existing = 0
                    if table:
                        existing = conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE date = ?", (current,)
                        ).fetchone()[0]
                    if existing:
                        logger.info(
                            f"Pipeline {name} on {current} wrote 0 records; "
                            f"{existing} valid rows already exist"
                        )
                    elif current == bj_date():
                        # 当日 0 行且无存量: 可能源端尚未发布 (benign), 仅 warning 留痕,
                        # 不计降级 — 负向对照要求不误伤"源还没发"的正常早跑场景。
                        logger.warning(
                            f"Pipeline {name} on {current} wrote 0 records "
                            "(may be before source publication)"
                        )
                    else:
                        msg = f"data pipeline '{name}' on {current} wrote 0 records"
                        if best_effort:
                            logger.warning(f"best-effort {msg}")
                            # ②fail-fast v1: best-effort 0 行 → 记降级 (父管道 partial)。
                            best_effort_degraded.append(f"{name}: 0 records")
                        else:
                            logger.error(msg)
                            overall_errors.append(msg)
            # F2.5 分表覆盖率判定: kline 即使子管道报 success 也必须独立核验
            # 当日落库行数 ≥ min_kline_rows, 不足则该日判 failed (防假成功)
            min_kline_rows = self._min_kline_rows()
            kline_db = getattr(app.data, "db", None)
            if kline_db is not None:
                try:
                    probe_conn = kline_db.get_connection()
                    day_rows = probe_conn.execute(
                        "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (current,)
                    ).fetchone()[0]
                    if day_rows < min_kline_rows:
                        msg = (f"data pipeline 'kline' coverage insufficient on {current}: "
                               f"{day_rows} rows < min_kline_rows {min_kline_rows}")
                        logger.error(msg)
                        overall_errors.append(msg)
                except sqlite3.Error as e:
                    logger.debug(f"daily_data_pipeline: kline coverage probe unavailable: {e}")
            dt = datetime.strptime(current, "%Y-%m-%d") + timedelta(days=1)
            current = dt.strftime("%Y-%m-%d")

        if overall_errors:
            # 仅当 kline 声称失败但当日行情实际已落库时，父管道降级为 partial success
            # （此时 kline 部分失败是超时/末尾个别股票未取到，非源无效）。
            # 其他关键子管道（valuation 等）失败仍保持 failed（truthfulness）。
            kline_fail = any("'kline'" in msg for msg in overall_errors)
            kline_rows = 0
            db_obj = getattr(app.data, "db", None) or getattr(self, "db", None)
            if kline_fail and db_obj is not None:
                try:
                    # 用独立只读连接核验，避免关闭共享连接污染后续调用；
                    # :memory: 为测试用新进程内库，直接走共享连接。
                    if db_obj.db_path == ":memory:":
                        probe = db_obj.get_connection()
                        kline_rows = probe.execute(
                            "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (start_date,)
                        ).fetchone()[0]
                    else:
                        import sqlite3 as _sq
                        probe = _sq.connect(db_obj.db_path, check_same_thread=False)
                        probe.row_factory = _sq.Row
                        try:
                            kline_rows = probe.execute(
                                "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (start_date,)
                            ).fetchone()[0]
                        except _sq.OperationalError:
                            kline_rows = 0
                        finally:
                            probe.close()
                except Exception:
                    logger.warning("daily_data_pipeline: could not verify kline rows", exc_info=True)
            if kline_fail and 0 < kline_rows < MIN_KLINE_ROWS:
                # 行情采集严重残缺 (远低于全市场 ~5500 只): 不降级为 partial,
                # 维持 failed 以便 e2e/次日重跑能捕获并修复 (例如只抓到 100/5540)。
                logger.error(
                    f"daily_data_pipeline kline coverage insufficient: "
                    f"kline_rows={kline_rows} < MIN_KLINE_ROWS={MIN_KLINE_ROWS} "
                    f"(expected ~5500); keeping failed status"
                )
            if kline_fail and kline_rows >= MIN_KLINE_ROWS:
                # 卡组二刀3 (2026-10-19): best_effort_degraded 并入 —
                # 此前提前 return 把 degraded 清单整体丢弃, 10-09 实证
                # DDP error_msg 无 shortterm 段 (全貌只在日志行)。
                merged_errors = list(overall_errors[:8]) + list(best_effort_degraded)
                logger.warning(
                    f"daily_data_pipeline partial success (kline_rows={kline_rows}, "
                    f"other errors): {' | '.join(merged_errors[:3])}"
                )
                return HandlerResult(
                    success=True, records_written=total_records,
                    metadata={"partial": True, "errors": merged_errors[:12]},
                )
            detail = " | ".join(overall_errors[:8])
            if len(overall_errors) > 8:
                detail += f" | ... and {len(overall_errors) - 8} more"
            # 卡组二刀3 (2026-10-19): best-effort 降级清单并入 failed 详情 —
            # 硬失败日也要能看见冷采集腿 degraded 了什么 (10-09 同族盲区)。
            if best_effort_degraded:
                _dshow = best_effort_degraded[:12]
                _dseg = "; ".join(_dshow)
                if len(best_effort_degraded) > 12:
                    _dseg += f"; ... and {len(best_effort_degraded) - 12} more"
                detail += " | best-effort degraded: " + _dseg
            return HandlerResult(
                success=False,
                records_written=total_records,
                errors=[
                    f"daily data pipeline failed ({len(overall_errors)} sub-pipeline errors, "
                    f"{total_records} records written): {detail}"
                ],
            )
        # ②fail-fast v1: 无硬失败但有 best-effort 子管道降级 (超时/失败/0行) →
        # 父管道降级 partial, 不再静默 success。复用 DL-43 三态既有链路:
        # pipeline_runs.status='partial' + warning 告警自动发 (run_pipeline 包装层),
        # 且 can_run 对 partial 放行 → 下游不阻塞 (与 failed 不同)。
        # 治 Q-51 静默失效: 父管道 success 但子管道零增量, 此前无人知晓。
        if best_effort_degraded:
            # 卡组二刀3 (2026-10-19): 截断 [:8]→[:12] — best-effort 腿共 11 条,
            # [:8] 会切掉第 9 位起的 shortterm_collect (正是 10-09 被丢的那条);
            # 超长显数 (... and N more), 与收官 detail 语义对齐, 不静默截断。
            _show = best_effort_degraded[:12]
            detail = "; ".join(_show)
            if len(best_effort_degraded) > 12:
                detail += f"; ... and {len(best_effort_degraded) - 12} more"
            logger.warning(
                f"daily_data_pipeline partial: {len(best_effort_degraded)} best-effort "
                f"sub-pipeline(s) degraded: {detail}"
            )
            return HandlerResult(
                success=True, records_written=total_records,
                metadata={"partial": True, "errors": _show},
            )
        return HandlerResult(success=True, records_written=total_records)

    def _run_factor_update(self, cfg: dict):
        from common.database import Database
        from common.utils import bj_date
        db = Database()
        conn = db.get_connection()
        latest_factor = conn.execute("SELECT MAX(date) FROM factor_daily").fetchone()[0]
        latest_kline = conn.execute("SELECT MAX(date) FROM daily_klines").fetchone()[0]
        conn.close()
        # 目标日期 = 数据已就绪的最大 kline 日期；若未超过已算因子日期则补因子日。
        # 这避免 8/12 行情已落库时误把 8/10 重跑一遍而漏算当日因子。
        kline_date = latest_kline if latest_kline else bj_date()
        factor_date = latest_factor if latest_factor else bj_date()
        date = max(factor_date, kline_date)
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        current_positions = None
        if hasattr(app, "execution") and app.execution:
            active = app.execution.stock_pool.get_by_status("active", "pending_entry")
            current_positions = {p["code"]: {"status": p.get("status", ""), "score": p.get("score", 0)} for p in active}
        result = app.strategy.run_daily(date, current_positions=current_positions)
        if not result:
            logger.warning(f"Strategy run_daily returned None for {date}")
            return HandlerResult(success=False, errors=[f"strategy run_daily returned None for {date}"])
        # 防空心成功: run_daily 返回非 None 不代表 factor_daily 已落库。
        # 2026-08-17 事故: snapshot 超时 → _get_stock_codes 返回 [] → 0 信号却记 success,
        # 掩盖了 factor_daily 当日 0 行的数据丢失。此处强制校验当日因子行数。
        fd_conn = db.get_connection()
        fd_count = fd_conn.execute("SELECT COUNT(*) FROM factor_daily WHERE date = ?", (date,)).fetchone()[0]
        # N3 (2026-09-14) 守卫口径换代: 行数之外补列值面。20→89 因子换代 (07-21)
        # 只写 factor_scores JSON 列后, 若未来形态再换代「行在值空」, 纯行数
        # 守卫照绿。列值面 = 当日 factor_scores 非空行占比, 低于阈值判空心。
        # 50% 阈值取保守下界: 89 因子体系正常日非空占比 >99%。
        fd_scores_count = fd_conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date = ? AND factor_scores IS NOT NULL AND factor_scores != ''",
            (date,)).fetchone()[0]
        fd_conn.close()
        if fd_count == 0:
            logger.error(f"factor_daily 当日 0 行, 判定失败 (防空心成功): date={date}")
            return HandlerResult(success=False, errors=[f"factor_daily empty for {date} (hollow success guard)"])
        if fd_scores_count * 2 < fd_count:
            logger.error(
                f"factor_daily 当日空心面: factor_scores 非空 {fd_scores_count}/{fd_count} 行 (<50%), 判定失败: date={date}")
            return HandlerResult(success=False, errors=[
                f"factor_daily hollow scores for {date}: {fd_scores_count}/{fd_count} rows non-empty (<50%)"])
        if not result.signals:
            logger.warning("No signals generated")
            return HandlerResult(success=True, errors=[], metadata={"signals": 0, "factor_daily": fd_count})
        # 写入 Execution Layer
        if hasattr(app, "execution") and app.execution:
            recommendations = []
            # R-2 方案②: 当日 rationale 全集一次查回 dict 化 (N+1 → 1 次查询),
            # chairman 口径与消费面同序。查不到的码留空串 (诚实水位, 不阻塞)。
            _rid_cache: dict[str, str | None] = {}
            _db_for_rid = app.data.db if hasattr(app, "data") and app.data else None
            for s in result.signals:
                if s.code not in _rid_cache:
                    _rid_cache[s.code] = _rationale_id_for_code(_db_for_rid, s.code, date)
                name = _resolve_stock_name(s.code)
                rec = {"code": s.code, "name": name, "action": s.action, "reason": s.reason,
                       "initial_score": s.strength, "confidence": s.confidence,
                       "entry_low": s.entry_low, "entry_high": s.entry_high,
                       "stop_loss": s.stop_loss, "take_profit": s.take_profit,
                       "rationale_id": _rid_cache[s.code] or ""}
                recommendations.append(rec)
            app.execution.plan_manager.receive_plan({"date": date, "recommendations": recommendations})
            # REC 通电卡 (2026-09-24): 推荐落地即开跟踪 (用户定谳「执行层=推荐
            # 跟踪器」——推过就跟踪, 不问买没买)。此前 RecommendationTracker(
            # 生产零调用 → 表永不建 (懒建病)。落库失败不炸主链 (同
            # _persist_invalidation_warnings 先例: 降级留痕, 铁律#12)。
            try:
                from execution_layer.tracking.recommendation_tracker import RecommendationTracker
                _tracker = RecommendationTracker(app.data.db if hasattr(app, "data") and app.data else None)
                for rec in recommendations:
                    # 单一写者 (2026-09-25 修法丙): confidence / confidence_tier /
                    # confidence_reason / entry_basis_trace_id / updated_at 全由
                    # start() 一次写完 —— 调用方**不得**再补 UPDATE。
                    # 此前这里补两条 UPDATE, 而 start() 幂等命中时跳过不刷
                    # confidence → 同日二次推荐字段分裂 (688187: conf 冻在 0.474,
                    # tier/reason 却是 0.623 算出的 high/"0.62"), 且不刷 updated_at
                    # → 第二次写零时间戳留痕。判例「幂等跳过+无条件覆盖=字段分裂」。
                    _tracker.start(
                        code=rec["code"], rec_date=date,
                        rationale_id=rec.get("rationale_id", ""),
                        verdict=rec.get("action", ""), reason=rec.get("reason", ""),
                        confidence=rec.get("confidence"))
                logger.info(f"REC tracking: {len(recommendations)} 只推荐已开跟踪")
            except Exception as _trk_e:  # noqa: BLE001 — 跟踪落库是附加面, 不炸推荐主链
                logger.warning(f"REC tracking 落库失败 (不炸主链): {_trk_e}")
        return HandlerResult(success=True, errors=[])

    def _run_fwd_return_backfill(self, cfg: dict):
        """DL-34 (D-34): 前向收益每日回填调度 — 独立管道, 排在 factor_update 之后。

        每日为"刚兑现"的日期回填 fwd_return_1d/5d/20d 到 factor_daily。复用
        data_layer.backfill_fwd_returns (DL-24: 幂等, 仅写 NULL 行, 未兑现保持 NULL,
        百分点单位)。回填写的是 factor_daily 的列, 必须等因子先算完 → depends_on
        factor_update。

        每个 horizon h 的"最近兑现日" = 最新 kline 日往前数第 h 个交易日 (该日之后
        已有 h 个交易日收盘, 收益已实现)。catchup_days (默认 3) 控制每次回填最近几个
        兑现日 — 幂等, 多跑无害, 兼做漏跑自愈。历史全量回补走
        scripts/backfill_fwd_returns_history.py (写库, 需 GO-9), 不在本每日管道内。

        铁律 #12: data 层缺失/单日异常不得静默 — 缺失报 failed, 单日异常记 errors 留痕。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        if data is None or not hasattr(data, "backfill_fwd_returns"):
            logger.error("fwd_return_backfill: data 层不可用 (无 backfill_fwd_returns)")
            return HandlerResult(success=False,
                                 errors=["data layer unavailable for fwd_return_backfill"])

        from data_layer._loader import FWD_HORIZONS as _fwd_horizons

        def data_layer_fwd_horizons():
            return _fwd_horizons

        catchup_days = int(cfg.get("catchup_days", 3))
        conn = self.db.get_connection()
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_klines ORDER BY date").fetchall()]
        if not dates:
            logger.warning("fwd_return_backfill: daily_klines 无数据, 跳过")
            return HandlerResult(success=True, errors=[],
                                 metadata={"filled_total": 0, "skipped": "no_klines"})

        n = len(dates)
        filled_total = 0
        errors: list[str] = []
        per_horizon: dict[int, int] = {}
        for h in data_layer_fwd_horizons():
            # 最近兑现日索引 = n-1-h; <0 说明历史不足 h 个交易日, 该 horizon 跳过。
            latest_realized_idx = n - 1 - h
            if latest_realized_idx < 0:
                continue
            filled_h = 0
            # 从最近兑现日往前回填 catchup_days 个 (幂等)。
            for i in range(catchup_days):
                idx = latest_realized_idx - i
                if idx < 0:
                    break
                target = dates[idx]
                try:
                    res = data.backfill_fwd_returns(target, horizons=(h,))
                    if res is None:
                        # catch_quantstar_error 吞异常返回 None — 不静默, 记错误。
                        errors.append(f"{target} h={h}: backfill 返回 None (异常被吞)")
                        continue
                    filled_h += int(res.get(h, 0) or 0)
                except Exception as e:
                    errors.append(f"{target} h={h}: {e}")
            per_horizon[h] = filled_h
            filled_total += filled_h

        metadata = {"filled_total": filled_total, "by_horizon": per_horizon,
                    "catchup_days": catchup_days, "latest_kline": dates[-1]}
        if errors:
            metadata["errors"] = errors
            logger.warning(f"fwd_return_backfill 部分失败: {len(errors)} 条: {errors[:5]}")
        return HandlerResult(success=True, errors=errors, metadata=metadata)

    def _run_rec_tracking_daily(self, cfg: dict):
        """REC 通电卡 (2026-09-24): 每日一致性守护 + 覆盖率落库。

        挂 retrospect_settlement 同族 (盘后回看推荐)。此前 consistency_check/
        coverage 生产零调度 =「每日自问」无载体。失败不静默 (铁律#12)。
        """
        try:
            from execution_layer.tracking.daily_guard import run_daily_guard
            db = getattr(getattr(self, "_get_app")(), "data", None)
            db = getattr(db, "db", None)
            if db is None:
                logger.warning("rec_tracking_daily: db 不可得, 跳过")
                return HandlerResult(success=True, metadata={"skipped": True})
            result = run_daily_guard(db)
            return HandlerResult(success=True, metadata={
                "tracked_total": result.get("tracked_total", 0),
                "silent_flip_count": result.get("silent_flip_count", 0),
                "coverage_ratio": result.get("coverage_ratio"),
            })
        except Exception as e:  # noqa: BLE001
            logger.error(f"rec_tracking_daily 失败: {e}")
            return HandlerResult(success=False, errors=[f"rec_tracking_daily: {e}"])

    def _run_quality_check(self, cfg: dict):
        """DL-33 (D-33): 数据质量门禁独立管道, 排在 daily_data_pipeline 之后。

        检查当日采集数据完整性/格式合规性, 结果落 quality_checks 表。
        未注册则跳过+告警 (铁律 #12 不静默), 不判失败。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("quality_check") is None:
            logger.warning("quality_check 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        pip_input = PipelineInput(date=bj_date(), db=getattr(data, "db", None))
        result = data.run_pipeline("quality_check", pip_input)
        records = getattr(result, "records_written", 0) if result else 0
        return HandlerResult(success=True, metadata={"quality_records": records})

    def _run_freshness_audit(self, cfg: dict):
        """DL-33 (D-33): 数据新鲜度审计独立管道, 排在 daily_data_pipeline 之后。

        检查主链路表数据是否当日到位, 结果落 freshness_audit 表。
        未注册则跳过+告警 (铁律 #12 不静默), 不判失败。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("freshness_audit") is None:
            logger.warning("freshness_audit 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        pip_input = PipelineInput(date=bj_date(), db=getattr(data, "db", None))
        result = data.run_pipeline("freshness_audit", pip_input)
        records = getattr(result, "records_written", 0) if result else 0
        return HandlerResult(success=True, metadata={"freshness_records": records})

    def _run_source_crosscheck(self, cfg: dict):
        """P0-5: 源健康第三方对拍 (新浪参照), 抽样核对主源 close/volume。

        差异落 source_crosscheck_diffs 表; 差异率超阈值发 warning 告警
        (源健康第三方证据, 独立通道)。对拍是观测件: 参照源不可达降级 partial
        不阻塞, 未注册跳过+告警 (铁律 #12 不静默)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("source_crosscheck") is None:
            logger.warning("source_crosscheck 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        pip_input = PipelineInput(
            date=bj_date(), db=getattr(data, "db", None),
            config={k: v for k, v in (cfg or {}).items()
                    if k in ("sample_size", "diff_rate_alert", "timeout_s")},
        )
        result = data.run_pipeline("source_crosscheck", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["source_crosscheck 管线返回空结果"])
        meta = getattr(result, "metadata", {}) or {}
        n_diff = meta.get("n_diff", 0)
        diff_rate = meta.get("diff_rate", 0.0)
        alert_at = meta.get("diff_rate_alert", 0.05)
        status = getattr(result, "status", "failed")
        # 差异率超阈值 → warning 告警 (源健康第三方证据, 独立通道)
        if status == "success" and diff_rate > alert_at:
            from ops_layer.core.models import Alert
            self.alert_manager.send(Alert(
                severity="warning", layer="data",
                title="source_crosscheck 主源与新浪参照差异率超阈值",
                detail=(f"date={pip_input.date} sampled={meta.get('n_sampled')} "
                        f"diff={n_diff} diff_rate={diff_rate:.2%} > {alert_at:.0%}, "
                        f"明细见 source_crosscheck_diffs"),
                action="核对 daily_klines 主源采集链路是否异常",
            ))
        return HandlerResult(
            success=(status in ("success", "partial")),
            errors=list(getattr(result, "errors", []) or []),
            metadata={"crosscheck_diffs": n_diff, "diff_rate": diff_rate,
                      "n_sampled": meta.get("n_sampled", 0),
                      "ref_source": meta.get("ref_source", "")},
        )

    def _run_valuation_backfill(self, cfg: dict):
        """丙件 (2026-09-25): 估值顶层补跑腿 (17:45)。

        与 _run_kline 同族形态, 但语义不同: kline 补跑腿是"重跑主跑 + 降级链";
        本腿是"只补缺口" —— 主跑因 tushare bulk 限流采不满时 (09-16~09-24
        实测 ~34%), 当日缺口是终态 (T+1 不补), 本腿用 baostock 逐股补齐。

        幂等: 缺口率 < GAP_SHORT_CIRCUIT_RATIO (2%) 则零开销短路, 主跑成功日
        不重复跑 (实测 3600 只 ~8.2 分钟, 不该天天付)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("valuation_backfill") is None:
            logger.warning("valuation_backfill 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(date=date, start_date=date, end_date=date,
                                  db=getattr(data, "db", None))
        result = data.run_pipeline("valuation_backfill", pip_input)
        if result is None:
            return HandlerResult(
                success=False, errors=["valuation_backfill 管线返回空结果"])
        status = getattr(result, "status", "failed")
        md = getattr(result, "metadata", {}) or {}
        return HandlerResult(
            success=(status in ("success", "partial")),
            errors=list(getattr(result, "errors", []) or []),
            metadata={"backfill_records": getattr(result, "records_written", 0),
                      "short_circuited": md.get("short_circuited"),
                      "gap": md.get("gap"), "gap_ratio": md.get("gap_ratio")})

    def _run_kline(self, cfg: dict):
        """DL-51: kline 顶层备源补跑 (17:40)。

        与 daily_data_pipeline 的子管线 kline 同名但走同一 data_layer 管线实例:
        幂等 — 主源 15:30 已成功时, KlinePipeline 的覆盖检查 (market_covered,
        活跃股票集 98% 已覆盖) 短路返回 success/records_written=0, 不重复写;
        主源失败时本跑走注册表降级链 (tushare→baostock, 17:30 后入库) 兜底。
        未注册则跳过+告警 (铁律 #12 不静默)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("kline") is None:
            logger.warning("kline 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(date=date, start_date=date, end_date=date,
                                  db=getattr(data, "db", None))
        result = data.run_pipeline("kline", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["kline 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        mode = (getattr(result, "metadata", {}) or {}).get("mode", "")
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={"kline_records": records, "mode": mode,
                      "short_circuited": mode == "market_covered"})

    def _run_news_per_stock(self, cfg: dict):
        """AG-NEWS 注册面补齐 (sm-d7b43861): news_per_stock 17:00 顶层管道。

        候选池 = stock_pool 昨荐 (added_date >= T-1 非 exited) + 活仓
        (active/pending_entry/watch) — 预检清单同口径 ~30 只。
        经 PipelineInput.symbols 透传 → NewsPipeline._fetch_per_stock_em
        (逐股 1s 节流, 单股 429 降级 skip)。空池 (周末/清池) 诚实 skip
        非假 success。
        """
        from common.utils import bj_date
        from datetime import datetime as _dt, timedelta as _td
        app = self._get_app()
        data = getattr(app, "data", None)
        if data is None:
            return HandlerResult(success=False, errors=["data layer 不可用"])
        date = cfg.get("date") or bj_date()
        yesterday = (_dt.strptime(date, "%Y-%m-%d") - _td(days=1)).strftime("%Y-%m-%d")
        codes: list[str] = []
        try:
            conn = getattr(data, "db", None)
            if conn is not None:
                conn = conn.get_connection()
                rows = conn.execute(
                    "SELECT DISTINCT code FROM stock_pool "
                    "WHERE (added_date >= ? AND status != 'exited') "
                    "   OR status IN ('active', 'pending_entry', 'watch')",
                    (yesterday,),
                ).fetchall()
                codes = [r[0] for r in rows][:30]
        except Exception as e:
            logger.warning(f"news_per_stock 候选池查询失败: {e}")
        if not codes:
            logger.info("news_per_stock: 候选池空 (周末/清池), 诚实 skip")
            return HandlerResult(success=True, metadata={"skipped": True, "pool_size": 0})
        from data_layer.pipelines.base import PipelineInput
        pip_input = PipelineInput(date=date, db=getattr(data, "db", None), symbols=codes)
        result = data.run_pipeline("stock_news", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["stock_news 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={"pool_size": len(codes), "news_records": records},
        )

    def _run_emotion_cycle(self, cfg: dict):
        """F 席 ST-REGIME 注册面补齐: emotion_cycle 留痕产线 handler。

        调 strategy_layer.regime.persist.run_emotion_cycle_daily (判定核
        纯函数在 main, 本 handler 只做接线+结构化结果)。unknown 也落库
        = 诚实留痕。
        """
        from common.utils import bj_date
        app = self._get_app()
        date = cfg.get("date") or bj_date()
        try:
            from strategy_layer.regime.persist import run_emotion_cycle_daily
            db = getattr(app.data, "db", None)
            out = run_emotion_cycle_daily(db, date=date) or {}
        except Exception as e:
            return HandlerResult(success=False, errors=[f"emotion_cycle 失败: {e}"])
        # R1 修 (统筹 sm-d32393cc82af4207 立卡): persisted 位接进 success —
        # 落库失败 (persisted=False, run_emotion_cycle_daily 只 warning 不抛)
        # 不得伪装 success, 否则 pipeline_runs 把没写进表的跑批记绿 (纪律③).
        if out.get("persisted") is not True:
            return HandlerResult(
                success=False,
                errors=[f"emotion_cycle 落库失败: persisted={out.get('persisted')} "
                        f"stage={out.get('stage')} hit={out.get('hit')} "
                        f"— 详情见 strategy.regime.persist warning 日志"],
                metadata={"stage": out.get("stage", "unknown"),
                          "rows": out.get("rows", 0),
                          "persisted": False},
            )
        return HandlerResult(
            success=True,
            errors=[out["error"]] if out.get("error") else [],
            metadata={"stage": out.get("stage", "unknown"), "rows": out.get("rows", 0)},
        )

    def _run_concept_plates_daily(self, cfg: dict):
        """R4 (09-01 concept 断电案, 统筹 sm-39c8b41402a748ee): concept_plates
        独立 16:35 顶层小管道。

        背景: 每日 DDP 的 concept child (ConceptPipeline) 三步全 fallback/
        冒充, zzshare 真概念通路被 GO-7 硬闸挡在日管道外 — campaign 收官后
        新交易日结构性 fallback-only (08-28/31 缩水 95% 实证)。本管道与
        #81 c-1 analyst 拆分案同构: 独立排程错峰, 不碰 DDP 结构。

        写锁实测 (R3 冒烟 09-01): fetch 142.45s 在写事务外 (不持锁),
        写锁内 29.796s (115k 行单事务) — GO-7 当年 22-40min 顾虑实为
        把 fetch 误算进持锁窗。16:35 起 → ~16:38 fetch 完 → ~16:40 写完
        净退, 与 DDP (concept child ~15:45 到达, 本地 fallback 秒级) 完全
        错峰。

        时序颠倒风险 (DDP concept child 若在本管道后跑, INSERT OR IGNORE
        会把 fallback 行混回) 由 17:05 写后校验哨兵对治 (issues daily 链,
        industry_fallback 再现 = 报 issues 非静默)。未注册则跳过+告警
        (铁律 #12 不静默)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("concept_plates") is None:
            logger.warning("concept_plates 管线未注册, 跳过 (真概念数据缺口 — 铁律 #12)")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(
            date=date, db=getattr(data, "db", None),
            config={"backfill": False, "sleep_s": 0.05,
                    "timeout_s": cfg.get("timeout_s", 600)})
        result = data.run_pipeline("concept_plates", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["concept_plates 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        meta = getattr(result, "metadata", {}) or {}
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={
                "concept_records": records,
                "write_lock_hold_s": meta.get("write_lock_hold_s"),
                "fetch_duration_s": meta.get("fetch_duration_s"),
                "deleted_fallback_rows": meta.get("deleted_fallback_rows"),
                "universe_size": meta.get("universe_size"),
                "filtered_out": meta.get("filtered_out"),
            })

    def _run_shortterm_recollect(self, cfg: dict):
        """刀2 (fix/anomaly-1630-recollect, 派单 sm-7c5089a73bd44eaf):
        shortterm 快照六项 16:30 补采轮 — 治「源在但漏」永久缺口。

        病灶: anomaly-analysis-list 端点无日期参数 = 纯当日快照, 上游
        发布可能晚于 15:44 首轮窗 (09-29 16:48 采得 200 行 vs 10-09
        15:44 空), DL-07「不可回填」下漏采即永久失去。10-19 anomaly
        402 行坐实当日更晚时刻数据在 — 补采轮补回。

        设计: 独立顶层小管道 (照 concept_plates_daily 先例 — 独立排程
        错峰, 不进 DAILY_SUB_PIPELINES, GO-7 硬闸语义保持); 只跑快照
        六项 (ShortTermCollectPipeline._snapshot_collectors 单一事实源),
        绝不碰 kline/valuation 主腿; INSERT OR REPLACE 幂等, 同日同键
        以补采轮为准 (更晚快照) 且覆盖必须留痕 (metadata
        recollect_overwrote, 不静默覆盖 — 铁律#12)。

        窗口 16:30: DDP 实测 15:30→15:44 (~14min) 后 46min 隔离;
        16:20/16:22 DL-33 双门禁之后 (不扰门禁首轮读数)。
        未注册管线则跳过+告警 (铁律 #12)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("shortterm_recollect") is None:
            logger.warning(
                "shortterm_recollect 管线未注册, 跳过 (快照补采缺口 — 铁律 #12)")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        t0 = time.time()
        result = app.data.run_pipeline(
            "shortterm_recollect",
            PipelineInput(date=date, db=getattr(data, "db", None)))
        elapsed = round(time.time() - t0, 1)

        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        meta = dict(getattr(result, "metadata", {}) or {})
        recollect_overwrote = meta.get("recollect_overwrote", [])

        # DL-43: partial / success / failed 三态对齐 HandlerResult 契约
        # 铁律 #12: 补采轮覆盖必须留痕 (recollect_overwrote)
        is_partial = (status == "partial")
        is_success = (status in ("success", "partial"))

        meta.update({
            "date": date,
            "records_written": records,
            "recollect_overwrote": recollect_overwrote,
            "elapsed_s": elapsed,
            "partial": is_partial,
        })

        return HandlerResult(
            success=is_success,
            records_written=records,
            errors=errors,
            metadata=meta,
        )

    def _run_concept_intervals_daily(self, cfg: dict):
        """件2 (09-02, 统筹 sm-8683e2dff09a4717): concept_intervals 独立
        17:05 顶层小管道 — concept_membership 成分变更区间存储。

        照 R4 concept_plates_daily 先例 (与 #81 c-1 analyst 拆分同构):
        独立排程错峰, 不碰 DDP 结构, GO-7 硬闸语义保持 (不进
        DAILY_SUB_PIPELINES — 08-31 调研卡钉死: 代码全有库里全无, 建了
        没通电又一处)。

        语义: 一次拉取即得历史进出板全史 (适配器 concept_intervals 面不
        做当日 PIT 过滤, time_in/time_out 原始区间), INSERT OR REPLACE
        幂等覆盖 — 出板事件 = 旧行 time_out 由 NULL 变有值。写锁只持
        写事务 (fetch 在外), 首跑建表 DDL 幂等。

        窗口: 17:05 — 16:35 concept_plates_daily 写完 (~16:40 净退) 后
        30min 隔离 (zzshare 适配器同源分摊限流面, 两管道间错开),
        16:52 agent_evaluate 之前错峰。未注册则跳过+告警 (铁律 #12)。

        回补腿 (D-104 幸存者偏差/历史区间重建) 另窗统筹裁, 本 handler
        只跑每日增量段: backfill=False。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("concept_intervals") is None:
            logger.warning("concept_intervals 管线未注册, 跳过 (区间存储缺口 — 铁律 #12)")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(
            date=date, db=getattr(data, "db", None),
            config={"backfill": False, "sleep_s": 0.05,
                    "timeout_s": cfg.get("timeout_s", 900)})
        result = data.run_pipeline("concept_intervals", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["concept_intervals 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        meta = getattr(result, "metadata", {}) or {}
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={
                "membership_records": records,
                "write_lock_hold_s": meta.get("write_lock_hold_s"),
                "fetch_duration_s": meta.get("fetch_duration_s"),
                "memberships_written": meta.get("memberships_written"),
                "rates_written": meta.get("rates_written"),
                "filtered_out": meta.get("filtered_out"),
                "universe_size": meta.get("universe_size"),
            })

    def _run_adjust_factor(self, cfg: dict):
        """DL-51: adjust_factor 顶层每日增量 (18:30)。

        baostock 复权因子 18:00 入库, 主采集 15:30 跑时当日数据尚不存在,
        故独立管道。幂等 INSERT OR REPLACE, 漏跑次日自动补齐。
        未注册则跳过+告警 (铁律 #12 不静默)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("adjust_factor") is None:
            logger.warning("adjust_factor 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(date=date, db=getattr(data, "db", None))
        result = data.run_pipeline("adjust_factor", pip_input)
        if result is None:
            return HandlerResult(success=False, errors=["adjust_factor 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={"adjust_records": records})

    def _run_analyst_forecasts(self, cfg: dict):
        """DDP c-1 (#81 两连超时修复, 统筹 sm-6e53bd96df784290):
        analyst_forecasts 独立 16:55 顶层每日管道 (从 DDP 串行链拆出)。

        背景: 逐码串行 HTTP ~5748 码 × ~318ms ≈ 30min 固定成本 (08-31 实测
        1824.4/1829.4s, σ≈2.5s), 在 DDP 串行链内把总需求顶到 2738/3223s
        > 2700 预算 → 两连超时 (均为 late_success: ghost 跑完数据全量落库,
        损失的是状态正确性)。拆出后 DDP 串行 908/1393s 回预算内。
        本管道自身 1900s 预算 (ops.yaml timeout_s 与 data_layer
        _capability_timeout 双钉对齐), 16:55 起跑 → ~17:26 收官, 与
        dev_verify_report(17:15)/ops_report(17:30) 只读无锁冲突。
        重试路径 (max_retries=1) 最坏 ~17:59 重叠 17:40 kline — kline
        wait_timeout_s=300 等锁语义兜底。未注册则跳过+告警 (铁律 #12)。
        """
        app = self._get_app()
        data = getattr(app, "data", None)
        _pipelines = getattr(data, "pipelines", None) or {}
        if _pipelines.get("analyst_forecasts") is None:
            logger.warning("analyst_forecasts 管线未注册, 跳过")
            return HandlerResult(success=True, metadata={"skipped": True})
        from data_layer.pipelines.base import PipelineInput
        from common.utils import bj_date
        date = cfg.get("date") or bj_date()
        pip_input = PipelineInput(date=date, db=getattr(data, "db", None))
        result = data.run_pipeline("analyst_forecasts", pip_input)
        if result is None:
            # 08-17 analyst 60s 截断误报教训 (同型): None 必须显式失败留痕 —
            # c-1 独立路径 1900s 双钉下不应发生, 发生即事件日证据。
            return HandlerResult(success=False,
                                 errors=["analyst_forecasts 管线返回空结果"])
        status = getattr(result, "status", "failed")
        records = getattr(result, "records_written", 0)
        errors = list(getattr(result, "errors", []) or [])
        meta = getattr(result, "metadata", {}) or {}
        return HandlerResult(
            success=(status == "success"), errors=errors,
            metadata={
                "analyst_records": records,
                "coverage": meta.get("coverage"),
                "attempted": meta.get("attempted"),
            })

    def _run_daily_backtest(self, cfg: dict):
        """BT-02 (2026-08-18) 去退化: 回测对象=当日真实推荐 (stock_pool.added_date),
        持有窗口=(signal_date, signal_date+hold_days)。

        原实现 date_range=(date, date) 当日收盘买当日收盘卖 (还违反 T+1),
        probe_codes 写死 ["000001","600519"] 与真实推荐无关 — 每天产假数字。
        现: 无当日推荐则跳过并记录, 不跑探针假验证 (宁可没有, 不可是假的)。
        持有窗口尾部若超出最新数据日, HoldMode 自动收敛到最后可用交易日,
        metadata 标 partial_window 提示窗口未走完。
        """
        from backtest_layer.core.models import BacktestInput, CostConfig
        from common.database import Database
        from common.utils import bj_date
        db = Database()
        conn = db.get_connection()
        latest_factor = conn.execute("SELECT MAX(date) FROM factor_daily").fetchone()[0]
        latest_kline = conn.execute("SELECT MAX(date) FROM daily_klines").fetchone()[0]
        kline_date = latest_kline if latest_kline else bj_date()
        factor_date = latest_factor if latest_factor else bj_date()
        date = max(factor_date, kline_date)

        # 当日真实推荐: stock_pool 中 added_date = 信号日的条目
        try:
            rows = conn.execute(
                "SELECT code FROM stock_pool WHERE added_date = ?", (date,)
            ).fetchall()
            codes = [r[0] for r in rows]
        except sqlite3.OperationalError:
            conn.close()
            logger.warning("Daily backtest skipped: stock_pool 表不存在")
            return HandlerResult(success=True, metadata={
                "date": date, "status": "skipped", "reason": "stock_pool_missing"})
        conn.close()

        if not codes:
            logger.info(f"Daily backtest skipped on {date}: 当日无真实推荐 (stock_pool.added_date)")
            return HandlerResult(success=True, metadata={
                "date": date, "status": "skipped", "reason": "no_recommendations"})

        hold_days = int(cfg.get("hold_days", 5))
        # BT-FIX ① (sm-6d296fb2f3534a87): 持有窗尾按交易日推算, 不再用自然日+hold_days。
        # 旧病理: 自然日窗尾在生产跑时刻永远在未来 → 卖出腿反向找交易日塌回 entry/空日 →
        # 静默零卖出 → nav({}) 假 -68%~-99% (8 条历史假行)。
        # conn 已在 codes 段 close — 重开只读连接取交易日序列 (daily_klines DISTINCT)。
        tconn = db.get_connection()
        try:
            trading_days = [r[0] for r in tconn.execute(
                "SELECT DISTINCT date FROM daily_klines WHERE date >= ? ORDER BY date",
                ((datetime.strptime(date, "%Y-%m-%d") - timedelta(days=15)).strftime("%Y-%m-%d"),)
            ).fetchall()]
        finally:
            tconn.close()
        from backtest_layer.modes.hold import FILL_NEXT_OPEN, _compute_hold_end_date
        # BT-CORRECTNESS ①: 默认 T+1 开盘成交 (信号日收盘成交 = 前视)。持有窗整体后移一日:
        # 建仓日 = signal 后第 1 个交易日, 窗尾 = 建仓日后第 hold_days 个交易日。
        fill_price = str(cfg.get("fill_price", FILL_NEXT_OPEN) or FILL_NEXT_OPEN)
        window_days = hold_days + 1 if fill_price == FILL_NEXT_OPEN else hold_days
        end_date, window_partial = _compute_hold_end_date(date, window_days, trading_days)
        if window_partial:
            logger.warning(
                f"Daily backtest on {date}: hold window truncated — only "
                f"{len([d for d in trading_days if d > date])} trading days "
                f"available for hold_days={hold_days}, end={end_date}")

        from bootstrap import bootstrap_app
        app = bootstrap_app()
        bt_input = BacktestInput(
            mode="hold", codes=codes,
            date_range=(date, end_date),
            hold_days=hold_days,
            cost=CostConfig(initial_capital=cfg.get("execution", {}).get("initial_capital", 300000)),
            fill_price=fill_price,
        )
        result = app.backtest.run(bt_input)
        # 窗口未走完 (尾部超出最新数据日): equity_curve 最后日期 < 目标 end_date
        curve = getattr(result, "equity_curve", None) or []
        partial_window = bool(curve) and curve[-1].get("date", "") < end_date
        metric = {
            "date": date,
            "status": "completed",
            "codes": codes,
            "hold_days": hold_days,
            "fill_price": fill_price,
            "window": [date, end_date],
            "partial_window": partial_window or window_partial,
            "sharpe": getattr(result, "sharpe", None),
            "total_return": getattr(result, "total_return", None),
            "max_drawdown": getattr(result, "max_drawdown", None),
        }
        logger.info(f"Daily backtest: {metric}")
        return HandlerResult(success=True, metadata=metric)

    def _run_evening_report(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_evening_report(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "evening", "delivered": msg is not None})

    def _run_premarket_brief(self, cfg: dict):
        """报告宪法 #7: 盘前速报 09:25 — 竞价快评+操作指引。"""
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_premarket_brief(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "premarket_brief", "delivered": msg is not None})

    def _run_morning_brief(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_morning_brief(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
            return HandlerResult(success=True, metadata={"date": date, "report": "morning_brief", "delivered": True})
        # B9 knife1 (fix/morning-brief-red): build 返 None（超时截断）必须是失败，
        # 不得假 success — 10-09 起 12 交易日停推零告警即因此静默吞掉。
        return HandlerResult(
            success=False,
            errors=["morning_brief build returned None (timeout 60s), not delivered"],
            metadata={"date": date, "report": "morning_brief", "delivered": False},
        )

    def _run_tail_advisory(self, cfg: dict):
        """卡VII: 尾盘投顾 14:52 — 只读, 同链 report_layer build+push。"""
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_tail_advisory(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "tail_advisory", "delivered": msg is not None})

    def _run_health_check(self, cfg: dict):
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        report = app.ops.check_health()
        result = {"status": report.overall_status}
        if report.overall_status != "green":
            logger.warning(f"Health check: {report.overall_status}")
            for layer in report.layers:
                if layer.status != "green":
                    logger.warning(f"  [{layer.status}] {layer.layer}: {layer.summary}")
                    result[layer.layer] = layer.status
        # F6.3: 健康检查后跑异常检测 → AlertManager (goal 选项 a)
        anomalies = self._run_anomaly_detection(report)
        if anomalies:
            result["anomalies"] = len(anomalies)
        # OPS-05: 调度漂移检测纳入健康检查末尾 — 失败只 warning 不阻断
        try:
            from ops_layer.schedule_check import run_schedule_check
            ok, drift = run_schedule_check(pipelines=self._pipeline_configs)
            if drift.get("crontab_unavailable"):
                logger.warning("schedule-check: crontab 不可读, 跳过对账")
            elif not ok:
                logger.warning(f"schedule-check 漂移: missing={drift['missing']}")
                result["schedule_drift"] = drift["missing"]
        except Exception as e:
            logger.warning(f"schedule-check skipped (fail-safe): {e}")
        # DL-51 第三方对账: data_layer 注册表 vs ops 层接线 — 检出"注册了但
        # ops 层不知道"的孤立管线 (DL-33 病灶的自动检出, G53 管道维度实例)。
        # 失败只 warning 不阻断。
        try:
            from ops_layer.schedule_check import check_orphan_pipelines
            app = self._get_app()
            data = getattr(app, "data", None)
            _pipelines = getattr(data, "pipelines", None)
            # PipelineRegistry.get_all() → [BasePipeline]; 取 .name
            registered = [p.name for p in getattr(_pipelines, "get_all", lambda: [])()]
            if registered:
                orphan = check_orphan_pipelines(
                    registered, self._pipeline_configs,
                    DAILY_SUB_PIPELINES + DAILY_SUB_PIPELINES_CONDITIONAL,
                    handler_hosted=HANDLER_HOSTED_PIPELINES)
                if orphan["orphans"]:
                    logger.warning(
                        f"orphan-pipelines: 注册了但无调度宿主: {orphan['orphans']}")
                    result["orphan_pipelines"] = orphan["orphans"]
        except Exception as e:
            logger.warning(f"orphan-pipeline check skipped (fail-safe): {e}")
        # OPS-07: 告警升级检查纳入健康检查 — 此前唯一调用方是无人看的 get_dashboard,
        # 升级从未在生产触发; 失败只 warning 不阻断
        try:
            escalated = self.alert_manager.check_escalation()
            if escalated:
                result["escalated_alerts"] = len(escalated)
        except Exception as e:
            logger.warning(f"check_escalation skipped (fail-safe): {e}")
        # G4 盲区补偿 (2026-08-24夜): quality_check 型管道死于 start_run 前
        # 只留 observability_fallback.jsonl 条目; 此处补扫升格为 alerts 行。
        # 接入现有 health_check 不新增 crontab (铁律 #11); 失败只 warning 不阻断。
        try:
            from ops_layer.g4_blindspot import promote_fallback_entries
            promoted = promote_fallback_entries(self.db)
            if promoted:
                logger.warning(f"[G4] blindspot: promoted {promoted} fallback entry(ies) to alerts")
                result["g4_blindspot_alerts"] = promoted
        except Exception as e:
            logger.warning(f"g4 blindspot skipped (fail-safe): {e}")
        # DL-26: 源数据层健康巡检 — 跨源行数交叉验证, 检出"调用成功但 0 行"
        # 的静默失效 (fuyao limit_up/limit_down 连续 4 天 0 行零告警, F-23)。
        # 接入现有 health_check (10:00/14:00/16:00), 不新增 crontab (铁律 #11);
        # 只探测便宜源类型 (fuyao/akshare 免费 HTTP), 昂贵源不主动探测避免限频。
        try:
            source_alerts = self._run_source_data_health(app)
            if source_alerts:
                result["source_data_health_alerts"] = source_alerts
        except Exception as e:
            logger.warning(f"source data health skipped (fail-safe): {e}")
        return HandlerResult(success=True, metadata=result)

    def _run_source_data_health(self, app) -> list[str]:
        """DL-26: 源数据层交叉验证 + 告警 (复用 OPS-07 告警闭环, 不造第二套)。

        对 PROBE_DATA_TYPES (limit_up/limit_down) 做跨源行数比较:
        其他源有数据而本源 0 行 → 本源 suspect → AlertManager 告警。
        去重: 同 title 已有 open 告警则不重复创建 (主源失效连续多天,
        不能每天 3 次轰炸)。P0-3② 后 AlertManager.should_alert 已原生按
        open 告警存在性做状态机去重, 此处本地检查保留作纵深防御快路径。
        """
        from common.utils import bj_date
        from data_layer.source_health import SourceHealthChecker
        from ops_layer.core.models import Alert

        registry = getattr(app.data, "_source_registry", None)
        if registry is None:
            return []

        checker = SourceHealthChecker(registry)
        date = bj_date()
        findings = checker.check_data_health(date)

        alerts_created = []
        for data_type, info in findings.items():
            if info.get("verdict") != "suspect":
                continue
            sources = info.get("sources", {})
            for suspect_name in info.get("suspect", []):
                title = f"源数据异常: {suspect_name} {data_type} 返回0行"
                # 跨日去重: 同 title 已有 open 告警 → 跳过 (恢复后由人工/
                # resolve_pipeline_alerts 关闭, 下次失效才会再告警)
                try:
                    row = self.db.get_connection().execute(
                        "SELECT COUNT(*) FROM alerts WHERE title = ? AND status = 'open'",
                        (title,),
                    ).fetchone()
                    if row and row[0] > 0:
                        logger.info(f"source health: open 告警已存在, 去重跳过: {title}")
                        continue
                except Exception:
                    pass  # 查询失败不阻断告警 (AlertManager 状态机去重兜底)
                detail = (
                    f"数据层交叉验证: {suspect_name} 的 {data_type} 返回 0 行, "
                    f"但同类型其他源有数据 (各源行数: {sources})。\n"
                    f"日期: {date}。可能原因: 源接口变更/限频/上游数据缺失。\n"
                    f"降级机制仍在工作 (其他源已兜底), 但主源失效不应静默。"
                )
                alert = Alert(
                    severity="warning", layer="data", title=title, detail=detail,
                    action="检查该源接口状态; 确认是否调整 type_priorities 换主源",
                )
                if self.alert_manager.send(alert):
                    alerts_created.append(title)
        return alerts_created

    # #80: duration 基线维护的作业白名单 (marimo P4 14 作业口径 — 见
    # scripts/marimo_pages/_common.py JOBS; 只含 cron 白名单内管道, 不含
    # paper_trading 长驻进程类)
    _BASELINE_PIPELINE_WHITELIST = frozenset({
        "morning_brief", "pre_market_refresh", "auction_parse", "morning_review",
        "midday_brief", "daily_data_pipeline", "factor_update", "ic_feedback",
        "quality_check", "freshness_audit", "daily_backtest", "evening_report",
        "daily_ops_report", "paper_trading", "stats_finalize",
    })

    def _baseline_pipelines(self) -> list[str]:
        """#80: 白名单 ∩ pipeline_runs 实有 success duration 记录的作业。

        自持数据源, 不依赖 load_pipelines() 加载序 (统筹 GO sm-1ed1590);
        空表/查询失败 = 空面空转不报错。
        """
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT DISTINCT pipeline_name FROM pipeline_runs "
                "WHERE status='success' AND duration_s IS NOT NULL"
            ).fetchall()
            known = {r["pipeline_name"] for r in rows}
        except Exception:
            known = set()
        return sorted(p for p in self._BASELINE_PIPELINE_WHITELIST if p in known)

    def _last_success_duration(self, pipeline_name: str) -> float | None:
        """#80: 最近一条 success duration (无则 None, 不参与比对)。"""
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT duration_s FROM pipeline_runs "
                "WHERE pipeline_name=? AND status='success' AND duration_s IS NOT NULL "
                "ORDER BY start_time DESC LIMIT 1",
                (pipeline_name,),
            ).fetchone()
            return row["duration_s"] if row else None
        except Exception:
            return None

    def _run_anomaly_detection(self, report) -> list[dict]:
        """F6.3: 记录健康历史 + 跨进程连续判定 + 最近运行扫描, 异常走 AlertManager。

        只接检测→告警; auto_healer 的破坏性 runbook (重启/清数/切换) 不自动触发,
        保留人工调用 (见 REPAIR_TRACKER F6.3 决策)。检测自身失败不影响健康检查主流程。
        """
        try:
            from ops_layer.anomaly_detector import AnomalyDetector
            from ops_layer.core.models import Alert

            detector = AnomalyDetector(self.db)
            layer_statuses = {l.layer: l.status for l in getattr(report, "layers", [])}
            detector.record_health(report.overall_status, layer_statuses)

            anomalies: list[dict] = []

            # #80 基线维护段 (16:50 health_check 唯一入口, 零新增 cron — 铁律 #11):
            # 刷新 14 作业白名单 duration 基线 → 对各作业最近一条 success duration
            # 做 z 检验, 命中并入本列表走下方既有 AlertManager 出口。维护段失败
            # fail-safe 只告警, 不阻断健康检查主流程 (与整段语义一致)。
            # 段内不再 import (v1 死导入教训: 段内 import 失败整段静默死)。
            try:
                _pipes = self._baseline_pipelines()
                _stats = detector.refresh_duration_baselines(_pipes)
                if _stats.get("upserted") or _stats.get("skipped"):
                    logger.info(
                        f"metric_baselines 维护: upsert={_stats['upserted']}, "
                        f"skipped={_stats['skipped']}")
                for _pn in _pipes:
                    _last = self._last_success_duration(_pn)
                    if _last is None:
                        continue
                    _a = detector.check_duration_baseline(_pn, _last)
                    if _a:
                        anomalies.append(_a)
            except Exception as _e:
                logger.warning(f"metric_baselines 维护段 skipped (fail-safe): {_e}")

            red = detector.check_health_red_db()
            if red:
                anomalies.append(red)
            anomalies.extend(detector.scan_recent_runs())

            for a in anomalies:
                self.alert_manager.send(Alert(
                    severity=a.get("severity", "warning"), layer="ops",
                    title=f"异常检测: {a.get('anomaly_type', 'unknown')}",
                    detail=a.get("detail", ""),
                    action="检查 pipeline_runs / health_history 并手动干预",
                ))
            if anomalies:
                logger.warning(f"Anomaly detection found {len(anomalies)} issue(s)")
            return anomalies
        except Exception as e:
            logger.warning(f"Anomaly detection skipped (fail-safe): {e}")
            return []

    def _run_pre_market_refresh(self, cfg: dict):
        from bootstrap import bootstrap_app
        from common.utils import bj_date
        date = bj_date()
        app = bootstrap_app()
        result = {"date": date, "status": "ok"}
        try:
            from data_layer.health.readiness import DataReadiness
            readiness = DataReadiness(app.data.db if hasattr(app.data, "db") else self.db).check(date)
            if hasattr(readiness, "is_ready") and not readiness.is_ready:
                result["status"] = "not_ready"
                logger.warning(f"Data readiness: not ready — {len(readiness.tables)}/{len(DataReadiness.REQUIRED_TABLES)} tables")
            else:
                logger.info("Data readiness: green")
        except Exception as e:
            logger.warning(f"Data readiness check: {e}")
            result["status"] = "error"
            result["detail"] = str(e)
        return HandlerResult(success=True, metadata=result)

    def _run_auction_parse(self, cfg: dict):
        from bootstrap import bootstrap_app
        from common.utils import bj_date
        date = bj_date()
        app = bootstrap_app()
        if hasattr(app, "execution") and app.execution and hasattr(app.execution, "stock_pool"):
            pending = app.execution.stock_pool.get_by_status("pending_entry")
            logger.info(f"Auction parse: {len(pending)} pending entries for {date}")
        # DL-22 (2026-08-18): 竞价快照冷采集 — 数据不可回填, 错过当日永久丢失。
        # 管线实现在数据层 (8b02cbc); 未注册则跳过并告警,
        # 采集失败/超时降级为 warning 不阻塞 auction_parse (冷采集 best-effort)。
        auction_records = None
        _pipelines = getattr(getattr(app, "data", None), "pipelines", None)
        if _pipelines is not None and _pipelines.get("auction_collect") is not None:
            from data_layer.pipelines.base import PipelineInput
            try:
                result = app.data.run_pipeline(
                    "auction_collect",
                    PipelineInput(date=date, db=getattr(app.data, "db", None)))
                auction_records = getattr(result, "records_written", 0) if result is not None else 0
                logger.info(f"Auction collect: {auction_records} records for {date}")
            except Exception as exc:
                logger.warning(f"auction_collect 冷采集失败 (不阻塞 auction_parse): {exc}")
        else:
            logger.warning("auction_collect 管线未注册 (数据层 DL-07 未合并), 竞价冷采集跳过")
        return HandlerResult(success=True, metadata={"date": date, "pending_entries": len(pending) if 'pending' in dir() else 0, "auction_records": auction_records})

    def _run_morning_review(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_morning_review(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "morning_review", "delivered": msg is not None})

    def _run_midday_brief(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_midday_brief(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "midday_brief", "delivered": msg is not None})

    def _run_daily_ops_report(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_ops_report(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "ops_report", "delivered": msg is not None})

    def _run_dev_verify_report(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_dev_verify(date, report_mode="debug")
        if msg:
            app.delivery.send(msg, report_mode="debug")
        return HandlerResult(success=True, metadata={"date": date, "report": "dev_verify", "delivered": msg is not None})

    def _run_intraday_monitor(self, cfg: dict):
        from bootstrap import bootstrap_app
        from common.utils import bj_now
        app = bootstrap_app()
        result = {"status": "ok"}
        try:
            snapshot = app.data.snapshot_intraday()
            n_quotes = len(snapshot.realtime_quotes) if hasattr(snapshot, "realtime_quotes") else 0
            n_anomalies = len(snapshot.anomalies) if hasattr(snapshot, "anomalies") else 0
            result["quotes"] = n_quotes
            result["anomalies"] = n_anomalies
            if n_anomalies > 0:
                logger.warning(f"Intraday monitor: {n_anomalies} anomalies detected")
                result["status"] = "anomalies"
                msg = app.report.build_intraday_alert(bj_now().split(" ")[0], report_mode=cfg_get("delivery.report_mode", "debug"))
                if msg:
                    app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
            else:
                logger.info(f"Intraday monitor: {n_quotes} quotes, no anomalies")
        except Exception as e:
            logger.warning(f"Intraday monitor error: {e}")
            result["status"] = "error"
            result["detail"] = str(e)
        return HandlerResult(success=True, metadata=result)

    def _run_stats_finalize(self, cfg: dict):
        """W2-1 (2026-09-20): 收盘后 execution_stats 收尾腿 (幂等重算)。

        病根: save_daily 原挂 daily_update (cron 16:16), 而 entry_date /
        action_records.entry 由 paper_broker 在 **16:35** 写 (同事务相邻两行)
        → 统计早于写入 19 分钟 → executed_entries 恒 0 (09-14~09-18 生产实证)。

        修法: 本腿在 paper_trading (16:35) 之后跑, 幂等重算当日 execution_stats
        (save_daily 内部先 DELETE 后 INSERT, 天然支持重跑/补偿)。

        口径见 stats/performance.py save_daily: executed_entries 读
        stock_pool.entry_date (无 status 限定; 适用下界 2026-08-12)。
        """
        from common.utils import bj_date
        from execution_layer.stats.performance import ExecutionStats
        date = cfg.get("date") or bj_date()
        try:
            ExecutionStats(self.db).save_daily(date)
            logger.info(f"stats_finalize: execution_stats 已重算 date={date}")
            return HandlerResult(success=True, records_written=1,
                                 metadata={"date": date, "recomputed": True})
        except Exception as e:
            logger.error(f"stats_finalize failed: {e}")
            return HandlerResult(success=False, errors=[f"stats_finalize failed: {e}"])

    def _run_adata_sentiment(self, cfg: dict):
        """派单 sm-68dc9532247f42b1: adata_sentiment 接线 handler。

        照 _run_emotion_cycle 样板 (line 1419), 直接 instantiate
        AdataSentimentPipeline 不走 BasePipeline 正典契约
        (非正典 run(params:dict) vs 正典 run(input:PipelineInput))。

        degraded 语义映射 (派单 §4 T2):
          - degraded=False 全数据 → success=True
          - degraded=True 源缺  → success=True partial=True (诚实降级非 failed)
          - handler exception → success=False errors=[msg]
        """
        from common.utils import bj_date
        try:
            from data_layer.pipelines.adata_sentiment_pipeline import (
                AdataSentimentPipeline)
        except ImportError as e:
            return HandlerResult(
                success=False,
                errors=[f"adata_sentiment import failed: {e}"])

        app = self._get_app()
        date = cfg.get("date") or bj_date()
        data = getattr(app, "data", None)
        db = getattr(data, "db", None) if data else None
        registry = getattr(data, "_source_registry", None) if data else None

        try:
            pipeline = AdataSentimentPipeline(db=db, source_registry=registry)
            out = pipeline.run({"date": date}) or {}
        except Exception as e:  # noqa: BLE001 — handler 不抛,走 HandlerResult errors
            return HandlerResult(success=False, errors=[f"adata_sentiment fail: {e}"])

        pop_rows = out.get("popularity_rows", 0)
        hot_rows = out.get("concept_hot_rows", 0)
        degraded = bool(out.get("degraded", False))
        meta = {
            "date": out.get("date", date),
            "popularity_rows": pop_rows,
            "concept_hot_rows": hot_rows,
            "degraded": degraded,
            "partial": degraded,
        }
        errors = []
        if degraded:
            errors.append("adata_sentiment degraded: 无可用源或数据库连接失败")
        return HandlerResult(success=True, errors=errors, metadata=meta)

    def _run_evening_paper_trading(self, cfg: dict):
        """W2 补偿腿: 16:35 paper_trading 因依赖断链被 skip 时的当日补跑。

        病根 (2026-09-19 审计实证): 09-10/09-11 daily_data_pipeline failed →
        factor_update 16:15 skipped → paper_trading 16:35 **skipped**
        (depends_on:[factor_update]) → 当日 PTS 行 total_signals=None,
        信号消费永久丢失。factor_update 有 18:05 fx_settlement 补偿腿,
        paper_trading 此前无任何补偿 (同族第 N 例: 防线只装了一半)。

        本腿照 fx_settlement 先例: 顶层独立排程 + depends_on:[] (断链日仍可跑),
        幂等守卫短路保证正常日零开销:
          当日 PTS 行 total_signals > 0 → 已消费过, 短路;
          行缺失 / total_signals IS NULL → 补跑 _run_paper_trading (幂等:
          建仓走 PaperBroker 状态机, reconcile 走 INSERT OR IGNORE)。

        诚实降级 (铁律 #12): 补跑异常不吞 — success=False + 留痕, 不静默。
        """
        from common.utils import bj_date
        date = (cfg or {}).get("date") or bj_date()
        conn = self.db.get_connection()
        # 守卫语义 = 同日 MAX (任一行有值即已消费):
        # PTS 是纯 INSERT (无 PK/REPLACE), 同日可有多行 (partial 后补跑成功);
        # 首跑 NULL + 补跑有值的场景下, LIMIT 1 拿最新 NULL 会误判未消费
        # → 重复消费。同日任一行 total_signals>0 即已消费 (独立复核指正)。
        row = conn.execute(
            "SELECT MAX(total_signals) FROM paper_trading_stats WHERE date = ?",
            (date,)).fetchone()
        if row is not None and row[0] is not None and row[0] > 0:
            logger.info(
                f"evening_paper_trading: 当日已消费 ({row[0]} 信号), 短路")
            return HandlerResult(success=True, metadata={
                "skipped": True, "reason": "already_consumed",
                "total_signals": row[0], "date": date})
        logger.warning(
            f"evening_paper_trading: 检出当日未消费 "
            f"(row={'absent' if row is None else row[0]}), 补跑纸交易")
        try:
            r = self._run_paper_trading(cfg)
        except Exception as e:
            logger.warning(f"evening_paper_trading 补跑异常: {e}", exc_info=True)
            return HandlerResult(success=False, errors=[f"compensation_failed: {e}"],
                                 metadata={"repaired": False, "date": date})
        meta = dict(getattr(r, "metadata", None) or {})
        meta.update({"repaired": True, "date": date})
        return HandlerResult(success=bool(getattr(r, "success", False)),
                             errors=list(getattr(r, "errors", None) or []),
                             metadata=meta)

    def _run_paper_trading(self, cfg: dict):
        """BT-12 (D-15 立项): 纸交易 — 持续监控 + 滑点实测 + 毕业闸门。

        每日收盘后跑: 读 stock_pool 当日真实推荐信号 → PaperTradingBridge 模拟
        执行 (带滑点/佣金/印花税) → reconciliation 对比信号价 vs 真实行情价算
        实测滑点 → 落 paper_trading / paper_trading_stats 表。

        落库 config 门控 (backtest.paper_trading.enabled, 默认关, 建表报用户 GO)。
        未启用时只内存模拟 + 返回统计, 不建表不写库。
        """
        from backtest_layer.paper_trading.runner import PaperTradingRunner
        from common.utils import bj_date
        date = bj_date()
        try:
            from bootstrap import bootstrap_app
            app = bootstrap_app()
            data_layer = app.data if hasattr(app, "data") else None
            config = getattr(data_layer, "config", None) if data_layer else None
            capital = cfg.get("execution", {}).get("initial_capital", 300000) if cfg else 300000
            runner = PaperTradingRunner(
                db=self.db, data_layer=data_layer, config=config,
                virtual_capital=float(capital),
            )
            daily = runner.run_daily(date)
            stats = runner.reconcile(date)
            # D7 通电: 毕业闸门 — 管道尾部检查, 只产建议不上实盘; 参数缺失诚实 skip
            graduation = runner.run_graduation_check(date)
            if graduation.get("decision") == "graduate":
                logger.warning(f"纸交易毕业建议: {graduation}")
            # #108 读腿: WFO 中间档权重可观测面 (paper_trading_weights 摘要,
            # 空态 None 诚实挂载 — wfo_paper_persist 未开/未跑时如实为 None)
            wfo_weights = runner.wfo_weights_on_test()
            meta = {
                "graduation": graduation,
                "wfo_weights_on_test": wfo_weights,
                "date": date,
                "n_signals": daily.n_signals,
                "n_filled": daily.n_filled,
                "paper_return": round(daily.paper_return, 4),
                "avg_slippage": stats.get("avg_slippage"),
                "max_slippage": stats.get("max_slippage"),
                "persisted": daily.persisted,
                "status": "completed",
            }
            logger.info(f"Paper trading: {meta}")
            return HandlerResult(success=True, metadata=meta)
        except Exception as e:
            logger.warning(f"Paper trading failed: {e}", exc_info=True)
            return HandlerResult(success=True, metadata={"date": date, "status": "error", "detail": str(e)})

    def _run_provider_check(self, cfg: dict):
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if hasattr(app, "agent") and app.agent:
            n_providers = app.agent.get_provider_count()
            logger.info(f"Provider check: {n_providers} providers available")
            return HandlerResult(success=True, metadata={"providers": n_providers})
        logger.warning("Provider check: agent layer not available")
        return HandlerResult(success=True, metadata={"providers": 0, "status": "agent_not_available"})

    def _run_retrospect_settlement(self, cfg: dict):
        """#112: recommendation_retrospect 结算通电 (建了没通电收口).

        每晚 fwd_return_backfill 结出 fwd 数据后: 新推荐建档案 (trace 快照)
        + 未结算行补缺失 horizon。幂等: PK INSERT OR IGNORE + UPDATE 仅
        fwd IS NULL 行, 重跑零副作用。hit 口径 = AG-01 canonical
        (agent_layer.quality.evaluator.is_hit_ag01 — 结算核心与 backfill 均 import).
        """
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if not (hasattr(app, "data") and app.data):
            return HandlerResult(success=True, metadata={"status": "no_db"})
        db = app.data.db
        from report_layer.retrospect_settlement import settle_retrospect
        stats = settle_retrospect(db)
        logger.info("Retrospect settlement: %s", stats)
        return HandlerResult(success=True, metadata=stats)

    def _run_agent_evaluate(self, cfg: dict):
        # BT-09 (AG-01 闭环): 回测层 AgentEvaluator 生产真实前向收益
        # (factor_daily.fwd_return_5d, 百分数→小数), ops 消费后喂给
        # Agent 层 Evaluator 回写 actual_return/hit。
        # 原实现恒传空 dict → 所有预测 actual=0, 观望全被误判命中 — 假闭环。
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if not (hasattr(app, "agent") and app.agent):
            return HandlerResult(success=True, metadata={"status": "no_evaluator"})
        db = app.data.db if hasattr(app, "data") and app.data else None
        if db is None:
            return HandlerResult(success=True, metadata={"status": "no_db"})

        from backtest_layer.evaluation.agent_eval import AgentEvaluator
        bt_eval = AgentEvaluator(db)
        date = cfg.get("date") or bt_eval.find_evaluable_date()
        if not date:
            logger.info("Agent evaluate: 无可评估日期 (前向窗口未到期或已全部结算)")
            return HandlerResult(success=True,
                                 metadata={"status": "no_evaluable_date"})

        bt_result = bt_eval.evaluate(date)
        from agent_layer.quality.evaluator import Evaluator
        ev = Evaluator(database=db)
        result = ev.evaluate(date, bt_result.forward_returns)
        # memory_layer Phase 1: 评估结果回流为 agent:evaluator feedback 记忆
        # (诚实降级: 写入失败只 warning, 不阻断管道)
        try:
            from memory_layer.feedback import record_evaluate_feedback
            record_evaluate_feedback(db, date=date, meta={
                "evaluated": result.get("evaluated", 0),
                "hits": result.get("hits", 0),
                "bt_hit_rate": bt_result.hit_rate,
            })
        except Exception as e:
            logger.warning(f"memory_layer 回流失败 (诚实降级): {e}")
        logger.info(
            f"Agent evaluate {date}: evaluator={result}, "
            f"bt_eval hit_rate={bt_result.hit_rate} "
            f"({bt_result.evaluated}/{bt_result.total_predictions} evaluated, "
            f"{bt_result.unevaluated} unevaluated)")
        return HandlerResult(success=True, metadata={
            "date": date,
            "evaluated": result.get("evaluated", 0),
            "hits": result.get("hits", 0),
            "bt_hit_rate": bt_result.hit_rate,
            "bt_unevaluated": bt_result.unevaluated,
        })

    def _run_agent_discovery(self, cfg: dict):
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if hasattr(app, "agent") and app.agent:
            health = app.agent.get_provider_health()
            n = getattr(health, 'providers_online', 0)
            logger.info(f"Agent discovery: {n} providers online")
            return HandlerResult(success=True, metadata={"providers_online": n})
        logger.warning("Agent discovery: agent layer not available")
        return HandlerResult(success=True, metadata={"providers_online": 0, "status": "agent_not_available"})

    def _run_agent_cleanup(self, cfg: dict):
        conn = self.db.get_connection()
        cleanup_days = cfg.get("agent.cleanup_days", 30) if cfg else 30
        conn.execute("DELETE FROM agent_memory WHERE created_at < datetime('now', ? || ' days', '+8 hours')", (f"-{cleanup_days}",))
        n = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        logger.info(f"Agent cleanup: deleted {n} old memory records (>{cleanup_days} days)")
        return HandlerResult(success=True, metadata={"deleted": n, "cleanup_days": cleanup_days})

    def _run_chart_analysis(self, cfg: dict):
        """#101 LLM读图 (切2): 候选集双录 chart_analysis (atr_floor 基线 + llm_chart)。

        作用域 §4: signal_provenance 当日 ∪ stock_pool 未出场 (~10-20股/日);
        排程由统筹在变更窗挂信号生成段之后 (本卡注册-only, 不动 crontab);
        LLM 通道走 AG-10 get_llm_service; 09 只写 chart_analysis —
        永不写 signal/pending_entry (§1.5 唯一写者=02), LLM 永不进打分公式。
        """
        from datetime import datetime

        from bootstrap import bootstrap_app

        app = bootstrap_app()
        db = app.data.db if hasattr(app, "data") and app.data else self.db
        service = None
        if (hasattr(app, "agent") and app.agent
                and hasattr(app.agent, "get_llm_service")):
            service = app.agent.get_llm_service()
        from agent_layer.chart_reader.leg_b import ChartLLMAnalyzer
        date = (cfg or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
        result = ChartLLMAnalyzer(db, llm_service=service).analyze(date)
        logger.info(f"Chart analysis {date}: {result}")
        return HandlerResult(success=True, metadata=result)


    def _settle_benchmark_leg(self, date: str) -> dict:
        """EX-1: benchmark 日终结算腿 — paper_trading_stats.benchmark_return 补写。

        病根 (2026-09-10 取证): 16:35 reconcile 路径生产 13 天零落地 (全 NULL),
        改在 18:05 窗补写 — index_daily 当日行 15:47~15:59 ddp 子管道已写,
        17:40 kline 兜底再加一层。UPDATE 当日行, 行不存在 (16:35 锁堵日) 则
        INSERT 兜底; backfill_days 顺带清历史 NULL 欠账。
        降级不抛错: 指数缺数/读失败只留痕 reason, 绝不拖垮因子终算主路径
        (#112 R-1 同判例 — 观测面增强腿非关键路径)。
        """
        try:
            # EX-3: settle_daily 双腿 (benchmark_return 补写 + 持仓快照落表)
            from backtest_layer.paper_trading.settlement import settle_daily
            data_layer = None
            config = None
            if self._app is not None:
                data_layer = getattr(self._app, "data", None)
                config = getattr(data_layer, "config", None) if data_layer else None
            else:
                try:
                    from bootstrap import bootstrap_app
                    app = bootstrap_app()
                    data_layer = getattr(app, "data", None)
                    config = getattr(data_layer, "config", None) if data_layer else None
                except Exception as e:
                    logger.warning(f"benchmark 结算腿: bootstrap 失败 ({e})")
                    return {"written": False, "reason": "bootstrap_failed"}
            return settle_daily(
                self.db, data_layer, date, config=config,
                backfill_days=int(self._cfg_all("backtest.paper_trading.benchmark_backfill_days", 5, None) or 5) if hasattr(self, "_cfg_all") else 5)
        except Exception as e:
            logger.warning(f"benchmark 结算腿失败 (不阻塞主路径): {e}")
            return {"written": False, "reason": "leg_failed", "detail": str(e)[:80]}

    def _run_fx_settlement(self, cfg: dict):
        """KLINE-FX 终算哨: 17:40 kline 兜底后, 若当日因子面 < K线面则补算。

        2026-09-07 首证 (KLINE-FX 链断): baostock 日K 17:30 后才入库, 主源
        15:30 部分日 (09-07: 523/5548 只) → 16:00 factor_update 在毒面上跑,
        #122 宁缺毋假只写 523 只因子 → fwd_return_backfill 16:10 同毒 →
        17:40 kline 补满 5549 但无 factor 重跑排程 → factor_daily/model_predictions
        全日停在 9.4%。本哨在 17:40 后条件补跑 factor_update + fwd_return_backfill,
        守卫短路保证正常日 (主源已满) 零开销。

        重跑安全性 (18:42/19:46 两次手工补跑实证): factor_daily 走
        INSERT OR IGNORE + UPDATE, fwd 走 only-NULL UPDATE — 幂等零副作用。
        执行层涟漪 (18:46 腿: 全量面重选 → 毒面票 exit_pending + 全量面新票挂入)
        与正常日 16:01 腿行为同型 (T+1 卖是系统常态, 09-03 建仓 7 票 09-04 全卖
        同型); paper_trading 16:35 已收走当日消费, 晚腿新挂票次日 16:35 消费。
        """
        from common.utils import bj_date
        date = (cfg or {}).get("date") or bj_date()
        conn = self.db.get_connection()
        n_kline = conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (date,)
        ).fetchone()[0]
        n_factor = conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date = ?", (date,)
        ).fetchone()[0]
        if n_kline == 0:
            logger.info("fx_settlement: 当日无K线, 短路 (kline 兜底也未得数据)")
            bench = self._settle_benchmark_leg(date)
            return HandlerResult(success=True, metadata={
                "skipped": True, "reason": "no_klines", "kline_rows": 0,
                "benchmark": bench})
        if n_factor >= n_kline:
            logger.info(
                f"fx_settlement: 因子面已满 ({n_factor}/{n_kline}), 短路")
            bench = self._settle_benchmark_leg(date)
            return HandlerResult(success=True, metadata={
                "skipped": True, "reason": "factor_face_complete",
                "factor_rows": n_factor, "kline_rows": n_kline,
                "benchmark": bench})
        # 缺口存在 (部分面日): 复跑两 handler — 与 16:00/16:10 首跑同一代码路径。
        logger.warning(
            f"fx_settlement: 检出因子缺口 {n_factor}/{n_kline} "
            f"(主源部分面毒化), 补跑 factor_update + fwd_return_backfill")
        fx = self._run_factor_update(cfg)
        fwd = self._run_fwd_return_backfill(cfg)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date = ?", (date,)
        ).fetchone()[0]
        bench = self._settle_benchmark_leg(date)
        # 铁律 #12: 补跑后仍不满 → 不静默, partial 留痕 (DL-43 先例)
        return HandlerResult(
            success=bool(fx.success),
            errors=list(fx.errors) + list(fwd.errors),
            metadata={
                "repaired": True, "factor_before": n_factor, "factor_after": n_after,
                "kline_rows": n_kline,
                "fwd_filled_total": (fwd.metadata or {}).get("filled_total", 0),
                "partial_gap": max(0, n_kline - n_after),
                "partial": n_after < n_kline,
                "benchmark": bench})

    def _run_ic_feedback(self, cfg: dict):
        """IC 反馈闭环 (F3.2 断链2) — 真实 IC 计算 + 权重衰减更新 + 写 DB。

        用滞后交易日 (默认 T-5 个交易日, 保证 5 日前向收益已实现) 的
        factor_scores 与 fwd_return_5d 计算每因子 Spearman IC, 再经
        ICTuner.update_weights_with_decay (20%上限/3天确认/周一平滑) 更新
        权重并写入 factor_weights 表。替代旧的"单股 get_scores 打日志"假实现。

        BT-05 (D-16 双制共存): 本 handler 是策略层 ICTuner 日级调权的消费方;
        末尾另附回测层 ICFeedback 的"IC 评估报告" (只评估不调权), 喂评估环与周报。

        ST-31 可观测改造 (2026-08-24, F-37/F-38, 铁律 #12 可观测不可静默):
        - target_date 按「交易日滞后」解析 (_resolve_ic_target_date); 旧实现
          按自然日减, 周末/节假日必落空, 曾连续 8 天静默跳过;
        - record_ic 显式传 calc_date=target_date; 旧实现内部 bj_date() 把
          T-5 数据算出的 IC 错标成运行日 (F-37);
        - skip 不再 success=True 静默成功 —— metadata["partial"]=True 走
          DL-43 三态 (pipeline_runs.status='partial' + warning 告警, 由
          run_pipeline 主线程发送并去重); error 返回 success=False 走 failed。
          选 partial 而非 failed: 全部注册管道无 depends_on:[ic_feedback],
          failed 触发的重试+熔断对"数据未就绪"是错误处置, partial 已有
          独立告警与落库路径 (DL-43 先例);
        - 专家权重健康检查: factor_weights 里 20 个专家标定因子 (DEFAULT_
          WEIGHTS) 权重合计 < MIN_EXPERT_WEIGHT_SUM 即降级告警 (本次事故
          直接探针: 实测已被稀释到 46.7%)。
        """
        import json as _json

        from strategy_layer.aggregation.aggregator import DEFAULT_WEIGHTS
        from strategy_layer.feedback.ic_tuner import ICTuner
        from strategy_layer.feedback.persistence import WeightPersistence

        # fwd_10d: IC 度量周期可配 (默认 5 = 行为不变)。列名走单一事实源,
        # 禁止复制字面量; lag < horizon 时自动抬到 horizon (否则截面全 NULL 白跑)。
        from data_layer._loader import FWD_HORIZON_COLS

        # #108: _cfg_get 双形态旁路退役 — 读点统一走 self._cfg_all
        # (Config 全键空间属主优先 + cfg 块双形态兜底, 先例
        # _dep_data_probe_enabled)。ic_horizon 生产值迁 local.yaml
        # strategy.ic_horizon (ops.yaml 管道块内嵌套撤除)。
        horizon = int(self._cfg_all("strategy.ic_horizon", 5, cfg))
        if horizon not in FWD_HORIZON_COLS:
            logger.warning(f"ic_feedback: unknown horizon {horizon}, fallback to 5")
            horizon = 5
        fwd_col = FWD_HORIZON_COLS[horizon]
        assert validate_identifier(fwd_col, "column"), f"bad fwd col: {fwd_col}"

        lag_days = int(self._cfg_all("strategy.ic_lag_days", horizon, cfg))
        if lag_days < horizon:
            logger.warning(
                f"ic_feedback: lag_days={lag_days} < horizon={horizon}, "
                f"raised to {horizon} (fwd_return 未兑现截面全空)")
            lag_days = horizon
        # ST-31 (F-38): target_date 按「交易日滞后」解析 (自然日减法在周末/节假日
        # 落非交易日 → factor_daily 无行 → IC 环静默跳过); 取代 fwd_10d 侧的
        # timedelta 自然日实现。
        target_date = _resolve_ic_target_date(self.db, lag_days)
        try:
            conn = self.db.get_connection()
            # 1. 取滞后交易日的 factor_scores + 前向收益 (列名经白名单校验后插值)
            rows = conn.execute(
                f"SELECT code, factor_scores, {fwd_col} FROM factor_daily "
                f"WHERE date = ? AND factor_scores IS NOT NULL AND {fwd_col} IS NOT NULL",
                (target_date,),
            ).fetchall()
            if len(rows) < 10:
                reason = f"insufficient rows for {target_date}: {len(rows)} (<10)"
                logger.warning(f"IC feedback skipped: {reason}")
                return HandlerResult(
                    success=True,
                    errors=[reason],
                    metadata={"date": target_date, "status": "skipped",
                              "reason": reason, "partial": True},
                )

            factor_scores_dict: dict[str, dict[str, float]] = {}
            forward_returns_dict: dict[str, float] = {}
            for code, fs_json, fwd in rows:
                try:
                    scores = _json.loads(fs_json) if isinstance(fs_json, str) else {}
                except (ValueError, TypeError):
                    scores = {}
                if not scores:
                    continue
                for fname, fval in scores.items():
                    if fval is None:
                        continue
                    factor_scores_dict.setdefault(fname, {})[code] = float(fval)
                forward_returns_dict[code] = float(fwd)

            if not factor_scores_dict or not forward_returns_dict:
                reason = f"no valid factor_scores/returns for {target_date} ({len(forward_returns_dict)} returns)"
                logger.warning(f"IC feedback skipped: {reason}")
                return HandlerResult(
                    success=True,
                    errors=[reason],
                    metadata={"date": target_date, "status": "skipped",
                              "reason": reason, "partial": True},
                )

            # 2. 计算 IC
            tuner = ICTuner()
            daily_ic = tuner.compute_ic(factor_scores_dict, forward_returns_dict)

            # 卡I (IC-by-lag 衰减哨兵, 统筹 sm-85ffae726bd041b0 ②):
            # compute_decay (BT-09) 零接线收口 — 用同一截面逐因子算
            # lag-N IC 衰减曲线写 factor_decay 表; 衰减红条 (half_life
            # 跌破阈值) 进 metadata 喂早报/周报点名。段内失败只 warning
            # 不杀主管道 (铁律#12: 可观测不可静默, 也不可喧宾夺主)。
            decay_alerts: list[dict] = []
            try:
                from backtest_layer.evaluation.factor_eval import FactorEvaluator

                ev = FactorEvaluator()
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factor_decay (
                        calc_date TEXT NOT NULL,
                        factor_name TEXT NOT NULL,
                        decay_curve TEXT,
                        half_life INTEGER,
                        created_at TEXT DEFAULT (datetime('now')),
                        PRIMARY KEY (calc_date, factor_name)
                    )
                    """
                )
                for fname, per_code in factor_scores_dict.items():
                    codes = sorted(per_code)
                    scores = [per_code[c] for c in codes]
                    rets = [forward_returns_dict[c] for c in codes]
                    decay_res = ev.compute_decay(fname, scores, rets, max_lag=10)
                    if not decay_res.decay_curve:
                        continue  # 空截面诚实跳过, 不写假行
                    conn.execute(
                        "INSERT OR REPLACE INTO factor_decay "
                        "(calc_date, factor_name, decay_curve, half_life) "
                        "VALUES (?, ?, ?, ?)",
                        (target_date, fname,
                         _json.dumps(decay_res.decay_curve), decay_res.half_life))
                    # 红条口径: lag-0 有效 (curve[0]>0) 但衰减过快
                    # (half_life 达标前 IC 已腰斩且 <=2 期) — 稳定因子
                    # (慢衰减) 不进。
                    if decay_res.half_life in (1, 2):
                        decay_alerts.append({
                            "factor": fname,
                            "half_life": decay_res.half_life,
                            "lag0_ic": decay_res.decay_curve[0],
                        })
                conn.commit()
                if decay_alerts:
                    logger.warning(
                        f"ic_feedback 衰减红条: {len(decay_alerts)} 个因子 "
                        f"半衰期过短 (date={target_date}): "
                        + ", ".join(a["factor"] for a in decay_alerts))
            except Exception as decay_exc:
                logger.warning(
                    f"ic_feedback: 衰减哨兵段失败 (主管道不受影响): {decay_exc}")

            # 卡#57 (因子分布漂移哨兵, b 路线零依赖 — 统筹
            # sm-832ad940cd864f31): sklearn 原生 PSI/KS/卡方/
            # Wasserstein 四件, 与卡I factor_decay 同域互补
            # (decay=时滞衰减, drift=分布漂移)。开关默认关 —
            # enabled=False 时整段跳过, 零现产行为改变; 通电=
            # 纯配置翻转 (signal_provenance 先例)。
            # triggered=OR 聚合 (统筹授权: 四件量纲不同不加权);
            # 样本不足 triggered=None 诚实跳过不写假行。
            drift_alerts: list[dict] = []
            drift_enabled = bool(self._cfg_all("factor_drift.enabled", False, cfg))
            if drift_enabled:
                try:
                    from backtest_layer.evaluation.factor_drift import compute_drift

                    ref_window = max(int(self._cfg_all(
                        "factor_drift.reference_window", 10, cfg)), 2)
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS factor_drift_metrics (
                            calc_date TEXT NOT NULL,
                            factor_name TEXT NOT NULL,
                            psi REAL,
                            ks_pvalue REAL,
                            chi2_pvalue REAL,
                            wasserstein_norm REAL,
                            triggered INTEGER,
                            created_at TEXT DEFAULT (datetime('now')),
                            PRIMARY KEY (calc_date, factor_name)
                        )
                        """
                    )
                    # reference = 滞后窗口 (target_date 前的最近
                    # ref_window 个有截面日) 该因子全体截面值;
                    # current = target_date 当日截面。
                    ref_dates = [r[0] for r in conn.execute(
                        "SELECT DISTINCT date FROM factor_daily "
                        "WHERE date < ? AND factor_scores IS NOT NULL "
                        "ORDER BY date DESC LIMIT ?",
                        (target_date, ref_window)).fetchall()]
                    # AUD-021: 参考截面一次装载分桶 (原实现逐因子重复查同一批参考日,
                    # 89 因子 x 10 参考日 x 5500 行 = 约 490 万次 JSON 解析)。
                    ref_by_factor = _load_ref_factor_values(conn, ref_dates)
                    for fname, per_code in factor_scores_dict.items():
                        codes = sorted(per_code)
                        current_vals = [per_code[c] for c in codes]
                        ref_vals = ref_by_factor.get(fname, [])
                        d_res = compute_drift(fname, ref_vals, current_vals)
                        if d_res.triggered is None:
                            continue  # 样本不足诚实跳过, 不写假行
                        conn.execute(
                            "INSERT OR REPLACE INTO factor_drift_metrics "
                            "(calc_date, factor_name, psi, ks_pvalue, "
                            "chi2_pvalue, wasserstein_norm, triggered) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (target_date, fname, d_res.psi, d_res.ks_pvalue,
                             d_res.chi2_pvalue, d_res.wasserstein_norm,
                             1 if d_res.triggered else 0))
                        if d_res.triggered:
                            drift_alerts.append({
                                "factor": fname,
                                "psi": d_res.psi,
                                "wasserstein_norm": d_res.wasserstein_norm,
                            })
                    conn.commit()
                    if drift_alerts:
                        logger.warning(
                            f"ic_feedback 漂移红条: {len(drift_alerts)} 个因子 "
                            f"分布漂移 (date={target_date}): "
                            + ", ".join(a["factor"] for a in drift_alerts))
                except Exception as drift_exc:
                    logger.warning(
                        f"ic_feedback: 漂移哨兵段失败 (主管道不受影响): {drift_exc}")

            # 3. 载入当前权重 + IC 历史, 衰减更新, 写 DB
            wp = WeightPersistence(config=cfg, db=self.db)
            current_weights = wp.load_weights(default_weights=None)

            # ST-31 (F-38): 专家权重健康探针 (先查后写)。首跑无权重表时
            # 走下方 default_seed, 不适用本检查。
            expert_weight_sum: float | None = None
            degraded_reasons: list[str] = []
            if current_weights:
                expert_present = [f for f in DEFAULT_WEIGHTS if f in current_weights]
                expert_weight_sum = sum(float(current_weights[f]) for f in expert_present)
                # AUD-011: 判据 = 专家因子权重和 / 其均匀份额 (expert_present/总因子数)。
                # 裸和口径随因子总数漂移 (20 因子时代 0.9 合理, 89 因子时代恒假降级);
                # 比值口径与因子总数无关 —— 20 因子表份额=1.0 时比值==裸和, 旧语义兼容。
                _uniform_share = (len(expert_present) / len(current_weights)
                                  if current_weights else 1.0)
                expert_share_ratio = (expert_weight_sum / _uniform_share
                                      if _uniform_share > 0 else 0.0)
                if expert_share_ratio < MIN_EXPERT_SHARE_RATIO:
                    # 文案保留 "expert weight sum" 子串: ST-31 既有测试与告警
                    # 查询 (detail LIKE '%expert weight sum%') 按此子串锚定。
                    msg = (f"expert weight sum {expert_weight_sum:.3f} — "
                           f"share ratio {expert_share_ratio:.3f} < {MIN_EXPERT_SHARE_RATIO} "
                           f"(uniform share={_uniform_share:.4f}, "
                           f"{len(expert_present)}/{len(DEFAULT_WEIGHTS)} expert factors "
                           f"in {len(current_weights)}-factor universe)")
                    logger.warning(f"IC feedback degraded: {msg}")
                    degraded_reasons.append(msg)

            # F-37: IC 由 target_date 的数据算出, calc_date 必须同日标注
            wp.record_ic(daily_ic, sample_size=len(forward_returns_dict),
                         calc_date=target_date)

            # 记忆层案A② 喂料面通电: IC 转负因子写 factor:<name> lesson
            # (evidence 必填三铁律; 失败只 warning 不阻断主管道)。
            try:
                from memory_layer.wire import record_ic_lessons
                _n_mem = record_ic_lessons(
                    self.db, daily_ic, sample_size=len(forward_returns_dict),
                    calc_date=target_date)
                if _n_mem:
                    logger.info(
                        f"ic_feedback: 记忆层写入 {_n_mem} 条 factor lesson "
                        f"(IC 转负, date={target_date})")
            except Exception as _mem_exc:
                logger.warning(
                    f"ic_feedback: 记忆层写入失败 (诚实降级): {_mem_exc}")

            # Q4 IC 质量闸门守卫: 近 30 日真 IC 证据 (wp.ic_history, 含当日
            # record_ic 累计) < MIN_IC_EVIDENCE 时 ic_decay 直接 no-op 不稀释,
            # 保持现权重 + warning 留痕。record_ic 照常积累证据。
            # 语义区分: 这是「伤害被预防」的保护性 no-op (证据积累中), 不是
            # 「伤害已发生」的 degraded — 故不 push degraded_reasons / 不翻
            # status, 只 logger.warning + metadata 留痕 (铁律#12 可观测)。
            ic_evidence_count = sum(len(v) for v in wp.ic_history.values())
            ic_evidence_guard = False
            if current_weights and ic_evidence_count < MIN_IC_EVIDENCE:
                ic_evidence_guard = True
                logger.warning(
                    f"IC feedback guard: IC evidence {ic_evidence_count} < "
                    f"{MIN_IC_EVIDENCE} (近30日真IC证据不足) → ic_decay no-op, "
                    f"保持现权重不稀释 (证据积累中, date={target_date})")
                n_updated = 0
            elif current_weights:
                is_monday = datetime.strptime(target_date, "%Y-%m-%d").weekday() == 0
                new_weights = tuner.update_weights_with_decay(
                    current_weights, wp.ic_history,
                    weight_history=wp.weight_history, is_monday=is_monday,
                )
                wp.record_weights(new_weights)
                wp.save_weights(new_weights)
                n_updated = len(new_weights)
                logger.info(f"IC feedback: computed IC for {len(daily_ic)} factors, updated weights for {n_updated} (date={target_date})")
            else:
                wp.save_weights(DEFAULT_WEIGHTS.copy(), method="default_seed")
                n_updated = 0
                logger.info(f"IC feedback: no prior weights, seeded DEFAULT_WEIGHTS (date={target_date})")

            top_ic = sorted(daily_ic.items(), key=lambda x: -abs(x[1]))[:5]

            # BT-05 (D-16 双制共存): 策略层 ICTuner 已调权 (上面); 回测层 ICFeedback
            # 只评估不调权 — 从 factor_ic 读近 30 日 IC 序列产出因子有效性报告,
            # 喂评估环与周报。两套职责不同, 都给真活。
            from backtest_layer.feedback.ic_feedback import build_ic_report_from_db
            ic_report = build_ic_report_from_db(self.db, lookback_days=30)

            meta = {
                "date": target_date,
                "n_stocks": len(forward_returns_dict),
                "n_factors": len(daily_ic),
                "n_weights_updated": n_updated,
                "top_ic": {k: round(v, 4) for k, v in top_ic},
                "decay_alerts": decay_alerts,
                "drift_alerts": drift_alerts,
                "ic_eval_report": {
                    "n_factors": ic_report.n_factors,
                    "strong": ic_report.strong,
                    "weak_or_invalid": ic_report.weak_or_invalid,
                },
                "expert_weight_sum": (
                    round(expert_weight_sum, 4)
                    if expert_weight_sum is not None else None
                ),
                "ic_evidence_count": ic_evidence_count,
                "ic_evidence_guard": ic_evidence_guard,
                "status": "completed",
            }
            if degraded_reasons:
                # 权重已稀释但本轮工作照常完成 (数据已落库但降级) → DL-43 partial
                meta["partial"] = True
                meta["errors"] = degraded_reasons
                meta["status"] = "degraded"
            return HandlerResult(success=True, metadata=meta)
        except Exception as e:
            logger.warning(f"IC feedback failed: {e}", exc_info=True)
            return HandlerResult(
                success=False,
                errors=[f"ic_feedback failed: {e}"],
                metadata={"date": target_date, "status": "error", "detail": str(e)},
            )

    def _run_wfo_gate(self, cfg: dict):
        """WFO 门禁 — 调用 WFOGate.evaluate() 全五项判定 (Sharpe/MDD/trades/超额/退化)。

        使用 ≥60 日窗口 (默认 120 日) 的 walk_forward 结果，聚合 IS/OOS 指标，
        替代旧的单日窗口 oos_eff>0 假门禁。
        """
        from datetime import datetime, timedelta

        from backtest_layer.core.models import BacktestInput, CostConfig
        from backtest_layer.feedback.wfo_gate import WFOGate
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        try:
            # ≥60 日窗口: 默认回溯 120 天
            lookback_days = max(60, int(self._cfg_all("backtest.wfo_lookback_days", 120, cfg)))
            start_dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback_days)
            start_date = start_dt.strftime("%Y-%m-%d")
            bt_input = BacktestInput(
                mode="walk_forward", codes=self._cfg_all("backtest.probe_codes", ["000001", "600519"], cfg),
                date_range=(start_date, date),
                cost=CostConfig(initial_capital=self._cfg_all("execution.initial_capital", 300000, cfg)),
            )
            result = app.backtest.run(bt_input)
            wf = getattr(result, "wf_result", None) or {}
            if wf.get("error"):
                logger.info(f"WFO gate: backtest error — {wf['error']}")
                return HandlerResult(success=True, metadata={"date": date, "status": "error", "detail": wf["error"]})

            # 从 walk_forward 聚合结果构造 IS/OOS 报告供 WFOGate 五项判定
            is_report = {
                "sharpe": wf.get("is_sharpe", 0.0),
                "max_drawdown": 0.0,
                "total_trades": 0,
                "excess_return": 0.0,
            }
            oos_report = {
                "sharpe": wf.get("oos_sharpe", 0.0),
                "max_drawdown": wf.get("oos_max_drawdown", 0.0),
                "total_trades": wf.get("oos_total_trades", 0),
                "excess_return": wf.get("oos_excess_return", 0.0),
            }
            gate = WFOGate()
            # BT-11 ⑤ (B-13): 效率比口径统一 — 透传 walk_forward 的 OOS/IS 收益比
            # (唯一口径), gate 不再自算夏普比效率; 缺失时为 None, 不拿 0 冒充
            oos_eff = wf.get("oos_efficiency")
            evaluation = gate.evaluate(is_report, oos_report, oos_efficiency=oos_eff)
            # BT-05 (D-17) 三档动作: 门禁判了门, 门后面要有路。
            # deploy→active_factor_weights / paper_trading→paper_trading_weights / hold→维持+告警
            tier = gate.tier(evaluation)
            action = self._apply_wfo_tier(tier, date, cfg)
            status = "passed" if evaluation.passed else "needs_review"
            logger.info(
                f"WFO gate: {status} tier={tier} action={action} "
                f"(OOS Sharpe={evaluation.oos_sharpe:.2f}, "
                f"MDD={evaluation.max_drawdown:.1%}, trades={evaluation.total_trades}, "
                f"degradation={evaluation.degradation:.0%}, "
                f"OOS_eff={oos_eff if oos_eff is not None else 'n/a'})"
                + (f" — {evaluation.reason}" if evaluation.reason else "")
            )
            return HandlerResult(success=True, metadata={
                "date": date,
                "window": f"{start_date}~{date}",
                "oos_efficiency": oos_eff,
                "oos_sharpe": evaluation.oos_sharpe,
                "is_sharpe": evaluation.is_sharpe,
                "max_drawdown": evaluation.max_drawdown,
                "total_trades": evaluation.total_trades,
                "degradation": evaluation.degradation,
                "status": status,
                "tier": tier,
                "action": action,
                "reason": evaluation.reason,
            })
        except Exception as e:
            logger.info(f"WFO gate: {e}")
            return HandlerResult(success=True, metadata={"date": date, "status": "error", "detail": str(e)})

    def _apply_wfo_tier(self, tier: str, date: str, cfg: dict) -> str:
        """BT-05 (D-17) WFO 三档动作执行。

        - deploy: 新权重写 active_factor_weights。自动部署档**建好但 config 开关
          默认关** (backtest.wfo_auto_deploy.enabled), record-only 验证期后放开 —
          对齐 IC 环三段式哲学 ("建好"与"放开"是两回事)。开关关闭时只记录不部署。
        - paper_trading: 权重写 paper_trading_weights (BT-12 纸交易消费)。
        - hold: 维持现有权重 + AlertManager 告警。

        权重来源: factor_weights 最新一日 (策略层 ICTuner 日级调权的产出)。
        写库均 config 门控 (默认关, 建表报用户 GO)。
        """
        def _latest_weights() -> dict:
            conn = self.db.get_connection()
            row = conn.execute("SELECT MAX(weight_date) FROM factor_weights").fetchone()
            latest = row[0] if row else None
            if not latest:
                return {}
            rows = conn.execute(
                "SELECT factor_name, weight FROM factor_weights WHERE weight_date = ?",
                (latest,),
            ).fetchall()
            return {r[0]: r[1] for r in rows if r[0] and r[1] is not None}

        try:
            if tier == "deploy":
                auto_deploy = bool(self._cfg_all("backtest.wfo_auto_deploy.enabled", False, cfg))
                weights = _latest_weights()
                if not weights:
                    return "deploy_skipped_no_weights"
                if not auto_deploy:
                    # record-only: 记录建议部署的权重, 不写 active 表
                    logger.info(f"WFO deploy tier (record-only, 开关关闭): {len(weights)} 权重待部署")
                    return "deploy_record_only"
                conn = self.db.get_connection()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS active_factor_weights ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "deploy_date TEXT NOT NULL, factor_name TEXT NOT NULL, "
                    "weight REAL NOT NULL, source TEXT DEFAULT 'wfo_deploy', "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                    "UNIQUE(deploy_date, factor_name))",
                )
                for factor, w in weights.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO active_factor_weights "
                        "(deploy_date, factor_name, weight) VALUES (?, ?, ?)",
                        (date, factor, float(w)),
                    )
                conn.commit()
                logger.info(f"WFO deploy: {len(weights)} 权重写入 active_factor_weights ({date})")
                return "deployed"
            if tier == "paper_trading":
                persist = bool(self._cfg_all("backtest.wfo_paper_persist.enabled", False, cfg))
                weights = _latest_weights()
                if not weights:
                    return "paper_skipped_no_weights"
                if not persist:
                    logger.info(f"WFO paper_trading tier (record-only, 开关关闭): {len(weights)} 权重待纸交易")
                    return "paper_record_only"
                conn = self.db.get_connection()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS paper_trading_weights ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "assign_date TEXT NOT NULL, factor_name TEXT NOT NULL, "
                    "weight REAL NOT NULL, source TEXT DEFAULT 'wfo_paper', "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                    "UNIQUE(assign_date, factor_name))",
                )
                for factor, w in weights.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO paper_trading_weights "
                        "(assign_date, factor_name, weight) VALUES (?, ?, ?)",
                        (date, factor, float(w)),
                    )
                conn.commit()
                logger.info(f"WFO paper_trading: {len(weights)} 权重写入 paper_trading_weights ({date})")
                return "paper_assigned"
            # hold: 维持现有权重 + 告警
            # G5 (2026-10-23): 措辞按性质分三态 —— 「未评估」不得说成「低于 0.5」。
            if tier == "hold_unevaluated":
                _title = "WFO 未评估, 维持现有权重"
                _detail = (f"WFO 从未产生 oos_efficiency（walk_forward 无生产调度），"
                           f"并非「Efficiency 低于 0.5」。门禁保守维持 hold，"
                           f"建议先让 walk_forward 跑起来再评估策略 (date={date})")
            else:
                _title = "WFO 门禁未通过, 维持现有权重"
                _detail = (f"WFO Efficiency 低于 0.5（已实际评估），"
                           f"策略可能过拟合，建议人工介入 (date={date})")
            try:
                self.alert_manager.create_alert(
                    title=_title,
                    detail=_detail,
                    severity="warning", layer="backtest",
                )
            except Exception as ae:
                logger.warning(f"WFO hold 告警失败: {ae}")
            logger.info(f"WFO hold tier: 维持现有权重 + 告警 ({date})")
            return "hold_alerted"
        except Exception as e:
            logger.warning(f"WFO tier 动作执行失败: {e}", exc_info=True)
            return f"tier_action_error: {e}"

    def _run_param_tuning(self, cfg: dict):
        """BT-05 (2026-08-18) 拆假: 真参数调优。

        原实现只打一行 portfolio snapshot 日志就返回 success — 设计 §10.2 的
        规则引擎从未运行。现:
        ① 规则引擎 (AdaptiveParamTuner): 读 post_exit_tracking 真实推荐表现
           (胜率/盈亏比) + daily_regime (BT-08 未就绪→None 跳过) → 参数建议;
        ② 可选搜索调参 (GridSearch): backtest.param_search.enabled=true 时
           对 probe_codes 真跑网格搜索 (需数据+引擎, 贵, 默认关);
        ③ 写 strategy_params 表: config backtest.param_persistence.enabled 门控
           (默认关, 建表需用户 GO — 见 04-回测层审计 BT-05)。
        """
        from datetime import datetime, timedelta

        from backtest_layer.feedback.param_rules import (
            AdaptiveParamTuner, read_recommendation_performance, read_regime,
        )
        from common.utils import bj_date
        date = bj_date()
        try:
            # ① 规则引擎: 真实推荐表现 → 参数建议
            lookback = int(self._cfg_all("param.lookback_days", 30, cfg))
            perf = read_recommendation_performance(self.db, lookback_days=lookback)
            regime = read_regime(self.db, date)
            tuner = AdaptiveParamTuner(
                base_buy_threshold=int(self._cfg_all("param.base_buy_threshold", 65, cfg)),
                base_stop_loss_pct=float(self._cfg_all("param.base_stop_loss_pct", -0.08, cfg)),
            )
            suggestion = tuner.suggest(perf, regime)

            # ② 可选搜索调参 (默认关): GridSearch 真回测网格搜索
            search_result = None
            if self._cfg_all("backtest.param_search.enabled", False, cfg):
                from backtest_layer.core.engine import BacktestEngine
                from backtest_layer.core.models import BacktestInput, CostConfig
                from backtest_layer.feedback.param_tuner import GridSearch
                from bootstrap import bootstrap_app
                app = bootstrap_app()
                search_lookback = max(30, int(self._cfg_all("backtest.param_search.lookback_days", 90, cfg)))
                start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=search_lookback)).strftime("%Y-%m-%d")
                base_input = BacktestInput(
                    mode="hold", codes=self._cfg_all("backtest.probe_codes", ["000001", "600519"], cfg),
                    date_range=(start, date),
                    cost=CostConfig(initial_capital=self._cfg_all("execution.initial_capital", 300000, cfg)),
                )
                gs = GridSearch(backtest_engine=BacktestEngine(),
                                data_layer=app.data, strategy_layer=app.strategy)
                grid = self._cfg_all(
                    "backtest.param_search.grid",
                    {"commission_rate": [0.0002, 0.0003], "slippage_rate": [0.001, 0.002]}, cfg)
                opt = gs.search(grid, base_input)
                search_result = {"method": opt.method, "best_params": opt.best_params,
                                 "best_sharpe": opt.best_sharpe, "n_evaluations": opt.n_evaluations}

            # ③ 写 strategy_params (config 门控, 默认关; 建表报用户 GO)
            # #108/#77: 门读法 Config 属主 — local.yaml 09-01 已落
            # backtest.param_persistence.enabled, 但 flat 点键从管道块恒
            # MISS → 恒 False (开关是死的); 09-06 首跑必现 persisted:False。
            persisted = self._persist_strategy_params(date, suggestion, search_result) if (
                self._cfg_all("backtest.param_persistence.enabled", False, cfg)) else False

            meta = {
                "date": date,
                "status": "completed",
                "suggestion": suggestion.to_dict(),
                "search": search_result,
                "persisted": persisted,
            }
            logger.info(f"Param tuning: {meta}")
            return HandlerResult(success=True, metadata=meta)
        except Exception as e:
            logger.warning(f"Param tuning failed: {e}", exc_info=True)
            return HandlerResult(success=True, metadata={"date": date, "status": "error", "detail": str(e)})

    def _persist_strategy_params(self, date: str, suggestion, search_result) -> bool:
        """BT-05: 参数建议写 strategy_params 表 (建表报用户 GO, config 门控)。"""
        import json as _json
        try:
            conn = self.db.get_connection()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS strategy_params ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "param_date TEXT NOT NULL, "
                "buy_threshold INTEGER, stop_loss_pct REAL, "
                "regime TEXT, adjustments TEXT, "
                "search_params TEXT, search_sharpe REAL, "
                "source TEXT DEFAULT 'rule_engine', "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "UNIQUE(param_date, source))",
            )
            conn.execute(
                "INSERT OR REPLACE INTO strategy_params "
                "(param_date, buy_threshold, stop_loss_pct, regime, adjustments, "
                " search_params, search_sharpe, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'rule_engine')",
                (date, suggestion.buy_threshold, suggestion.stop_loss_pct,
                 suggestion.regime, _json.dumps(suggestion.adjustments, ensure_ascii=False),
                 _json.dumps(search_result["best_params"]) if search_result else None,
                 search_result["best_sharpe"] if search_result else None),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.warning(f"strategy_params 写入失败 (不影响调参结果): {e}")
            return False

    def _run_end_of_day(self, cfg: dict):
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if hasattr(app, "execution") and app.execution:
            snapshot = app.execution.get_portfolio_snapshot()
            nav = getattr(snapshot, "portfolio_nav", "N/A")
            logger.info(f"End of day: NAV={nav}")
            return HandlerResult(success=True, metadata={"nav": str(nav)})
        return HandlerResult(success=True, metadata={"status": "execution_not_available"})

    def _run_post_market(self, cfg: dict):
        from common.utils import bj_date
        bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        if hasattr(app, "execution") and app.execution:
            snapshot = app.execution.get_portfolio_snapshot()
            nav = getattr(snapshot, "portfolio_nav", "N/A")
            logger.info(f"Post-market: NAV={nav}")
            return HandlerResult(success=True, metadata={"nav": str(nav)})
        return HandlerResult(success=True, metadata={"status": "execution_not_available"})

    def _run_model_retrain(self, cfg: dict):
        LABEL_COLUMN = "fwd_return_20d"

        import json as _json

        import numpy as np

        from bootstrap import bootstrap_app
        from common.utils import bj_date
        from strategy_layer.ml.lgb_scorer import LightGBMScorer
        from strategy_layer.ml.registry import ModelRegistry
        from strategy_layer.ml.ridge_scorer import RidgeScorer
        from strategy_layer.ml.xgb_scorer import XGBScorer

        bj_date()
        logger.info("Model retrain: training ML model on factor_daily data")

        try:
            app = bootstrap_app()
            data_layer = app.data

            # 1. Compute fwd returns via Data Layer
            # (#110) 数据腿预算参数化: 两腿不再受外层静态 30s 硬顶, 真源
            # ops.pipelines.model_retrain.data_leg_* (get_section 嵌套重建,
            # cfg 即 model_retrain 块; 缺键回落 30 = 存量行为零变)。
            compute_budget = int(cfg.get("data_leg_compute_timeout_s", 30) or 30)
            training_budget = int(cfg.get("data_leg_training_timeout_s", 30) or 30)
            data_layer.compute_fwd_returns(LABEL_COLUMN, timeout_s=compute_budget)

            # 2. Load training data via Data Layer
            all_rows = data_layer.get_training_dataset(
                LABEL_COLUMN, timeout_s=training_budget
            )
            # W2-8 (2026-09-19): None 守卫 — get_training_dataset 超时腿静默返 None,
            # 直取 len() → TypeError 穿透 (生产实证 alert#86, 09-01/09-02 两连)。
            # 空集与不可得区分 (判例#6): None=腿异常/超时; 空列表=数据真空。
            if all_rows is None:
                logger.error("Model retrain: training dataset unavailable (None from data layer, likely timeout)")
                return HandlerResult(success=False, errors=["training dataset unavailable (None)"])
            if not all_rows:
                logger.warning("Model retrain: training dataset empty (0 rows)")
                return HandlerResult(success=False, errors=["training dataset empty (0 rows)"])
            # Detect format: 4 columns with JSON in [2] = factor_scores mode
            has_factor_scores = (
                len(all_rows) > 0
                and len(all_rows[0]) == 4
                and isinstance(all_rows[0][2], str)
                and all_rows[0][2].startswith("{")
            )
            if has_factor_scores:
                logger.info("Using factor_scores JSON column (aligned with prediction pipeline)")
            else:
                logger.info("Using raw feature columns (fallback mode)")
            FALLBACK_COLUMNS = [
                "pe_ttm", "pb", "roe", "gross_margin", "net_margin",
                "rev_growth", "profit_growth", "momentum_5d",
                "volatility_20d", "volume_ratio", "macd_hist", "rsi_14",
                "pe_rank", "pb_rank", "roe_rank", "momentum_rank", "macd_rank",
                "total_mv", "circ_mv", "concept_count",
            ]

            if len(all_rows) < 200:
                logger.warning(f"Insufficient training data: {len(all_rows)} rows")
                return HandlerResult(success=False, errors=[f"insufficient training data: {len(all_rows)} rows"])

            # 3. Date-based split with embargo (BT-CORRECTNESS ⑥):
            # 标签 = 20 日前向收益 → 每个切分边界前丢弃 LABEL_HORIZON 个交易日 (embargo),
            # 三段 train / early-stop-val / holdout; 汇报的 val_ic = holdout 逐日截面 IC 均值,
            # early-stop 集不再冒充验证 IC。日期不足以切三段时退化为两段 (holdout 兼作
            # early-stop, metadata 标 holdout_used_for_early_stopping=True)。
            from strategy_layer.ml.splits import embargoed_date_split, mean_daily_ic
            LABEL_HORIZON = 20
            all_dates = sorted({r[0] for r in all_rows if r[0]})
            split = embargoed_date_split(all_dates, LABEL_HORIZON, train_frac=0.7, es_frac=0.15)
            holdout_used_for_es = False
            if not split.es_val or not split.holdout:
                split = embargoed_date_split(all_dates, LABEL_HORIZON, train_frac=0.8, es_frac=0.0)
                split.es_val = set(split.holdout)
                holdout_used_for_es = True
            train_rows = [r for r in all_rows if r[0] in split.train]
            es_rows = [r for r in all_rows if r[0] in split.es_val]
            val_rows = [r for r in all_rows if r[0] in split.holdout]
            logger.info(
                f"Embargoed split (h={LABEL_HORIZON}): train={len(train_rows)}/{len(split.train)} dates, "
                f"es={len(es_rows)}/{len(split.es_val)} dates, holdout={len(val_rows)}/{len(split.holdout)} dates, "
                f"embargo_dropped={len(split.dropped)} dates, holdout_used_for_es={holdout_used_for_es}")

            # 4. Build feature matrix (same space as prediction)
            # F3.3: 用真实因子名作为特征列 (非 f0..fN 占位), 使 regime_scorer 可加载
            def _collect_factor_names(rows):
                names = set()
                for row in rows:
                    fs_json = row[2]
                    try:
                        scores = _json.loads(fs_json) if isinstance(fs_json, str) else {}
                    except (ValueError, TypeError):
                        scores = {}
                    names.update(k for k, v in scores.items() if v is not None)
                return sorted(names)

            def build_dataset_factor_scores(rows, feature_names):
                X, y, dates, codes_out = [], [], [], []
                for row in rows:
                    date_val, code_val = row[0], row[1]
                    fs_json, label = row[2], row[3]
                    if label is None or (isinstance(label, float) and np.isnan(label)):
                        continue
                    try:
                        scores = _json.loads(fs_json) if isinstance(fs_json, str) else {}
                    except (ValueError, TypeError):
                        scores = {}
                    if not scores:
                        continue
                    features = []
                    for k in feature_names:
                        v = scores.get(k, 0.0)
                        features.append(0.0 if v is None else float(v))
                    if any(abs(v) > 100 for v in features):
                        continue
                    X.append(features)
                    y.append(label)
                    dates.append(date_val)
                    codes_out.append(code_val)
                return np.array(X, dtype=np.float64), np.array(y, dtype=np.float64), dates, codes_out

            def build_dataset_raw(rows, feat_cols):
                X, y, dates, codes_out = [], [], [], []
                n_feat = len(feat_cols)
                for row in rows:
                    date_val, code_val = row[0], row[1]
                    label = row[n_feat + 2]
                    if label is None or (isinstance(label, float) and np.isnan(label)):
                        continue
                    features = []
                    skip = False
                    for i in range(n_feat):
                        val = row[i + 2]
                        if val is None or (isinstance(val, float) and np.isnan(val)):
                            val = 0.0
                        features.append(val)
                    if any(abs(v) > 100 for v in features if v is not None):
                        skip = True
                    if not skip:
                        X.append(features)
                        y.append(label)
                        dates.append(date_val)
                        codes_out.append(code_val)
                return np.array(X, dtype=np.float64), np.array(y, dtype=np.float64), dates, codes_out

            if has_factor_scores:
                FEATURE_COLUMNS = _collect_factor_names(train_rows)
                X_train, y_train, _, _ = build_dataset_factor_scores(train_rows, FEATURE_COLUMNS)
                X_es, y_es, d_es, _ = build_dataset_factor_scores(es_rows, FEATURE_COLUMNS)
                X_val, y_val, d_val, _ = build_dataset_factor_scores(val_rows, FEATURE_COLUMNS)
            else:
                FEATURE_COLUMNS = list(FALLBACK_COLUMNS)
                X_train, y_train, _, _ = build_dataset_raw(train_rows, FALLBACK_COLUMNS)
                X_es, y_es, d_es, _ = build_dataset_raw(es_rows, FALLBACK_COLUMNS)
                X_val, y_val, d_val, _ = build_dataset_raw(val_rows, FALLBACK_COLUMNS)

            logger.info(f"After filtering: X_train={X_train.shape}, X_es={X_es.shape}, X_holdout={X_val.shape}")

            def _holdout_ic(scorer, X, y, dates_):
                if len(X) == 0:
                    return 0.0
                return mean_daily_ic(scorer.predict(X.tolist()), y, dates_)

            es_args = (X_es, y_es) if len(X_es) > 0 else (None, None)

            if len(X_train) < 100 or X_train.shape[1] == 0:
                logger.warning(f"Insufficient training data: {len(X_train)} rows, {X_train.shape[1]} features")
                return HandlerResult(success=False, errors=[f"insufficient training data: {len(X_train)} rows, {X_train.shape[1]} features"])

            # 维度兜底对齐 (正常情况 FEATURE_COLUMNS 长度 == X_train.shape[1])
            if len(FEATURE_COLUMNS) != X_train.shape[1]:
                logger.warning(f"Feature count mismatch: names={len(FEATURE_COLUMNS)} vs X={X_train.shape[1]}, truncating")
                FEATURE_COLUMNS = FEATURE_COLUMNS[:X_train.shape[1]]
            logger.info(f"Feature columns ({len(FEATURE_COLUMNS)}): {FEATURE_COLUMNS[:8]}...")

            from scipy.stats import spearmanr

            # 5. Train XGBoost (optimized hyperparameters)
            logger.info("Training XGBoost model...")
            xgb_scorer = XGBScorer()
            xgb_scorer.feature_names = FEATURE_COLUMNS
            xgb_scorer.train(X_train, y_train, *es_args)

            xgb_pred_train = xgb_scorer.predict(X_train.tolist())
            xgb_train_ic = spearmanr(xgb_pred_train, y_train)[0]
            xgb_val_ic = _holdout_ic(xgb_scorer, X_val, y_val, d_val)
            xgb_es_ic = _holdout_ic(xgb_scorer, X_es, y_es, d_es)

            xgb_importances = {}
            if hasattr(xgb_scorer.model, "feature_importances_"):
                for i, name in enumerate(FEATURE_COLUMNS):
                    xgb_importances[name] = float(xgb_scorer.model.feature_importances_[i])

            # 6. Train LightGBM (optimized hyperparameters)
            logger.info("Training LightGBM model...")
            lgb_scorer = LightGBMScorer()
            lgb_scorer.feature_names = FEATURE_COLUMNS
            lgb_scorer.train(X_train, y_train, *es_args)

            lgb_pred_train = lgb_scorer.predict(X_train.tolist())
            lgb_train_ic = spearmanr(lgb_pred_train, y_train)[0]
            lgb_val_ic = _holdout_ic(lgb_scorer, X_val, y_val, d_val)
            lgb_es_ic = _holdout_ic(lgb_scorer, X_es, y_es, d_es)

            lgb_importances = {}
            if hasattr(lgb_scorer.model, "feature_importances_"):
                for i, name in enumerate(FEATURE_COLUMNS):
                    lgb_importances[name] = float(lgb_scorer.model.feature_importances_[i])

            # 7. Train Ridge regression (baseline)
            logger.info("Training Ridge model...")
            ridge_scorer = RidgeScorer()
            ridge_scorer.feature_names = FEATURE_COLUMNS
            ridge_scorer.train(X_train, y_train)

            ridge_pred_train = ridge_scorer.predict(X_train.tolist())
            ridge_train_ic = spearmanr(ridge_pred_train, y_train)[0]
            ridge_val_ic = _holdout_ic(ridge_scorer, X_val, y_val, d_val)

            # 8. Save to ModelRegistry
            timestamp = bj_now().replace(" ", "T").replace(":", "-")[:19]
            registry = ModelRegistry()

            xgb_metrics = {
                "train_ic": round(xgb_train_ic, 4) if not np.isnan(xgb_train_ic) else 0.0,
                "val_ic": round(xgb_val_ic, 4) if not np.isnan(xgb_val_ic) else 0.0,
                "n_train": int(X_train.shape[0]),
                "n_val": int(X_val.shape[0]),
                "n_es": int(X_es.shape[0]),
                "val_ic_kind": "holdout_mean_daily_spearman",
                "embargo_days": LABEL_HORIZON,
                "holdout_used_for_early_stopping": holdout_used_for_es,
                "es_ic": round(xgb_es_ic, 4) if not np.isnan(xgb_es_ic) else 0.0,
                "n_features": X_train.shape[1],
                "feature_importance_top5": dict(sorted(xgb_importances.items(), key=lambda x: -x[1])[:5]),
            }
            xgb_version = f"xgb_{timestamp}"
            xgb_path = registry.register(xgb_version, xgb_scorer, xgb_metrics)
            logger.info(f"XGBoost saved: {xgb_version} → {xgb_path}")

            lgb_metrics = {
                "train_ic": round(lgb_train_ic, 4) if not np.isnan(lgb_train_ic) else 0.0,
                "val_ic": round(lgb_val_ic, 4) if not np.isnan(lgb_val_ic) else 0.0,
                "n_train": int(X_train.shape[0]),
                "n_val": int(X_val.shape[0]),
                "n_es": int(X_es.shape[0]),
                "val_ic_kind": "holdout_mean_daily_spearman",
                "embargo_days": LABEL_HORIZON,
                "holdout_used_for_early_stopping": holdout_used_for_es,
                "es_ic": round(lgb_es_ic, 4) if not np.isnan(lgb_es_ic) else 0.0,
                "n_features": X_train.shape[1],
                "feature_importance_top5": dict(sorted(lgb_importances.items(), key=lambda x: -x[1])[:5]),
            }
            lgb_version = f"lgb_{timestamp}"
            lgb_path = registry.register(lgb_version, lgb_scorer, lgb_metrics)
            logger.info(f"LightGBM saved: {lgb_version} → {lgb_path}")

            ridge_metrics = {
                "train_ic": round(ridge_train_ic, 4) if not np.isnan(ridge_train_ic) else 0.0,
                "val_ic": round(ridge_val_ic, 4) if not np.isnan(ridge_val_ic) else 0.0,
                "n_train": int(X_train.shape[0]),
                "n_val": int(X_val.shape[0]),
                "n_es": int(X_es.shape[0]),
                "val_ic_kind": "holdout_mean_daily_spearman",
                "embargo_days": LABEL_HORIZON,
                "holdout_used_for_early_stopping": holdout_used_for_es,
                "n_features": X_train.shape[1],
            }
            ridge_version = f"ridge_{timestamp}"
            ridge_path = registry.register(ridge_version, ridge_scorer, ridge_metrics)
            logger.info(f"Ridge saved: {ridge_version} → {ridge_path}")

            # 9. Log summary
            logger.info(
                f"Model retrain complete: "
                f"XGB train_ic={xgb_train_ic:.4f} val_ic={xgb_val_ic:.4f} | "
                f"LGB train_ic={lgb_train_ic:.4f} val_ic={lgb_val_ic:.4f} | "
                f"Ridge train_ic={ridge_train_ic:.4f} val_ic={ridge_val_ic:.4f} | "
                f"X={X_train.shape}"
            )

            # #94-A: 成功路径同样不许隐式 None — 此前落 :588 no-op 包裹
            # (07-19/20 历史"成功"行实为 no-op 假 success, records_written 恒 0)。
            # 显式 HandlerResult: 三模型版本与指标随 metadata 落 pipeline_runs
            # (铁律 #12 可观测)。
            return HandlerResult(
                success=True,
                metadata={
                    "versions": {
                        "xgb": xgb_version, "lgb": lgb_version, "ridge": ridge_version,
                    },
                    "xgb": xgb_metrics, "lgb": lgb_metrics, "ridge": ridge_metrics,
                },
            )

        except Exception as e:
            # #94-A: except 路径不许裸奔无 return — 隐式 None 会被 :588 no-op
            # 包裹成假 success (2026-09-01 实弹: 双 30s 超时→all_rows=None→
            # len(all_rows) TypeError→此处吞掉→None→假 success, 终态二连撞锁后
            # 行永久 running)。契约: 数据管道三件显式 HandlerResult。
            logger.warning(f"Model retrain: {e}", exc_info=True)
            return HandlerResult(success=False, errors=[f"model_retrain failed: {e}"])

    def _run_monthly_report(self, cfg: dict):
        from bootstrap import bootstrap_app
        from common.utils import bj_date
        date = bj_date()
        app = bootstrap_app()
        delivered = False
        try:
            msg = app.report.build_weekly_summary(date, report_mode=cfg_get("delivery.report_mode", "debug"))
            if msg:
                msg.title = f"月度报告 {date[:7]}"
                app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
                delivered = True
        except Exception as e:
            logger.exception(f"Monthly report failed: {e}")
        return HandlerResult(success=delivered, metadata={"date": date, "report": "monthly", "delivered": delivered})

    def _run_weekly_summary(self, cfg: dict):
        from common.utils import bj_date
        date = bj_date()
        from bootstrap import bootstrap_app
        app = bootstrap_app()
        msg = app.report.build_weekly_summary(date, report_mode=cfg_get("delivery.report_mode", "debug"))
        if msg:
            app.delivery.send(msg, report_mode=cfg_get("delivery.report_mode", "debug"))
        return HandlerResult(success=True, metadata={"date": date, "report": "weekly", "delivered": msg is not None})

    def _backup_via_staging(self, src: str, staging: str):
        """机制卡 (2026-09-07)①: 案1 staging 引擎舞步 — SQLite backup API。

        原地保留原实现语义 (一致性快照 src_con.backup)。独立成方法供两处
        调用: (a) 显式 engine=staging 直连; (b) 案2 VACUUM 引擎病
        (OperationalError 族) 降级回退。gzip 尾段不在此面 — 共用尾段留在
        _run_db_backup 体内 (retention 内省守卫钉体内流式摘要/轮转模式)。
        """
        import sqlite3
        src_con = sqlite3.connect(src)
        staging_con = sqlite3.connect(staging)
        src_con.backup(staging_con, pages=-1, progress=None)
        src_con.close()
        staging_con.close()
        logger.info(f"SQLite backup (consistent snapshot): {src} → {staging}")

    def _run_incremental_backup(self, cfg: dict):
        """增量备份 (v1.0 判据 6): 按表 dump 当日行 — db_backup (全备) 的日差件.

        幂等重跑; -1 表 (dump 失败) 落 errors 面不静默; db 文件不存在 =
        failed 不装成功 (铁律#12). backup_dir 走 _cfg_all 全键空间
        (ops.incremental_backup_dir, 默认 data/backups 与 db_backup 同根).
        """
        import os
        from ops_layer.incremental_backup import run_incremental_backup

        backup_dir = self._cfg_all(
            "ops.incremental_backup_dir", "data/backups", cfg)
        db_path = self.db.db_path
        if not os.path.exists(db_path):
            logger.warning(
                f"incremental_backup: db not found at {db_path}")
            return HandlerResult(
                success=False,
                errors=[f"incremental_backup: db not found: {db_path}"])

        # date 可经 cfg 指定 (缺省=当日) — 支持历史日回补, 并使测试可测确定行为,
        # 不再让"今天恰好等于夹具日"成为通过条件 (2026-09-16 时点炸弹修复).
        run_date = cfg.get("date") or bj_date()
        out = run_incremental_backup(db_path, backup_dir, date=run_date)
        summary = out["summary"]
        failed_tables = sorted(t for t, v in summary.items() if v == -1)
        errors = [
            f"incremental_backup: dump failed: {t}" for t in failed_tables
        ] if failed_tables else []
        # 三态: 有失败表但多数成功 = partial (不装成功, 不全盘 failed)
        success = not failed_tables
        return HandlerResult(
            success=success,
            errors=errors,
            metadata={
                "total_rows": out["total_rows"],
                "summary": summary,
                "backup_dir": out["dir"],
                "partial": bool(failed_tables),
                "date": run_date,
            },
        )

    def _run_db_backup(self, cfg: dict):
        import glob
        import gzip
        import hashlib
        import os
        import shutil
        import sqlite3
        backup_dir = "data/backups"
        os.makedirs(backup_dir, exist_ok=True)

        # 机制卡 (2026-09-07)⑤: tmp 清扫自愈 — 上轮中断遗留 qs_*.vacuum.tmp
        # (中断 VACUUM ≈14G 死件) 本轮开头清掉; 置于两道闸之前 (盘近满时先
        # 回收再判闸)。清扫 best-effort, 单件失败不阻断备份。
        # 模式刻意窄于轮转段 (qs_*.vacuum.tmp ≠ 08-21 血案锚 qs_*.db*)。
        for _stale in glob.glob(os.path.join(backup_dir, "qs_*.vacuum.tmp")):
            try:
                os.remove(_stale)
                logger.info(f"db_backup: swept stale vacuum tmp {_stale}")
            except OSError:
                pass

        usage = shutil.disk_usage(os.path.expanduser("~"))
        free_pct = usage.free / usage.total * 100
        if free_pct < 10:
            logger.warning(f"Disk space too low ({free_pct:.0f}% free), skipping backup")
            # 修1 (2026-09-05): 跳过≠成功 (铁律#12) — 09-02~09-04 四晚 runs 行
            # success+0s+error 空+无 .gz 假绿实证; 跳过=没做备份=failed 语义,
            # 且跳过即发 critical 告警 (静默跳过曾连告警链都不介入)。
            from ops_layer.core.models import Alert
            self.alert_manager.send(Alert(
                severity="critical", layer="ops",
                title="数据库备份被跳过: 磁盘空间不足",
                detail=(f"db_backup 跳过: 磁盘剩余 {free_pct:.1f}% (shutil 口径) < 10%, "
                        f"备份未产出, data/backups/ 无今日 .gz"),
                action="清理磁盘空间后手工补跑 db_backup",
            ))
            return HandlerResult(success=False,
                                 errors=[f"db_backup skipped: disk free {free_pct:.0f}% < 10%"])

        today = bj_now().split(" ")[0]
        src = self.db.db_path
        staging = os.path.join(backup_dir, f"qs_{today}.db")
        dst = os.path.join(backup_dir, f"qs_{today}.db.gz")

        # 机制卡 (2026-09-07)④: 单次闸 = df 余量 > 源库×1.2 (运行时 getsize
        # 自适应库涨缩, 不写死; f_bavail 口径与 disk_check 修2 同基)。
        # 峰值核算 (RED 备稿 ①): 案2 tmp 14.2G + gz 3.5G = 17.7G <
        # 15.6G×1.2≈18.7G — 闸盖住 tmp+gz 并存段; 案1 staging 峰值 19.1G >
        # 同闸 (案1 过不了自己的闸 = 主案选案2 次级证据)。
        # 与 ops.disk_alert_floor_gb=20G 地板键并行取严: 地板=全盘长期健康线,
        # 单次闸=一次性动作线, 语义注释落 ops.yaml。非文件库 (:memory:)
        # 无锚不设此闸 (getsize OSError → src_size=0), 修1/floor 两闸仍生效。
        try:
            src_size = os.path.getsize(src)
        except OSError:
            src_size = 0
        if src_size > 0:
            _st = os.statvfs(os.path.expanduser("~"))
            avail_b = _st.f_bavail * _st.f_frsize
            gate_b = src_size * 1.2
            if avail_b < gate_b:
                avail_gb = avail_b / 2 ** 30
                gate_gb = gate_b / 2 ** 30
                logger.warning(
                    f"Disk avail {avail_gb:.1f}G < src×1.2 gate {gate_gb:.1f}G, skipping backup")
                from ops_layer.core.models import Alert
                self.alert_manager.send(Alert(
                    severity="critical", layer="ops",
                    title="数据库备份被跳过: 磁盘余量不足 (源库×1.2 闸)",
                    detail=(f"db_backup 跳过: df 余量 {avail_gb:.1f}G (f_bavail 口径) < "
                            f"源库 {src_size / 2 ** 30:.1f}G × 1.2 = {gate_gb:.1f}G, "
                            f"备份未产出 (VACUUM tmp + gz 峰值需过此闸)"),
                    action="清理磁盘空间后手工补跑 db_backup",
                ))
                return HandlerResult(success=False, errors=[
                    f"db_backup skipped: disk avail {avail_gb:.1f}G < src×1.2 ({gate_gb:.1f}G)"])

        # 机制卡 (2026-09-07)①: 双轨引擎 — 案2 VACUUM INTO 主 (ro 一致性
        # 快照直写 tmp, 无裸 .db 与 .gz 并存 = 病A 消灭) + 案1 staging 显式
        # config 回退 (退路在 config 不在代码)。引擎键走 _cfg_all 全键空间
        # (#108 先例: Config 属主优先, cfg 块须嵌套形态兜底, 平点键恒 MISS)。
        engine = self._cfg_all("ops.db_backup_engine", "vacuum_into", cfg)
        if engine not in ("staging", "vacuum_into"):
            logger.warning(f"db_backup: unknown engine {engine!r}, fallback vacuum_into")
            engine = "vacuum_into"

        vacuum_tmp = os.path.join(backup_dir, f"qs_{today}.db.vacuum.tmp")
        produced = staging if engine == "staging" else vacuum_tmp
        partial = False
        errors: list = []

        try:
            if engine == "staging":
                self._backup_via_staging(src, staging)
            else:
                _t0 = time.time()
                src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
                src_con.execute("VACUUM INTO ?", (produced,))
                src_con.close()
                logger.info(
                    f"db_backup vacuum_into: {produced} "
                    f"({os.path.getsize(produced) / 2 ** 30:.2f}GiB "
                    f"in {time.time() - _t0:.0f}s, ro snapshot)")
        except Exception as e:
            # 盘满病伪装成引擎病进来 (SQLITE_FULL OperationalError) —
            # 归不降级族: 换引擎无意义 (案1 峰值更大), 清 tmp + failed。
            _msg = str(e).lower()
            if "full" in _msg and ("disk" in _msg or "database" in _msg):
                if os.path.exists(vacuum_tmp):
                    os.remove(vacuum_tmp)
                logger.warning(f"db_backup vacuum_into disk-full: {e}")
                return HandlerResult(success=False, errors=[f"db_backup failed: {e}"])
            if engine == "staging":
                if os.path.exists(staging):
                    os.remove(staging)
                return HandlerResult(success=False, errors=[f"db backup failed: {e}"])
            # 降级族 (引擎病 — 换引擎有意义): VACUUM OperationalError (锁/
            # 版本/只读态变体) → 同进程直落案1 staging 序 + partial 语义
            # (件在但引擎非默认, final_status='partial' 判读哨可辨)。
            # 盘满/超时族不在此面: 守卫已前置拦截, 中途 ENOSPC 在 gzip 段
            # 单独接 (不降级), 超时由 run_pipeline 层掐 + ghost 判读。
            logger.warning(f"db_backup vacuum_into failed, falling back to staging: {e}")
            if os.path.exists(vacuum_tmp):
                os.remove(vacuum_tmp)
            try:
                self._backup_via_staging(src, staging)
            except Exception as e2:
                if os.path.exists(staging):
                    os.remove(staging)
                return HandlerResult(
                    success=False,
                    errors=[f"db backup failed: vacuum_into ({e}) → staging ({e2})"])
            produced = staging
            partial = True
            errors = [f"db_backup: vacuum_into 失败降级 staging ({e})"]

        try:
            _t1 = time.time()
            with open(produced, "rb") as f_in, gzip.open(dst, "wb", compresslevel=1) as f_out:
                shutil.copyfileobj(f_in, f_out, length=1024*1024)
            os.remove(produced)
            logger.info(
                f"db_backup gzip: {dst} ({os.path.getsize(dst) / 2 ** 30:.2f}GiB "
                f"in {time.time() - _t1:.0f}s)")
        except OSError as e:
            # 不降级族 (盘满病): 中途 ENOSPC — 案1 峰值 19.1G > 案2 17.7G,
            # 降级 = 空跑一次再炸; 清 tmp + 半截 gz + failed, 判读哨接管。
            for _corpse in (produced, dst):
                if os.path.exists(_corpse):
                    try:
                        os.remove(_corpse)
                    except OSError:
                        pass
            return HandlerResult(success=False, errors=[f"db_backup failed: {e}"])

        src_checksum = ""
        try:
            # 流式摘要: 原实现 f.read() 会把整个 .gz (约 2.7GB) 一次读入内存,
            # 在 15GB 内存的生产机上与管线并发时有 OOM 风险。
            _h = hashlib.sha256()
            with open(dst, "rb") as f:
                for _chunk in iter(lambda: f.read(1024 * 1024), b""):
                    _h.update(_chunk)
            src_checksum = _h.hexdigest()[:16]
        except Exception:
            pass

        logger.info(f"DB backup (SQLite API): {src} → {dst} ({os.path.getsize(dst) / 1024:.0f} KB, DB={os.path.getsize(src)/1024/1024:.0f}MB, sha256={src_checksum}, engine={'staging' if (engine == 'staging' or partial) else 'vacuum_into'})")

        # 只轮转本 handler 自己产出的日备份 qs_<YYYY-MM-DD>.db.gz。
        # 原实现 glob("qs_*.db*") + 字典序会连带删除手工/安全备份和 -wal/-shm 残留:
        # 2026-08-21 23:03 实测删掉 3 份日备份(含当日刚产出的那份)+ 08-17 安全快照
        # + v4 建表锚点, 仅因它们同样命中 qs_*.db*。
        # 安全备份改放 data/backups/pinned/ 子目录, 本轮转逻辑不再触及。
        # W2 卡⑤ (2026-09-20): 轮转升级 GFS 三档 (7日+4周+6月) — 原 keep_daily
        # 单档只保 7 天恢复深度, 陈旧损坏/误删 8 天后无恢复点。分级逻辑/白名单
        # 删除/三硬约束全在 ops_layer/backup_retention (单一定义点, 本处只接线)。
        # 约束承接: 只碰 qs_????-??-??.db.gz (08-21 血案锚不复发), 锚=文件名日期
        # 非 mtime (cp --preserve 陷阱), 保留集非空且含最新 (硬约束#2)。
        from ops_layer.backup_retention import prune_backups
        keep_daily = 7
        keep_weekly = 4
        keep_monthly = 6
        if cfg:
            try:
                keep_daily = max(1, int(cfg.get("backup.keep_daily", 7)))
            except (TypeError, ValueError):
                keep_daily = 7
            try:
                keep_weekly = max(0, int(cfg.get("backup.keep_weekly", 4)))
            except (TypeError, ValueError):
                keep_weekly = 4
            try:
                keep_monthly = max(0, int(cfg.get("backup.keep_monthly", 6)))
            except (TypeError, ValueError):
                keep_monthly = 6
        deleted = prune_backups(backup_dir, keep_daily=keep_daily,
                                keep_weekly=keep_weekly,
                                keep_monthly=keep_monthly)
        if deleted:
            logger.info(f"db_backup GFS prune: removed {len(deleted)}: "
                        f"{[os.path.basename(p) for p in deleted]}")

        remote_path = cfg.get("backup.remote_path", "") if cfg else ""
        if remote_path:
            import subprocess as _sp
            try:
                _sp.run(["rsync", "-avz", dst, remote_path],
                       capture_output=True, text=True, timeout=120, check=True)
                logger.info(f"Remote backup: {dst} → {remote_path} (rsync OK)")
            except Exception as e:
                logger.warning(f"Remote backup failed: {e}")

        metadata = {"engine": "staging" if (engine == "staging" or partial) else "vacuum_into"}
        if partial:
            metadata["partial"] = True
        return HandlerResult(success=True, errors=errors, metadata=metadata)

    def _run_log_rotation(self, cfg: dict):
        """P0-6: 日志轮转 — 尺寸轮转 + 30 天清理。

        血案: 原实现只删 mtime 超 30 天的文件, 但 pipeline.log 由 cron 每日
        >> 追加写, mtime 永远新鲜 → 从不轮转, 涨到 12.4MB (九成 tqdm 噪音)。
        结构化日志 (quantstar.log) 自带 RotatingFileHandler 轮转, 本 handler
        只管 cron 重定向文件与其他纯文本日志的尺寸轮转与过期清理。
        """
        import os
        import time

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        logs_dir = os.path.join(project_root, "logs")
        if not os.path.isdir(logs_dir):
            return HandlerResult(success=True, errors=[])

        max_bytes = int(cfg.get("max_bytes", 10 * 1024 * 1024))
        backup_count = int(cfg.get("backup_count", 5))
        now = time.time()
        cutoff = now - 30 * 86400
        rotated, removed = [], []

        for f in sorted(os.listdir(logs_dir)):
            path = os.path.join(logs_dir, f)
            if not os.path.isfile(path):
                continue
            # 跳过 RotatingFileHandler / 本 handler 自管的轮转备份 (.1~.9 后缀)
            if any(f.endswith(f".{i}") for i in range(1, 10)):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            # 1) 尺寸轮转: cron 重定向文件 (pipeline.log 等) 无 RotatingFileHandler 兜底
            if st.st_size >= max_bytes:
                try:
                    for i in range(backup_count - 1, 0, -1):
                        src_path = f"{path}.{i}"
                        if os.path.exists(src_path):
                            os.replace(src_path, f"{path}.{i + 1}")
                    os.replace(path, f"{path}.1")
                    rotated.append(f)
                    logger.info(f"Rotated oversized log: {f} ({st.st_size} bytes)")
                    continue
                except OSError as e:
                    logger.warning(f"Log rotation failed for {f}: {e}")
            # 2) 过期清理: 只删 30 天未写的文件 (活跃文件跳过)
            if st.st_mtime < cutoff:
                try:
                    os.remove(path)
                    removed.append(f)
                    logger.info(f"Removed old log: {f}")
                except OSError as e:
                    logger.warning(f"Failed to remove old log {f}: {e}")

        return HandlerResult(success=True, errors=[],
                             metadata={"rotated": rotated, "removed": removed})

    def _run_disk_check(self, cfg: dict):
        import os
        # 修2 (2026-09-05): df 口径 + 双判据 — 旧 shutil 口径 used/total 的
        # used = total−f_bfree 含 root 保留块, 物理上限 ~95%, `>90` 实际触发线
        # = df ~94.5% (缓冲仅 ~1.2G); 09-03/09-04 全天 shutil 87.5~89.7% 恒在
        # 盲区 → alerts 磁盘类全史 0 行 (同期 81~88 连续落库 = 通道健康,
        # 病在条件从未成立)。df 口径: Used = Size − Avail (f_bavail 基, 排除
        # root 保留块), pct = used/(used+avail) 与 df 输出对齐; 双判据 =
        # pct≥阈值 或 绝对余量<地板 — GB 是三口径并存时唯一无歧义锚。
        # 阈值走 Config 全键空间 (#108 _cfg_all 先例, 禁管道块作用域读法)。
        path = os.path.expanduser("~")
        st = os.statvfs(path)
        avail_b = st.f_bavail * st.f_frsize
        used_b = st.f_blocks * st.f_frsize - avail_b
        pct = used_b / (used_b + avail_b) * 100
        avail_gb = avail_b / (2 ** 30)
        logger.info(f"Disk usage: {pct:.1f}% (df 口径, avail {avail_gb:.1f}G)")
        warning_pct = float(self._cfg_all("ops.disk_warning_pct", 85, cfg))
        critical_pct = float(self._cfg_all("ops.disk_alert_pct", 92, cfg))
        floor_gb = float(self._cfg_all("ops.disk_alert_floor_gb", 20, cfg))
        if pct >= critical_pct or avail_gb < floor_gb:
            from ops_layer.core.models import Alert
            self.alert_manager.send(Alert(
                severity="critical", layer="ops",
                title="磁盘空间严重不足",
                detail=(f"磁盘使用率 {pct:.1f}% (df 口径), 可用 {avail_gb:.1f}G "
                        f"(critical: ≥{critical_pct:.0f}% 或 <{floor_gb:.0f}G)"),
                action="清理磁盘空间",
            ))
        elif pct >= warning_pct:
            from ops_layer.core.models import Alert
            self.alert_manager.send(Alert(
                severity="warning", layer="ops",
                title="磁盘空间偏高",
                detail=(f"磁盘使用率 {pct:.1f}% (df 口径), 可用 {avail_gb:.1f}G "
                        f"(warning: ≥{warning_pct:.0f}%; critical: ≥{critical_pct:.0f}% "
                        f"或 <{floor_gb:.0f}G)"),
                action="检查磁盘增长并安排清理",
            ))
        else:
            # 磁盘恢复不代表备份成功，仅关闭本监控拥有的精确标题。
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT id FROM alerts WHERE status = 'open' AND layer = 'ops' "
                "AND title IN (?, ?, ?)",
                ("磁盘空间不足", "磁盘空间严重不足", "磁盘空间偏高"),
            ).fetchall()
            for row in rows:
                self.alert_manager.resolve(row["id"])
        return HandlerResult(success=True, errors=[])

    def shutdown(self, timeout: float = 10.0):
        """关闭管道执行 executor，避免解释器退出时泄漏 ghost 线程。

        P0-7 方向C: 先有界等待已登记的存活 ghost 自然结束 (而非依赖
        concurrent.futures 全局 atexit 的无限 join); 超时仍有存活者则发
        critical 告警如实记录, 再 shutdown(wait=False) 放行退出。
        红线: 不主动掐断 ghost 线程 (2026-08-17 静默丢数据事故教训)。
        """
        try:
            survivors = self.ghosts.wait_active(timeout)
            if survivors:
                names = ", ".join(sorted({s.pipeline_name for s in survivors}))
                logger.error(f"shutdown: {len(survivors)} ghost(s) still alive after {timeout}s: {names}")
                from ops_layer.core.models import Alert
                self.alert_manager.send(Alert(
                    severity="critical", layer="ops",
                    title="Ghost 线程未退出 (shutdown)",
                    detail=(f"有界等待 {timeout}s 后仍有 {len(survivors)} 个 ghost 存活: {names}; "
                            f"进程退出时可能被全局 atexit join 拖住并持有 DB 锁"),
                    action="检查 logs/ghost_registry.jsonl; 必要时重启进程释放锁",
                ))
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.warning(f"Orchestrator executor shutdown error: {e}")
