"""FactorEngine — 预取数据 + 3层缓存 + 计算因子 + 清洗"""
import math
from concurrent.futures import ProcessPoolExecutor

from common.logging import get_logger
from strategy_layer.cache import FactorCache
from strategy_layer.factors import ALL_FACTORS
from strategy_layer.factors.base import FactorContext, FactorResult
from strategy_layer.factors.factor_cleaning import mad_clip, zscore

logger = get_logger("strategy.engine")


def _arm_pdeathsig() -> None:
    """Q3 孤儿防护: 子进程启动时武装 PR_SET_PDEATHSIG(SIGKILL)。

    父进程死亡(干净退出/被外部 kill -9)时, spawn 出的 worker 会被 reparent 到
    init 继续跑并持 SQLite 锁 — 08-21 ghost 持锁 6h47m 的真实机制。watchdog 的
    timeout 超时路径本就整组杀(覆盖超时), 但覆盖不到「父死而非超时」路径;
    PDEATHSIG 让 worker 在父死时自杀, 补齐该缺口。

    注意: 不动进程组(不加 setsid) — setsid 会把 worker 挪出 timeout 的进程组,
    反而废掉超时路径已有的整组杀。PDEATHSIG 与组杀正交, 双保险。
    竞态守卫: 若武装前父已死, 信号永不触发 => 主动退出。
    """
    try:
        import ctypes
        import os
        orig_ppid = os.getppid()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        SIGKILL = 9
        libc.prctl(PR_SET_PDEATHSIG, SIGKILL)
        if os.getppid() != orig_ppid:
            os._exit(0)  # 父已死, 自杀防孤儿
    except Exception:
        # 武装失败不阻断因子计算 (降级为无 PDEATHSIG, 与现状一致)
        logger.warning("PDEATHSIG 武装失败, worker 孤儿防护降级", exc_info=True)


class CleanedFactorMatrix:
    """清洗后的因子矩阵: {code: {factor_name: score}}"""
    def __init__(self, date: str, data: dict[str, dict[str, float]]):
        self.date = date
        self.data = data

    def items(self):
        return self.data.items()

    def __len__(self):
        return len(self.data)

    def __getitem__(self, code):
        return self.data.get(code, {})

    def __contains__(self, code):
        return code in self.data

    def __iter__(self):
        return iter(self.data)


def _compute_chunk_worker(args):
    """子进程 worker: 计算一批 codes 的全部因子 (模块级以便 ProcessPoolExecutor pickle)。

    在子进程内重建独立 DataLayer/FactorEngine, 避免跨进程共享父进程的
    DB 连接与内存缓存; 返回 list[FactorResult] 由父进程聚合清洗。
    """
    _arm_pdeathsig()  # Q3: 父死自杀, 防孤儿持锁 (spawn 子进程首行武装)
    codes, date, mode, db_path, max_window = args
    from common.database import Database
    from data_layer import DataLayer

    db = Database(db_path)
    dl = DataLayer(db)
    engine = FactorEngine(data_layer=dl, mode=mode, db=db)
    results: list[FactorResult] = []
    for code in codes:
        ctx = engine.build_context(code, date, max_window)
        for factor in engine.factors:
            try:
                results.append(factor.compute(ctx))
            except Exception:
                results.append(FactorResult(code=code, date=date, factor_name=factor.name))
    return results


class FactorEngine:
    def __init__(self, data_layer=None, factors=None, mode: str = "live", db=None):
        self.data_layer = data_layer
        self.factors = factors or [cls() for cls in ALL_FACTORS]
        self._mode = mode
        self.cache = FactorCache(db=db)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str):
        self._mode = mode

    def compute_all(self, codes: list[str], date: str, workers: int = 1) -> CleanedFactorMatrix:
        logger.info(f"FactorEngine.compute_all mode={self._mode}, stocks={len(codes)}, date={date}, workers={workers}")
        max_window = max(f.window_size for f in self.factors) if self.factors else 25
        factor_results: list[FactorResult] = []

        # 并行路径: 按 CPU 核数分批, 子进程计算 (冷启动全量重算单线程约 99 分钟, 4 核约 25 分钟)
        if workers and workers > 1 and len(codes) >= workers * 10:
            db_path = None
            if self.cache._db is not None:
                db_path = getattr(self.cache._db, "db_path", None)
            if db_path is None and self.data_layer is not None:
                db_path = getattr(getattr(self.data_layer, "db", None), "db_path", None)
            if db_path:
                chunk = (len(codes) + workers - 1) // workers
                tasks = [(codes[i:i + chunk], date, self._mode, db_path, max_window)
                         for i in range(0, len(codes), chunk)]
                try:
                    import multiprocessing as _mp
                    # 必须用 spawn: fork 会继承父进程已打开的 SQLite WAL 连接,
                    # 导致子进程首次查询返回空数据(因子全 0); spawn 保证子进程干净
                    _spawn_ctx = _mp.get_context("spawn")
                    with ProcessPoolExecutor(max_workers=workers, mp_context=_spawn_ctx) as ex:
                        for part in ex.map(_compute_chunk_worker, tasks):
                            factor_results.extend(part)
                    # ST-12: 并行路径补缓存写入 (S-18) — 子进程结果回父进程后批量 cache.put,
                    # 与串行路径一致; 否则 workers>1 时三层缓存完全失效, 每日全量重算。
                    # (put 内部跳过 score 非数值的结果, 失败项不污染缓存)
                    for r in factor_results:
                        self.cache.put(r.code, r.date, r.factor_name, r)
                    self.cache._stats["compute"] = sum(
                        1 for r in factor_results if r.score is not None or r.raw_value is not None)
                    return self._clean(factor_results, date)
                except Exception as e:
                    logger.warning(f"并行因子计算失败 ({e}), 回退串行")
                    factor_results = []
            else:
                logger.warning("并行因子计算跳过: 无法解析 db_path, 使用串行")

        # 串行路径 (原逻辑, 保留为回退)
        for code in codes:
            ctx = self.build_context(code, date, max_window)
            for factor in self.factors:
                # Layer 1+2: Check cache
                cached = self.cache.get(code, date, factor.name)
                if cached is not None:
                    factor_results.append(cached)
                    continue

                # Layer 3: Compute from scratch
                try:
                    result = factor.compute(ctx)
                    self.cache.put(code, date, factor.name, result)
                    factor_results.append(result)
                except Exception as e:
                    logger.warning(f"Factor {factor.name} failed for {code}: {e}")
                    factor_results.append(FactorResult(code=code, date=date, factor_name=factor.name))

        self.cache._stats["compute"] = sum(1 for r in factor_results if r.score is not None or r.raw_value is not None)
        return self._clean(factor_results, date)

    def _fetch_shortterm_data(self, date: str) -> dict:
        """ST-STYLE 市场级盘口数据 — limit_up_ladder 当日行 + auction_snapshot 当日行。
        每日一次查询, 全部股票共享 (市场级因子 + 个股命中查表)。
        """
        if not self.data_layer or not hasattr(self.data_layer, "db"):
            return {}
        conn = self.data_layer.db.get_connection()
        out: dict = {}
        try:
            rows = conn.execute(
                "SELECT trade_date, code, board_num, board_level FROM limit_up_ladder "
                "WHERE snapshot_date = ?", (date,)).fetchall()
            out["ladder_rows"] = [dict(r) for r in rows]
            if rows:
                out["max_board_num"] = max((r["board_num"] or 0) for r in rows)
        except Exception:
            pass
        try:
            arows = conn.execute(
                "SELECT code, auction_pct, auction_volume FROM auction_snapshot "
                "WHERE date = ?", (date,)).fetchall()
            out["auction_rows"] = [dict(r) for r in arows]
        except Exception:
            pass
        return out

    def build_context(self, code: str, date: str, window: int) -> FactorContext:
        """构建因子计算上下文 — 从 DataLayer 获取历史数据"""
        klines = []
        financials = []
        valuations = []
        moneyflow = []
        concept_data = []
        lhb_data = []
        analyst_data = []
        sentiment_data = {}
        shortterm_data: dict = {}  # 初始化须在 if 块外 — 无 data_layer 时 return 仍引用之

        if self.data_layer:
            try:
                history = self.data_layer.get_history(code, date, window)
                klines = history.klines
                financials = history.financials
                valuations = history.valuations
                moneyflow = history.moneyflow
            except Exception as e:
                logger.warning(f"Failed to fetch data for {code}: {e}")

            try:
                concept_data = [{"concept_name": c.concept_name, "change_pct": c.change_pct} for c in self.data_layer.get_concept_data(code, date)]
            except Exception as e:
                logger.warning(f"Failed to fetch concept data for {code}: {e}")
            try:
                lhb_data = [{"net_buy_amount": lhb_item.net_buy_amount, "drama_score": lhb_item.drama_score} for lhb_item in self.data_layer.get_lhb_data(code, date)]
            except Exception as e:
                logger.warning(f"Failed to fetch LHB data for {code}: {e}")
            try:
                analyst_data = [{"consensus_score": a.consensus_score, "target_price": a.target_price, "rating": a.rating} for a in self.data_layer.get_analyst_forecasts(code, date)]
            except Exception as e:
                logger.warning(f"Failed to fetch analyst data for {code}: {e}")

            # ST-STYLE (sm-25d60e49f5e046d3): 市场级盘口数据预取 — 每股共享, 一次查询
            try:
                st = self._fetch_shortterm_data(date)
                if st:
                    shortterm_data = st
            except Exception as e:
                logger.warning(f"shortterm_data 预取失败 (降级为空): {e}")
            sentiment_data = {}
            try:
                sd = self.data_layer.get_sentiment_data(date)
                if sd:
                    from dataclasses import asdict
                    sentiment_data = asdict(sd)
            except Exception:
                pass

        return FactorContext(
            code=code, date=date, klines=klines, financials=financials,
            valuations=valuations, moneyflow=moneyflow,
            concept_data=concept_data, lhb_data=lhb_data, analyst_data=analyst_data,
            sentiment_data=sentiment_data,
            shortterm_data=shortterm_data,
            sector=self.data_layer.get_sector(code) if self.data_layer else "",
        )

    def _clean(self, raw: list[FactorResult], date: str) -> CleanedFactorMatrix:
        """清洗: MAD 去极值 + z-score 标准化。

        卡IV后半 (P1-15): 本方法为现产签名, 行为不变 (开关门控的
        中性化在 _clean_with_config; None cfg 路径零字节差异)。
        """
        return self._clean_impl(raw, date, neutralize=False)

    def set_neutralize_repo(self, repo) -> None:
        """注入行业+市值双源 (中性化数据依赖)。

        协议 (两方法):
          repo.get_industry_map(codes, date) -> dict[code, str]
          repo.get_market_caps(codes, date) -> dict[code, float]
        生产适配器读 stock_info.industry + stock_valuation.market_cap;
        未注入而开关开 → 诚实直通 (数据不可得不冒充中性化)。
        """
        self._neutralize_repo = repo

    def _clean_with_config(self, raw: list[FactorResult], date: str,
                           cfg) -> CleanedFactorMatrix:
        """开关门控清洗: strategy.factor_neutralize.enabled 开时
        在去极值后、标准化前插截面中性化 (教科书顺序)。"""
        enabled = False
        if cfg is not None:
            try:
                enabled = bool(cfg.get("strategy.factor_neutralize.enabled",
                                       False))
            except Exception:
                enabled = False
        return self._clean_impl(raw, date, neutralize=enabled)

    def _clean_impl(self, raw: list[FactorResult], date: str,
                    neutralize: bool = False) -> CleanedFactorMatrix:
        """清洗: MAD 去极值 → [中性化 (开关)] → z-score 标准化"""
        by_factor: dict[str, list[FactorResult]] = {}
        for r in raw:
            by_factor.setdefault(r.factor_name, []).append(r)

        cleaned: dict[str, dict[str, float]] = {}

        # 中性化数据一次取齐 (全体 codes, 行业+市值)
        ind_map: dict[str, str] = {}
        cap_map: dict[str, float] = {}
        if neutralize:
            repo = getattr(self, "_neutralize_repo", None)
            if repo is None:
                neutralize = False  # 数据源未注入, 诚实直通
                logger.warning(
                    "factor_neutralize: repo 未注入, 中性化降级直通")
            else:
                all_codes = sorted({r.code for r in raw})
                try:
                    ind_map = repo.get_industry_map(all_codes, date) or {}
                    cap_map = repo.get_market_caps(all_codes, date) or {}
                except Exception as e:
                    neutralize = False
                    logger.warning(f"factor_neutralize 数据源失败, "
                                   f"降级直通: {e}")

        from strategy_layer.factors.neutralize import neutralize_cross_section

        for fname, results in by_factor.items():
            valid = [r for r in results
                     if isinstance(r.score, (int, float))
                     and not isinstance(r.score, bool)
                     and math.isfinite(r.score)]
            if not valid:
                continue
            scores = [r.score for r in valid]
            codes = [r.code for r in valid]

            clipped = mad_clip(scores)
            if neutralize:
                clipped = neutralize_cross_section(
                    fname, clipped,
                    [ind_map.get(c, "") for c in codes],
                    [cap_map.get(c) for c in codes])
            standardized = zscore(clipped)

            for code, s in zip(codes, standardized):
                cleaned.setdefault(code, {})[fname] = s

        return CleanedFactorMatrix(date=date, data=cleaned)
