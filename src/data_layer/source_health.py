"""数据源健康检查 — 主动探测 + 数据层交叉验证 + 健康报告

DL-26 扩展: 原实现只做连通性检查 (is_available), 无法检测"调用成功但返回 0 行"
的静默失效 (fuyao limit_up/limit_down 连续 4 天返回 0 行, 零告警)。

数据层判据设计 (DL-26 核心判断):
  强信号 = 同一 data_type 的其他源有数据, 但本源返回 0 行。
  这正是 fuyao case: akshare 拿到 54 只涨停, fuyao 0 只 → fuyao 坏, 不是"没数据"。

假阳性防护 (统筹踩坑教训 — "数字对了, 含义错了"):
  1. 全部源都返回 0 行 → 不告警 (可能确实无数据: 极端行情无涨停/跌停股);
  2. 单源类型 (limit_ladder/skyrocket 等 fuyao 独占) 无法交叉验证 → 只查连通性;
  3. 非交易日跳过数据层探测 (无数据是正常的);
  4. limit_down 允许 0 行 (无跌停股是常态) — 仅当其他源有行时才判本源异常;
  5. 豁免标记: EXEMPT_DATA_TYPES 里的类型不上报 (如遗留审计缓存类)。

巡检频率分层 (DL-26 判断 2):
  - 便宜源 (fuyao/akshare/gtimg: 免费 HTTP, 无限额): 随 health_check 主动探测;
  - 昂贵源 (tushare 限额 / baostock 有状态登录+全局锁): 不主动探测,
    仅在实际管线调用失败时由 record_source_switch 标记。
  → 不新增 crontab, 接入现有 health_check (10:00/14:00/16:00), 每日 3 次。
  288 次/天 × 源数的方案会自己把源打限频, 弃用。
"""

import time
from datetime import datetime

from common.logging import get_logger
from data_layer.sources.registry import SourceRegistry

logger = get_logger("data.source_health")

# 主动探测的数据类型: 只选便宜源覆盖的类型 (fuyao/akshare 免费 HTTP)。
# kline_bulk/financial 等昂贵类型不主动探测 (tushare 限额/baostock 锁)。
PROBE_DATA_TYPES = ("limit_up", "limit_down")

# 单源独占类型 — 无法交叉验证, 数据层检查跳过 (只查连通性)。
# fuyao 独占: limit_ladder/limit_break/hot_stock_history/skyrocket/auction_*
# 这些类型 0 行无法区分"源坏"还是"确实无数据"。
SINGLE_SOURCE_TYPES = frozenset({
    "limit_ladder", "limit_break", "hot_stock_history", "skyrocket",
    "auction_benchmark", "auction_snapshot", "sector_fund_flow",
})

# 豁免类型 — 不上报数据层告警 (遗留/非实时写入)。
# 统筹踩坑: alpha_factors 已被代码注释标为「遗留审计缓存, S3 豁免」。
EXEMPT_DATA_TYPES = frozenset({"alpha_factors"})

# 允许 0 行的类型 (0 行不一定是故障)。
# limit_down: 无跌停股是常态; 但其他源有行时仍判本源异常 (交叉验证优先)。
ZERO_OK_TYPES = frozenset({"limit_down"})


def _is_trading_day(date: str) -> bool:
    """粗判交易日: 周末一定非交易日; 节假日由调用方传入的 date 本身保证。

    不查日历表 — 数据层探测只在 health_check 时跑, 传入的 date 通常是最近
    交易日。周末直接跳过探测即可避免大部分假阳性。
    """
    try:
        dt = datetime.strptime(date[:10], "%Y-%m-%d")
        return dt.weekday() < 5  # 0-4 = Mon-Fri
    except (ValueError, TypeError):
        return True  # 解析失败不跳过 (宁可多查不漏报)


class SourceHealthChecker:
    def __init__(self, registry: SourceRegistry, probe_timeout_s: float = 5.0):
        self._registry = registry
        self._probe_timeout = probe_timeout_s
        self._health: dict[str, dict] = {}

    # ---- 连通性层 (原有) ----
    def check_all(self) -> dict[str, dict]:
        results = {}
        for source in self._registry.get_all_sources():
            results[source.name] = self._check_one(source)
        self._health = results
        return results

    def _check_one(self, source) -> dict:
        start = time.time()
        try:
            available = source.is_available()
            elapsed = round(time.time() - start, 3)
            status = "healthy" if available else "unavailable"
            if available:
                logger.info(f"Source {source.name} OK ({elapsed}s)")
            else:
                logger.warning(f"Source {source.name} unavailable ({elapsed}s)")
            return {
                "status": status,
                "latency_s": elapsed,
                "last_check": time.time(),
                "capabilities": list(getattr(source, "capabilities", set())),
            }
        except Exception as e:
            elapsed = round(time.time() - start, 3)
            logger.warning(f"Source {source.name} error: {e} ({elapsed}s)")
            return {
                "status": "error",
                "latency_s": elapsed,
                "last_check": time.time(),
                "error": str(e),
                "capabilities": list(getattr(source, "capabilities", set())),
            }

    # ---- 数据层交叉验证 (DL-26 新增) ----
    def check_data_health(self, date: str) -> dict[str, dict]:
        """对 PROBE_DATA_TYPES 做跨源行数比较。

        返回 {data_type: {"sources": {name: row_count}, "suspect": [names],
                          "verdict": "ok"|"suspect"|"all_empty"|"skipped"}}

        判据:
          - 全部源 0 行 → all_empty (不告警, 可能确实无数据);
          - 部分源有行, 部分源 0 行 → 0 行的源列入 suspect;
          - 单源类型 / 豁免类型 / 非交易日 → skipped。
        """
        results: dict[str, dict] = {}

        if not _is_trading_day(date):
            logger.info(f"data health: {date} 非交易日 (周末), 跳过数据层探测")
            return results

        for data_type in PROBE_DATA_TYPES:
            if data_type in EXEMPT_DATA_TYPES:
                results[data_type] = {"verdict": "skipped", "reason": "exempt"}
                continue
            if data_type in SINGLE_SOURCE_TYPES:
                results[data_type] = {"verdict": "skipped", "reason": "single_source"}
                continue

            sources = self._registry.get_sources_for(data_type)
            if len(sources) < 2:
                # 只有一个可用源 → 无法交叉验证
                results[data_type] = {"verdict": "skipped", "reason": "single_source"}
                continue

            row_counts: dict[str, int] = {}
            for source in sources:
                try:
                    rows = source.get_data(data_type, {"date": date})
                    count = len(rows) if rows else 0
                    row_counts[source.name] = count
                except Exception as e:
                    logger.warning(
                        f"data health: {source.name} {data_type} probe error: {e}")
                    row_counts[source.name] = -1  # -1 = 调用失败 (区别于 0 行)

            # 判定
            positive = {n: c for n, c in row_counts.items() if c > 0}
            zero = [n for n, c in row_counts.items() if c == 0]
            failed = [n for n, c in row_counts.items() if c < 0]

            if not positive:
                # 全部 0 行或失败 → 可能确实无数据 (不告警)
                verdict = "all_empty"
                suspect = []
                if data_type in ZERO_OK_TYPES:
                    logger.info(
                        f"data health: {data_type} 全部源 0 行 "
                        f"({data_type} 允许 0 行, 不告警)")
                else:
                    logger.warning(
                        f"data health: {data_type} 全部源 0 行 — "
                        f"可能确实无数据, 也可能全部源故障 (不告警, 留观)")
            else:
                # 有源有数据 → 0 行的源是 suspect
                suspect = zero + failed
                verdict = "suspect" if suspect else "ok"
                if suspect:
                    logger.warning(
                        f"data health: {data_type} 交叉验证异常 — "
                        f"有数据源: {positive}, 0行/失败源: {suspect}")

            results[data_type] = {
                "verdict": verdict,
                "sources": row_counts,
                "suspect": suspect,
                "date": date,
            }

        return results

    # ---- 报告 (原有) ----
    def get_health(self, source_name: str | None = None) -> dict:
        if source_name:
            return self._health.get(source_name, {})
        return self._health

    def get_healthy_sources(self) -> list[str]:
        return [name for name, h in self._health.items() if h.get("status") == "healthy"]

    def get_unhealthy_sources(self) -> list[str]:
        return [name for name, h in self._health.items() if h.get("status") != "healthy"]

    def summary(self) -> dict:
        if not self._health:
            self.check_all()
        all_sources = list(self._health.keys())
        healthy = self.get_healthy_sources()
        return {
            "total": len(all_sources),
            "healthy": len(healthy),
            "unhealthy": len(all_sources) - len(healthy),
            "healthy_sources": healthy,
            "unhealthy_sources": self.get_unhealthy_sources(),
        }
