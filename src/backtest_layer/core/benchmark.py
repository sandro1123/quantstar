"""backtest_layer/core/benchmark.py — 基准对比 (沪深300 / 创业板指)

BT-04 真接线 (B-02/B-04 修复): 基准曲线改从 data_layer.get_index_daily
(DL-23 指数日线读 API, 契约 get_index_daily(code, start_date, end_date) →
[{date, close, ...}] 升序) 真实获取; 基准默认沪深300, 可经 config
backtest.benchmark_index 配置。

核心纪律 — "宁可没有不可是假的": 指数数据缺失/不足 (数据层缺读 API、
表未入库、窗口无数据、点数<2) 时, 基准系字段一律降级 None 并标注
"基准不可用", 绝不回落 0 假值、绝不再产出 excess_return=total_return 的假超额。
"""
from common.logging import get_logger

logger = get_logger("backtest.benchmark")

# BT-04: 基准名 → 指数代码映射 (DL-23 index_daily 入库码型)
INDEX_CODES = {
    "csi300": "000300.SH",
    "hs300": "000300.SH",
    "000300.sh": "000300.SH",
    "chinext": "399006.SZ",
    "cyb": "399006.SZ",
    "399006.sz": "399006.SZ",
}

# BT-04: 非指数基准占位 (旧默认值), 一律回落 config/默认沪深300
_NON_INDEX_PLACEHOLDERS = ("", "auto", "equal_weight")
DEFAULT_BENCHMARK = "csi300"
BENCHMARK_UNAVAILABLE_NOTE = "基准不可用"


def resolve_benchmark_name(explicit, data_layer) -> str:
    """BT-04: 基准名解析 — 显式指定 > config backtest.benchmark_index > 默认沪深300。

    explicit 为空/"auto"/"equal_weight"(旧占位) 时读 config;
    config 缺失/非法值时回落默认沪深300。
    """
    if isinstance(explicit, str) and explicit not in _NON_INDEX_PLACEHOLDERS:
        return explicit
    config = getattr(data_layer, "config", None) if data_layer is not None else None
    if config is not None:
        try:
            name = config.get("backtest.benchmark_index", DEFAULT_BENCHMARK)
            if isinstance(name, str) and name:
                return name
        except Exception:
            pass
    return DEFAULT_BENCHMARK


def _max_drawdown(navs: list[float]) -> float:
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


class Benchmark:
    def __init__(self, name: str = DEFAULT_BENCHMARK):
        self.name = name if isinstance(name, str) else DEFAULT_BENCHMARK
        self.index_code = INDEX_CODES.get(self.name.lower())
        # 未在映射表中的裸指数代码 (如 config 直接给 000905.SH) 原样透传
        if self.index_code is None and "." in self.name:
            self.index_code = self.name

    def calc_excess_return(self, strategy_return: float, benchmark_return: float) -> float:
        return strategy_return - benchmark_return

    def calc_information_ratio(self, excess_returns: list[float]) -> float:
        if len(excess_returns) < 2:
            return 0.0
        mean = sum(excess_returns) / len(excess_returns)
        var = sum((r - mean) ** 2 for r in excess_returns) / (len(excess_returns) - 1)
        std = var ** 0.5
        return mean / std if std > 0 else 0.0

    def fetch_benchmark_curve(self, date_range: tuple[str, str], data_layer=None) -> list[dict]:
        """BT-04: 取基准日线净值曲线 (首日归一为 1.0)。

        真数据源 = data_layer.get_index_daily (DL-23)。任何不可用情形
        (无数据层/无读 API/查询异常/空数据/有效点数<2) 返回空列表,
        由调用方诚实降级 — 不造假曲线。
        """
        curve, _ = self._fetch_with_reason(date_range, data_layer)
        return curve

    def _fetch_with_reason(self, date_range: tuple[str, str], data_layer) -> tuple[list[dict], str]:
        if data_layer is None:
            return [], "数据层未提供"
        if self.index_code is None:
            return [], f"未知基准名'{self.name}'"
        api = getattr(data_layer, "get_index_daily", None)
        if not callable(api):
            return [], "数据层无 get_index_daily 读 API (DL-23 未合并/未启用)"
        try:
            rows = api(self.index_code, start_date=date_range[0], end_date=date_range[1])
        except Exception as e:
            return [], f"指数查询异常: {e}"
        if not isinstance(rows, (list, tuple)) or not rows:
            return [], f"指数数据缺失 ({self.index_code} 窗口内无记录, index_daily 或未完成生产入库)"
        points: list[tuple[str, float]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            date = r.get("date")
            close = r.get("close")
            if date and isinstance(close, (int, float)) and not isinstance(close, bool) and close > 0:
                points.append((str(date), float(close)))
        if len(points) < 2:
            return [], f"指数点数不足 ({len(points)}<2, 无法构成收益曲线)"
        points.sort(key=lambda p: p[0])
        base = points[0][1]
        return [{"date": d, "nav": c / base} for d, c in points], ""

    def attach_to_report(self, report, data_layer) -> bool:
        """BT-04: 为回测报告附加真实基准对比 (单一权威接线点)。

        指数可得时:
          - benchmark_return = 指数窗口真实收益;
          - excess_return    = 策略 total_return - benchmark_return (真实超额);
          - 若报告净值曲线与指数日期重叠≥2 天, 再真算 IR 与相对最大回撤
            (超额净值曲线 MDD); 否则二者 None (拒绝编造)。
        指数不可得: 全部基准系字段 None, benchmark_comparison 标
        available=False + 原因, 警告进 data_quality_warnings。
        返回 True 表示基准已附加。
        """
        curve, reason = self._fetch_with_reason(report.date_range, data_layer)
        if not curve:
            report.benchmark_return = None
            report.excess_return = None
            report.information_ratio = None
            report.benchmark_max_drawdown = None
            report.excess_max_drawdown = None
            report.benchmark_index = self.index_code or ""
            report.benchmark_comparison = {
                "available": False,
                "benchmark": self.name,
                "index_code": self.index_code or "",
                "reason": reason,
            }
            warn = f"{BENCHMARK_UNAVAILABLE_NOTE}: {reason}"
            if isinstance(getattr(report, "data_quality_warnings", None), list):
                if warn not in report.data_quality_warnings:
                    report.data_quality_warnings.append(warn)
            return False

        bnav = [e["nav"] for e in curve]
        bmap = {e["date"]: e["nav"] for e in curve}
        benchmark_return = bnav[-1] / bnav[0] - 1.0
        report.benchmark_return = round(benchmark_return, 6)
        report.benchmark_max_drawdown = round(_max_drawdown(bnav), 6)
        report.benchmark_index = self.index_code
        # 超额 = 策略真实收益 - 基准真实收益 (同窗口标量, 恒可真算)
        report.excess_return = round(report.total_return - benchmark_return, 6)

        # IR / 相对最大回撤需逐日对齐, 无日期净值曲线时降级 None
        strat_points: list[tuple[str, float]] = []
        for e in getattr(report, "equity_curve", None) or []:
            if isinstance(e, dict) and e.get("date"):
                nav = e.get("nav")
                if isinstance(nav, (int, float)) and not isinstance(nav, bool) and nav > 0:
                    strat_points.append((str(e["date"]), float(nav)))
        smap = dict(strat_points)
        common = sorted(d for d, _ in strat_points if d in bmap)

        excess_mdd = None
        ir = None
        note = ""
        if len(common) >= 2:
            s0 = smap[common[0]]
            ratio_curve = [(smap[d] / s0) / (bmap[d] / bmap[common[0]]) for d in common]
            excess_mdd = round(_max_drawdown(ratio_curve), 6)
            daily_excess = [
                (smap[common[i]] / smap[common[i - 1]] - 1.0)
                - (bmap[common[i]] / bmap[common[i - 1]] - 1.0)
                for i in range(1, len(common))
            ]
            if len(daily_excess) >= 2:
                ir = round(self.calc_information_ratio(daily_excess), 4)
        else:
            note = "策略净值曲线无日期/与基准重叠不足 (IR/相对回撤不可算, 拒绝编造)"

        report.excess_max_drawdown = excess_mdd
        report.information_ratio = ir

        comparison = {
            "available": True,
            "benchmark": self.name,
            "index_code": self.index_code,
            "strategy_return": report.total_return,
            "benchmark_return": report.benchmark_return,
            "excess_return": report.excess_return,
            "information_ratio": ir,
            "benchmark_max_drawdown": report.benchmark_max_drawdown,
            "excess_max_drawdown": excess_mdd,
            "aligned_days": len(common),
            "benchmark_start": curve[0]["date"],
            "benchmark_end": curve[-1]["date"],
        }
        if note:
            comparison["note"] = note
        report.benchmark_comparison = comparison
        return True

    def align_to_strategy(self, benchmark_curve: list[dict], strategy_curve: list[dict]) -> tuple[list[float], list[float]]:
        """将基准曲线与策略曲线按日期对齐，返回 (benchmark_navs, strategy_navs)。"""
        if not benchmark_curve or not strategy_curve:
            return [], []
        bench_map = {e.get("date", ""): e.get("nav", 1.0) for e in benchmark_curve}
        strat_map = {e.get("date", ""): e.get("nav", 1.0) for e in strategy_curve}
        common_dates = sorted(set(bench_map) & set(strat_map))
        if not common_dates:
            return [], []
        return [bench_map[d] for d in common_dates], [strat_map[d] for d in common_dates]

    def calc_benchmark_metrics(self, benchmark_curve: list[dict]) -> dict:
        """计算基准曲线的收益指标。"""
        if not benchmark_curve or len(benchmark_curve) < 2:
            return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0}
        navs = [e.get("nav", 1.0) for e in benchmark_curve]
        total_return = (navs[-1] / navs[0] - 1) if navs[0] > 0 else 0.0
        peak = navs[0]
        max_dd = 0.0
        for v in navs:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        days = len(navs)
        annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 else 0.0
        return {
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_dd, 4),
        }

    def comparison_summary(self, strategy_metrics: dict, benchmark_metrics: dict) -> dict:
        """生成策略 vs 基准对比摘要。"""
        excess = strategy_metrics.get("total_return", 0) - benchmark_metrics.get("total_return", 0)
        return {
            "strategy_return": strategy_metrics.get("total_return", 0),
            "benchmark_return": benchmark_metrics.get("total_return", 0),
            "excess_return": round(excess, 4),
            "strategy_sharpe": strategy_metrics.get("sharpe", 0),
            "benchmark_max_dd": benchmark_metrics.get("max_drawdown", 0),
        }
