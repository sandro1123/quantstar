"""RegimeSplit — 按市场状态拆分回测结果 (BT-08 接线)"""
from common.logging import get_logger

logger = get_logger("backtest.validation")


class RegimeSplit:
    def split(self, equity_curve: list[float], regimes: list[str]) -> dict[str, dict]:
        """原始接口: 按等长 nav/regime 序列拆分 (保留向后兼容)"""
        by_regime: dict[str, list[float]] = {}
        for nav, regime in zip(equity_curve, regimes):
            by_regime.setdefault(regime, []).append(nav)

        result = {}
        for regime, navs in by_regime.items():
            if len(navs) < 2:
                result[regime] = {"count": len(navs), "return": 0.0}
                continue
            total_return = (navs[-1] / navs[0] - 1) if navs[0] > 0 else 0
            result[regime] = {"count": len(navs), "return": total_return}
        return result

    def split_report(self, report, strategy_layer) -> dict:
        """BT-08: 从 BacktestReport + strategy_layer 读 API 生成 regime_breakdown

        Returns:
            dict: regime_breakdown 结构, 含 available/coverage/segments/note
        """
        # 1. strategy_layer 可用性
        if strategy_layer is None or not hasattr(strategy_layer, "get_daily_regime"):
            return {"available": False, "reason": "strategy_layer 未注入或无 get_daily_regime"}

        # 2. equity_curve 日期轴提取
        curve = report.equity_curve
        if not curve:
            return {"available": False, "reason": "equity_curve 为空"}
        # signal 模式 equity_curve 可能是纯 float 列表 (无日期轴)
        if isinstance(curve[0], (int, float)):
            return {"available": False, "reason": "equity_curve 无日期轴 (signal 模式)"}

        dates = [pt.get("date", "") for pt in curve]
        navs = [pt.get("nav", 0.0) for pt in curve]
        if not dates or not dates[0]:
            return {"available": False, "reason": "equity_curve 无日期轴"}

        # 3. 读 regime 数据 (走策略层读 API, 避免跨层裸 SQL)
        date_range = (dates[0], dates[-1])
        try:
            regime_rows = strategy_layer.get_daily_regime(date_range)
        except Exception as e:
            logger.warning(f"BT-08 get_daily_regime 异常: {e}")
            return {"available": False, "reason": f"get_daily_regime 异常: {e}"}

        total_days = len(dates)
        if not regime_rows:
            return {
                "available": False,
                "reason": f"regime 数据不足（覆盖 0/{total_days} 天）",
                "coverage": f"0/{total_days}",
            }

        # 4. 构建 date→regime 映射
        regime_map = {row["date"]: row["regime"] for row in regime_rows}

        # 5. 按 regime 分段统计
        by_regime: dict[str, list[float]] = {}
        covered = 0
        for date, nav in zip(dates, navs):
            regime = regime_map.get(date)
            if regime is None:
                continue
            covered += 1
            by_regime.setdefault(regime, []).append(nav)

        coverage_str = f"{covered}/{total_days}"
        segments = {}
        for regime, seg_navs in by_regime.items():
            if len(seg_navs) < 2:
                segments[regime] = {"days": len(seg_navs), "return": 0.0, "max_drawdown": 0.0}
                continue
            seg_return = (seg_navs[-1] / seg_navs[0] - 1) if seg_navs[0] > 0 else 0.0
            # 子序列 MDD
            peak = seg_navs[0]
            mdd = 0.0
            for v in seg_navs:
                peak = max(peak, v)
                dd = (peak - v) / peak if peak > 0 else 0.0
                mdd = max(mdd, dd)
            segments[regime] = {
                "days": len(seg_navs),
                "return": round(seg_return, 4),
                "max_drawdown": round(mdd, 4),
            }

        # 6. 交易数按 regime 归属 (entry_date 在 regime_map 中)
        trade_counts: dict[str, int] = {}
        for trade in (report.trade_log or []):
            entry = trade.get("entry_date", "")
            regime = regime_map.get(entry)
            if regime:
                trade_counts[regime] = trade_counts.get(regime, 0) + 1
        for regime in segments:
            segments[regime]["trades"] = trade_counts.get(regime, 0)

        # 7. 组装结果
        result = {
            "available": True,
            "coverage": coverage_str,
            "coverage_pct": round(covered / total_days, 4) if total_days > 0 else 0.0,
            "segments": segments,
        }
        if covered < total_days:
            result["note"] = f"regime 数据部分覆盖（{coverage_str} 天），未覆盖日期未纳入拆分"
        return result


def attach_regime_breakdown(report, strategy_layer) -> None:
    """BT-08: 统一附加点 — 将 regime_breakdown 写入 report (异常降级不阻塞)"""
    try:
        report.regime_breakdown = RegimeSplit().split_report(report, strategy_layer)
    except Exception as e:
        logger.warning(f"BT-08 regime 拆分异常(诚实降级): {e}")
        report.regime_breakdown = {"available": False, "reason": f"regime 拆分异常: {e}"}
