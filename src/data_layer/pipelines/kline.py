"""pipelines/kline.py — K 线采集管道（增量 + 并行，通过 SourceRegistry 获取数据源）"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date, datetime as _dt

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos import get_repo

logger = get_logger("data.kline")

_SOURCE_CALL_TIMEOUT_S = 15.0  # 单个源单只拉取超时, 慢源不得阻塞整体

# DDP-BUDGET-FIT: 北交所旧代码前缀 (43/83/87) 所有 kline 源均不覆盖 (实测 0 行),
# 逐股×4源重试浪费 ~1300s。批量跳过, 仅保留 92xxx (有数据覆盖)。
# 92xxx 是北交所新代码, 有源覆盖, 不得跳过。
_BSE_SKIP_PREFIXES = ("43", "83", "87")

# W2-1 丙案 (2026-09-21, 审计裁定): 停牌/陈旧码不做「排除」只做「显形」。
# 60 天无 K线 且 is_active=1 的码: 分类='unknown' (不声称停牌/退市, 不从分母移除,
# 不静默吞 errors), 写入显形表 pipeline_kline_exceptions, 数量趋势异常即报警。
# 判据独立性: 「未采到」只作排查分组证据, 不作停牌推断; 活跃股 (60 天内有 K线)
# 空返回仍进 errors 判源故障, 不误伤。
_SUSPENDED_KLINE_LOOKBACK_DAYS = 60

_UNKNOWN_GAP_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_kline_exceptions (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'unknown',
    run_id TEXT,
    last_kline_date TEXT,
    source_attempts TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (date, code, reason)
);
CREATE INDEX IF NOT EXISTS idx_kline_exc_date ON pipeline_kline_exceptions(date);
CREATE INDEX IF NOT EXISTS idx_kline_exc_code ON pipeline_kline_exceptions(code);
"""


def _last_kline_date(conn, code: str):
    """该 code 最近一条 K线日期 (显形表 last_kline_date 字段); 无则 None。"""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM daily_klines WHERE code = ?", (code,)).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _row_is_degenerate(volume, amount, close) -> bool:
    """退化行: volume/amount/close **显式**全 0 (停牌/无交易退化形态)。

    NULL (缺列/夹具未填) 不算退化 —「缺列」≠「显式 0」, 判例同族:
    空集与零命中必须可区分。w21 夹具只填 close 的行是「有 close 无
    volume/amount」, 不是停牌退化形态 — 原版把 NULL 当 0 误杀过 (自踩)。

    与病D哨兵行 (volume==amount==5.88e-39) 区分: 那是浮点下溢哨兵,
    这是显式全 0 — 两者都「无意义」, 但本判据只认显式全 0 (哨兵行非 0 不误伤)。
    """
    def _is_explicit_zero(v):
        return v is not None and v == 0
    return (_is_explicit_zero(volume)
            and _is_explicit_zero(amount)
            and _is_explicit_zero(close))


def _fetch_is_expected_empty(conn, code: str, query_date: str) -> bool:
    """该股票在 query_date 的空返回是否「预期」= 近 60 天无「有效行」。

    W2 停牌豁免卡 (feat/suspended-exempt): 判据从「无任何行」升级为
    「无有效行」——末行全 0 退化 (停牌形态) 算预期空。

    True  = 近 _SUSPENDED_KLINE_LOOKBACK_DAYS 天无有效 K 线 → unknown_gap 分组
    False = 近 60 天有有效行 (末行非零真值) → 空是源故障, 必须判 partial。

    安全前提 (统筹核定): 判据纯库内读 daily_klines, 不依赖外网/全源探测 —
      源故障当天该票最后一条仍是非零真值行 (故障发生在今天, 历史是真的);
      真停牌最后一条是全 0 退化行 ⇒ 真源故障当天不会被误豁免。
      极端形态「故障当天恰好也写进全 0 行」属写入闸问题 (病D哨兵同族), 留卡。

    conn 为 None (无库上下文) 时保守返 False — 宁报错不漏报。
    """
    if conn is None:
        return False
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.strptime(query_date, "%Y-%m-%d")
                  - timedelta(days=_SUSPENDED_KLINE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        # 表结构自适应: 夹具表 (w21) 只有 code/date/close 三列 — 缺 volume/amount
        # 时无从判定退化 = 保守不判退化 (缺列≠显式0, 不误杀历史真行)。
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_klines)")}
        except Exception:
            cols = set()
        sel = ", ".join(c for c in ("volume", "amount", "close") if c in cols) or "close"
        last = conn.execute(
            f"SELECT {sel} FROM daily_klines "
            "WHERE code = ? AND date >= ? ORDER BY date DESC LIMIT 1",
            (code, cutoff)).fetchone()
        if last is None:
            return True
        d = dict(zip([c for c in ("volume", "amount", "close") if c in cols], last))
        return _row_is_degenerate(
            d.get("volume"), d.get("amount"), d.get("close"))
    except Exception:
        return False


# ---- L2: suspended_codes 表 (显形载体, 非硬编码名单) ----
SUSPENDED_DDL = """
CREATE TABLE IF NOT EXISTS suspended_codes (
    code TEXT PRIMARY KEY,
    last_active_date TEXT,
    detected_at TEXT,
    source TEXT DEFAULT 'auto',
    note TEXT DEFAULT ''
)
"""


def _ensure_suspended_table(conn) -> None:
    """幂等建表 (显形载体; 写失败不阻断主流程 — 观测载体非关键路径)。"""
    if conn is None:
        return
    conn.execute(SUSPENDED_DDL)
    conn.commit()


def _suspended_mark_auto(conn, code: str, last_active_date, detected_at: str) -> None:
    """检出退化末行 → auto 入表 (幂等 upsert; 不覆盖已有 manual 行)。

    健性: 先 ensure 表 (调用方库可能是全新库/夹具库, 不依赖先建表 —
    附加观测面失败不得反噬主流程, 铁律#12)。
    """
    if conn is None:
        return
    try:
        _ensure_suspended_table(conn)
        conn.execute(
            "INSERT INTO suspended_codes (code, last_active_date, detected_at, source, note) "
            "VALUES (?, ?, ?, 'auto', '') "
            "ON CONFLICT(code) DO UPDATE SET "
            "last_active_date=excluded.last_active_date, detected_at=excluded.detected_at "
            "WHERE suspended_codes.source != 'manual'",
            (code, last_active_date, detected_at))
        conn.commit()
    except Exception:
        pass


def _suspended_mark_manual(conn, code: str, note: str) -> None:
    """人工挂 (必须带 note — 硬要求: 人工判断须留理由)。"""
    if conn is None:
        return
    try:
        _ensure_suspended_table(conn)
        conn.execute(
            "INSERT INTO suspended_codes (code, last_active_date, detected_at, source, note) "
            "VALUES (?, '', datetime('now', '+8 hours'), 'manual', ?) "
            "ON CONFLICT(code) DO UPDATE SET source='manual', note=excluded.note",
            (code, note))
        conn.commit()
    except Exception:
        pass


def _suspended_maybe_recover(conn, code: str) -> bool:
    """非零行出现 → auto 行出表 (自愈靠新数据不靠等, 判例#154 同构)。
    manual 行不动 (人工判断不被自动逻辑覆盖)。返回是否真删了。"""
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT volume, amount, close FROM daily_klines "
            "WHERE code = ? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        if row is not None and not _row_is_degenerate(row[0], row[1], row[2]):
            cur = conn.execute(
                "DELETE FROM suspended_codes WHERE code = ? AND source = 'auto'", (code,))
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        return False
    return False


def _maybe_mark_suspended(conn, code: str, query_date: str,
                         records: list | None = None) -> bool:
    """退化 (显式全 0) ⇒ auto 入 suspended_codes (两分支公共位)。

    修 W1 终审 P1#3 (10-20): 原实现把 auto 入表只挂在 err 分支
    (「源返空」), 而末行退化的票多数是**源返了记录但记录全 0** 走 records
    分支 ⇒ 判据说退化、落表说不是, 6 只漏检 (000016/002388/002717/
    002731/301314/301468)。本 helper 把「判退化」与「入表」绑到同一处。

    **判定源分两种** (v2 修, 根因: records 分支的调用点在 batch_upsert
    之前 ⇒ 查库内末行看到的是**昨天**的行, 今天刚返的退化行还没落库,
    判不出退化 —— 这才是 6 只漏检的真机制):
      - records 给了 (records 分支) → 用**本次返的记录**判定 (与落库顺序无关)
      - records 没给 (err 分支, 源返空) → 回落查库内末行 (库里末行=最新已知)

    判据**不放宽**: 须 volume/amount/close 三列**显式**全 0
    (NULL≠0; 哨兵 2^-127≠0; 非零真值≠0) —— 只有真停牌退化形态入表。
    已有 manual 行不被覆盖 (人工判断优先)。
    """
    if conn is None or not code:
        return False
    degenerate = False
    if records:
        for r in records:
            if _row_is_degenerate(r.get("volume"), r.get("amount"), r.get("close")):
                degenerate = True
                break
    else:
        try:
            row = conn.execute(
                "SELECT volume, amount, close FROM daily_klines "
                "WHERE code = ? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        except Exception:
            return False
        if row is None:
            return False
        degenerate = _row_is_degenerate(row[0], row[1], row[2])
    if not degenerate:
        return False
    try:
        _suspended_mark_auto(conn, code, _last_kline_date(conn, code), query_date)
        return True
    except Exception:
        return False


def _is_suspended(conn, code: str) -> bool:
    """消费面查询: 命中 suspended_codes → 豁免 partial。"""
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM suspended_codes WHERE code = ? LIMIT 1", (code,)
        ).fetchone() is not None
    except Exception:
        return False


def _record_unknown_gap(conn, date: str, code: str, last_kline_date, source_attempts: str,
                        run_id=None):
    """60 天无 K线 且 is_active=1 的码写入显形表 (classification='unknown')。

    显形非排除: 不声称停牌、不从分母移除; 数量趋势异常由 freshness/health 层报警。
    写库失败不阻断主流程 (显形表是观测载体, 非关键路径)。
    """
    if conn is None:
        return
    try:
        conn.executescript(_UNKNOWN_GAP_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_kline_exceptions "
            "(date, code, reason, classification, run_id, last_kline_date, source_attempts) "
            "VALUES (?,?,?,?,?,?,?)",
            (date, code, "no_kline_60d", "unknown", run_id, last_kline_date, source_attempts))
        conn.commit()
    except Exception:
        logger.warning(f"kline: unknown_gap 显形写入失败 code={code}", exc_info=True)


def _baostock_bulk_ready(date: str) -> bool:
    """DL-25: baostock 日 K 于交易日 17:30 才入库 (盘点文档 §一 结构性限制)。

    历史日期 (早于今天) 恒就绪; 当日数据须本地时间 ≥17:30。
    早于 17:30 抓当日必空 → 调用方跳过备源, 避免空跑 15s。
    """
    if date < _date.today().isoformat():
        return True
    now = _dt.now()
    return now.hour * 60 + now.minute >= 17 * 60 + 30


class KlinePipeline(BasePipeline):
    name = "kline"
    requires = ["stock_info"]
    produces = ["daily_klines"]

    def __init__(self, source_registry=None, max_workers: int = 8):
        self.source_registry = source_registry
        self._max_workers = max_workers
        # empty 三态卡: 每只股票取完的源返空形态桶 (EMPTY/ERROR/NONE)。
        # 走实例属性不走 _fetch_kline 签名 — 签名一改就打碎
        # test_dry_run_pipelines.py:246 的四签名 fake (v1 教训)。
        self._last_empty_forms: dict = {}

    def _fetch_kline(self, repo, code: str, start_date: str, end_date: str) -> tuple[int, list[dict], str | None]:
        """按源链取 K线; 全空时按形态分桶上报 (empty 三态可辨卡)。

        三态 (10-19 生产实证, quantstar.log 五源对 920305):
          EMPTY — 源返 0 行 (tushare/easyquotation/baostock rows=0)
          ERROR — 源抛异常 (akshare_sina JSONDecodeError / akshare
                  ConnectionError; 同日 698 条 tushare rows=0 + 分钟限频
                  ERROR 批量证明 ERROR/EMPTY 同窗混合是常态, 分类是刚需)
          NONE  — 源适配器显式返 None (tushare 无 token, tushare_source.py:194)
        三桶写实例属性 self._last_empty_forms (签名零变更, 不动存量
        四签名 fake — v1 教训), run 侧聚合进 metadata["empty_forms"]。
        ERROR 条目只记 source+error_type 不记全文 (防日志爆)。
        """
        if not self.source_registry:
            self._last_empty_forms = {}
            return 0, [], "no source_registry"
        sources = self.source_registry.get_sources_for("kline")
        if not sources:
            self._last_empty_forms = {}
            return 0, [], "no available kline sources"
        empty_names: list[str] = []
        error_entries: list[dict] = []
        none_names: list[str] = []
        for source in sources:
            try:
                # 每个源加独立超时, 避免慢源对退市股挂起阻塞整体。
                # 注意: 不用 `with ThreadPoolExecutor(...)` 上下文, 其 __exit__ 会
                # shutdown(wait=True) 等待超时线程结束而再次挂起。
                _single = ThreadPoolExecutor(max_workers=1)
                try:
                    fut = _single.submit(
                        source.get_data, "kline",
                        {"code": code, "start_date": start_date, "end_date": end_date},
                    )
                    klines = fut.result(timeout=_SOURCE_CALL_TIMEOUT_S)
                finally:
                    _single.shutdown(wait=False, cancel_futures=True)
                # 刀1 (spec EQ_WIRE): 每源结果痕迹 — 空窗也打 (链内静默是
                # 10-07「调了但看不见」的第一级)。非异常路径零日志是盲区。
                if not klines:
                    logger.info(f"kline: source {source.name} for {code}: rows=0")
                if klines:
                    # 重算 change_pct: 基于同复权口径前一复权日 close, 保证数据有效性
                    klines = self._recompute_change_pct(repo, code, klines)
                    records = []
                    for k in klines:
                        records.append({
                            "code": k.code, "date": k.date,
                            "open": k.open, "high": k.high, "low": k.low,
                            "close": k.close, "volume": k.volume, "amount": k.amount,
                            "pre_close": k.pre_close, "change_pct": k.change_pct,
                            "turnover_rate": k.turnover_rate,
                        })
                    return len(records), records, None
                # empty 三态卡: 分类 — None (适配器显式返空) 与 [] (真 0 行)
                # 是不同形态 (无 token=配置态 vs 源无数据=数据态), 报文与
                # metadata 都须可分。
                if klines is None:
                    none_names.append(source.name)
                else:
                    empty_names.append(source.name)
            except Exception as e:
                # 09-25 塌方教训: debug 级被吞致腿内 21 分钟零业务日志 — 升 info。
                logger.info(f"kline: source {source.name} failed for {code}: {e}")
                error_entries.append({
                    "source": source.name,
                    "error_type": type(e).__name__,
                })
                continue
        names = [src.name for src in sources]
        self._last_empty_forms = {
            "empty": empty_names, "error": error_entries, "none": none_names,
        }
        # 报文: 保留 "returned empty" 子串 (kline.py:362 分流 +
        # test_kline_pipeline.py:82-84 双依赖), 尾部分形态摘要。
        seg = (f"all {len(sources)} kline sources {names} "
               f"returned empty for {code}")
        parts = []
        if empty_names:
            parts.append(f"EMPTY×{len(empty_names)}[{','.join(empty_names)}]")
        if none_names:
            parts.append(f"NONE×{len(none_names)}[{','.join(none_names)}]")
        if error_entries:
            err_desc = ";".join(
                f"{e['source']}:{e['error_type']}" for e in error_entries)
            parts.append(f"ERROR×{len(error_entries)}[{err_desc}]")
        if parts:
            seg += f" (形态: {'; '.join(parts)})"
        return 0, [], seg

    def _recompute_change_pct(self, repo, code: str, klines: list) -> list:
        """重算 change_pct: 用同库前一复权日 close 计算, 修正数据源 pre_close 缺失/不复权问题。
        首日(无前一交易日)则尝试用数据源自身 pre_close, 否则置 0。
        """
        try:
            conn = None
            direct = getattr(repo, "db", None)
            if direct is not None and hasattr(direct, "get_connection"):
                conn = direct.get_connection()
            elif hasattr(repo, "get_connection"):
                conn = repo.get_connection()
            else:
                child = getattr(repo, "kline", None)
                child_db = getattr(child, "db", None) if child else None
                if child_db is not None and hasattr(child_db, "get_connection"):
                    conn = child_db.get_connection()
            if conn is None:
                raise AttributeError("repo has no usable db connection")
            prev = conn.execute(
                "SELECT close FROM daily_klines WHERE code=? AND date<? AND close>0 ORDER BY date DESC LIMIT 1",
                (code, klines[0].date),
            ).fetchone()
        except Exception:
            prev = None

        prev_close = float(prev[0]) if prev and prev[0] else 0.0
        out = []
        for k in sorted(klines, key=lambda x: x.date):
            close = float(k.close or 0)
            # F-23 (2026-10-22): 停牌日 close=0 → change_pct 记 **NULL** (不可算),
            # 不是 0 (会被下游读成"平盘") 也不是 -100 (会被读成跌停).
            # 用户 10-22 裁: 停牌日 change_pct 记 NULL.
            if close <= 0:
                k.change_pct = None
                k.pre_close = prev_close if prev_close > 0 else k.pre_close
                out.append(k)
                continue                      # prev_close 保持不变, 不被 0 污染
            if prev_close and prev_close > 0:
                k.change_pct = round((close - prev_close) / prev_close * 100, 2)
            else:
                k.change_pct = 0.0
            k.pre_close = prev_close if prev_close > 0 else k.pre_close
            if close and close > 0:
                prev_close = close
            out.append(k)
        return out

    def run(self, input: PipelineInput) -> PipelineResult:
        repo = get_repo(input.db)
        symbols = input.symbols or repo.stock.get_all_active()
        start_date = input.effective_start
        end_date = input.effective_end

        # DDP-BUDGET-FIT: 北交所旧码 (43/83/87) 全源无覆盖, 批量跳过避免逐股×4源空跑。
        # 仅对全市场模式生效 (input.symbols 为空时); 指定符号集不干预。
        bj_skipped = 0  # P1-4: 计数进 metadata (覆盖率分母可审计, 不静默)
        if not input.symbols:
            before = len(symbols)
            symbols = [c for c in symbols if not c.startswith(_BSE_SKIP_PREFIXES)]
            skipped = before - len(symbols)
            bj_skipped = skipped
            if skipped:
                logger.info(f"kline: 跳过 {skipped} 只北交所旧码 (43/83/87, 全源无覆盖)")

        if hasattr(input, 'db') and input.db:
            try:
                conn = input.db.get_connection()
            except Exception:
                conn = None
        else:
            conn = None

        if conn is not None and not input.symbols:
            # DL-51: 幂等短路 — end_date 行数对比近期交易日中位数。
            # 原判据用 get_all_active() (5996, 含北交所) 做分母, 但 bulk 源只覆盖
            # 沪深 (~5540), 98% 阈值永远达不到 → 17:40 备源补跑每次都重拉全市场
            # (上一世"主源已成功则短路"的声明被实测证伪, 见回执)。
            # 现: end_date 已有行数 >= 近 5 个交易日中位行数 95% 即视为已覆盖。
            # 该判据与源无关 — 只问"今日行数是否接近正常交易日", 主源成功即短路,
            # 主源失败 (0 行) 则继续走降级链。
            try:
                recent = conn.execute(
                    "SELECT COUNT(*) FROM daily_klines WHERE date < ? "
                    "GROUP BY date ORDER BY date DESC LIMIT 5",
                    (end_date,),
                ).fetchall()
                if recent:
                    counts = sorted(r[0] for r in recent)
                    baseline = counts[len(counts) // 2]  # 中位数, 抗单日异常
                    covered = conn.execute(
                        "SELECT COUNT(*) FROM daily_klines WHERE date = ?",
                        (end_date,),
                    ).fetchone()[0]
                    if baseline > 0 and covered >= baseline * 0.95:
                        return PipelineResult(name=self.name, date=input.date, status="success",
                            records_written=0, errors=[],
                            metadata={"start_date": start_date, "end_date": end_date,
                                      "mode": "market_covered", "covered": covered,
                                      "baseline": baseline})
            except Exception:
                pass

        if conn is not None and input.symbols:
            # 指定符号集: 仅当这些符号在 end_date 全部有行才跳过
            try:
                target_symbols = input.symbols
                phs = ",".join("?" for _ in target_symbols)
                covered = conn.execute(
                    f"SELECT COUNT(*) FROM daily_klines WHERE date = ? AND code IN ({phs})",
                    (end_date, *target_symbols)
                ).fetchone()[0]
                if covered >= len(target_symbols):
                    return PipelineResult(name=self.name, date=input.date, status="success",
                        records_written=0, errors=[],
                        metadata={"start_date": start_date, "end_date": end_date,
                                  "mode": "symbols_covered", "covered": covered, "target": len(target_symbols)})
            except Exception:
                pass

        if not symbols:
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_written=0,
                errors=["no available symbols for kline sources"],
            )

        errors = []
        total = 0

        if not input.symbols and start_date == end_date and self.source_registry:
            # 全市场当日批量抓取: 只接受覆盖达标的结果, 否则继续尝试下一个源,
            # 避免某源返回残缺切片(如仅 100 只)即提前返回 success 掩盖失败。
            _BULK_MIN_ROWS = 4000
            # DL-25: 批量 K 线是注册表可见能力 (kline_bulk), 不再 duck-type 适配器
            # 专有方法。注册表按 type_priority 排序 → 主源失败或行数 < _BULK_MIN_ROWS
            # 时自动降级到下一个声明 kline_bulk 的源 (tushare 主 → baostock 备)。
            bulk_sources = self.source_registry.get_sources_for("kline_bulk")
            intended = bulk_sources[0].name if bulk_sources else ""
            for source in bulk_sources:
                bulk_fetch = getattr(source, "get_daily_market", None)
                if bulk_fetch is None:
                    continue
                # DL-25 (降级时机≥17:30): baostock 当日数据 17:30 后才入库,
                # 早于 17:30 跳过备源避免空跑; 历史日期恒就绪。
                # 当日主源挂 + 早于 17:30 → 本批无备源可用, 由 ≥17:30 补跑兜底
                # (crontab 行待 GO, 见回执)。
                if source.name == "baostock" and not _baostock_bulk_ready(start_date):
                    logger.info(
                        "kline bulk: baostock skipped (当日数据 17:30 后才入库)")
                    continue
                try:
                    records = bulk_fetch(start_date)
                    if records and len(records) >= _BULK_MIN_ROWS:
                        # DL-03 (D91): 原子写入 + 写入后行数校验, 不达标整体回滚,
                        # 杜绝"半批入库"产生新的残缺日 (F-07)。
                        written = repo.kline.atomic_upsert_date(
                            start_date, records, min_rows=_BULK_MIN_ROWS)
                        if written < 0:
                            logger.warning(
                                f"bulk source {source.name}: post-write row check failed "
                                f"(< {_BULK_MIN_ROWS}), rolled back, trying next source"
                            )
                            errors.append(
                                f"{source.name} bulk daily post-write check failed, rolled back")
                            continue
                        # DL-25-guard: 保留此前源累积的 errors (如主源墙钟超时后
                        # 降级备源成功) —— 铁律 #12 不得静默降级, 降级痕迹随
                        # PipelineResult 写入 pipeline_runs 可观测。
                        # DL-26: 非主源成功 → 落 SourceSwitchRecord 留痕 (设计 §15)。
                        if source.name != intended:
                            try:
                                from data_layer.source_switch import record_source_switch
                                record_source_switch(
                                    input.db, data_type="kline_bulk",
                                    intended_source=intended, actual_source=source.name,
                                    reason="; ".join(errors[-3:]) if errors else "主源不可用",
                                    pipeline=self.name)
                            except Exception:  # noqa: BLE001 — 留痕失败不阻断
                                pass
                        return PipelineResult(
                            name=self.name, date=input.date, status="success",
                            records_written=written, errors=errors,
                            metadata={
                                "start_date": start_date, "end_date": end_date,
                                "mode": "bulk_daily", "source": source.name,
                            },
                        )
                    elif records:
                        logger.warning(
                            f"bulk source {source.name} returned only {len(records)} rows "
                            f"(< {_BULK_MIN_ROWS}), trying next source"
                        )
                except Exception as exc:
                    errors.append(f"{source.name} bulk daily failed: {exc}")

        # empty 三态卡: 每只股票的源结果形态桶 (machine-readable) —
        # _fetch_kline 零签名变更 (v1 教训: 加参打碎四签名 fake),
        # 三桶走实例属性, run 侧逐 future 收集。
        empty_forms: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {executor.submit(
                self._fetch_kline, repo, code, start_date, end_date): code
                for code in symbols}
            batch = []
            unknown_gaps = []
            for future in as_completed(futures):
                count, records, err = future.result()
                _forms = getattr(self, "_last_empty_forms", None) or {}
                if _forms:
                    empty_forms[futures[future]] = dict(_forms)
                if err:
                    # W2-1 丙案: 60 天无 K线 且 is_active=1 → unknown_gap 显形分流,
                    # 不进 errors (不判源故障), 但写显形表 + metadata 计数 (不静默)。
                    # 活跃股空返回仍进 errors — 判据独立性, 不误伤真源故障。
                    code_done = futures[future]
                    if "returned empty" in err and _fetch_is_expected_empty(
                            conn, code_done, end_date):
                        unknown_gaps.append(code_done)
                        _record_unknown_gap(
                            conn, end_date, code_done,
                            _last_kline_date(conn, code_done), err)
                        # L2: 检出退化末行 → auto 入 suspended_codes (显形载体,
                        # 下一只停牌股自动入表零新卡 — 非硬编码名单)。
                        # 走公共 helper (W1 终审 P1#3: 判退化与入表必须同源)
                        _maybe_mark_suspended(conn, code_done, end_date)
                        logger.info(
                            f"kline: {code_done} 60 天无 K线, unknown_gap 显形 "
                            f"(last={_last_kline_date(conn, code_done)})")
                        continue
                    errors.append(err)
                elif records:
                    # L2 自愈: 非零行出现 → auto 行出表 (靠新数据解除不靠等, #154)
                    code_done = futures[future]
                    if _suspended_maybe_recover(conn, code_done):
                        logger.info(
                            f"kline: {code_done} 恢复交易 (非零行), "
                            f"suspended_codes 自动出表")
                    # L2 补 (W1 终审 P1#3 修): 源返了记录但记录是**全 0 退化行**
                    # ⇒ 同样是停牌退化形态, 该码也必须入表 (原实现只挂在 err
                    # 分支 ⇒ 6 只漏检)。判据仍锚「末行显式全 0」不放宽。
                    if _maybe_mark_suspended(conn, code_done, end_date, records=records):
                        logger.info(
                            f"kline: {code_done} 末行退化 (全 0), "
                            f"suspended_codes auto 入表")
                    batch.extend(records)
                    total += count
                    if len(batch) >= 500:
                        repo.kline.batch_upsert(batch)
                        batch = []
            if batch:
                repo.kline.batch_upsert(batch)

        # W2-1 丙案: 双指标 — full_universe(is_active 全量, 5816 量级) observed
        # coverage + eligible gate; unknown_gap 显形计数随 metadata 上报, 不静默。
        # eligible 判据在 F2.5 (orchestrator) 侧, 此处只提供 unknown_gap 显形数据。
        n_unknown = len(unknown_gaps) if 'unknown_gaps' in dir() else 0
        status = "success" if not errors else ("partial" if total > 0 else "failed")
        # L3: exempt 计数随 metadata 上报 — partial 少了但没人知道少了谁=假绿
        # (判据#163/#153); exempt 名单每日可见可对拍 (partial 下降数=exempt 数)。
        # 刀3 (spec EQ_WIRE, 重定义): 当日格到位计数 — total 里混着越窗前旧口径的
        # 行 (10-07 实测 eq 越窗行撞 PK 同值覆盖, total=5224 但当日格 0 行)。
        # status 不动 (最小面), end_date 行数进 metadata + 零行 WARNING 显形,
        # 下游 freshness/valuation_backfill 已有独立报警, 不双发。
        end_date_rows = 0
        try:
            if conn is not None:
                end_date_rows = conn.execute(
                    "SELECT COUNT(*) FROM daily_klines WHERE date = ?",
                    (end_date,)).fetchone()[0]
        except Exception:
            pass
        if end_date_rows == 0:
            logger.warning(
                f"kline: end_date {end_date} 当日格 0 行 "
                f"(total={total} 行全为非 end_date 日期, 越窗/假日?) — "
                f"当日格缺失显形")
        # L3: suspended exempt 对拍锚 — 本轮命中间表的 unknown_gap 码中, 有多少
        # 在 suspended_codes 表内 (exempt 面)。partial 下降数应 = 该计数,
        # 防「partial 少了但没人知道少了谁」假绿 (判据#163/#153)。
        _exempt_codes = []
        try:
            for _c in (unknown_gaps if 'unknown_gaps' in dir() else []):
                if _is_suspended(conn, _c):
                    _exempt_codes.append(_c)
        except Exception:
            pass
        return PipelineResult(name=self.name, date=input.date, status=status,
            records_written=total, errors=errors,
            metadata={"start_date": start_date, "end_date": end_date,
                      "bj_legacy_skipped": bj_skipped,
                      "unknown_gap_count": n_unknown,
                      "suspended_exempt_count": len(_exempt_codes),
                      "suspended_exempt_codes": _exempt_codes[:50],
                      "end_date_rows": end_date_rows,
                      "unknown_gap_codes": (unknown_gaps[:50] if 'unknown_gaps' in dir() else []),
                      # empty 三态卡: 源返空形态分桶 (EMPTY/ERROR/NONE) —
                      # 下游 freshness/pipeline_runs 可机器判读「源没数据」
                      # vs 「源炸了」vs 「源无凭证」, 不再一句抹平。
                      "empty_forms": empty_forms})
