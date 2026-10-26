"""Built-in analyzers (G-E12, G-E13)"""

import statistics

from execution_layer.analysis.base_analyzer import BaseAnalyzer


class SQNAnalyzer(BaseAnalyzer):
    """System Quality Number (G-E13): SQN = (avg_trade_return / std_trade_return) * sqrt(num_trades)"""

    def name(self) -> str:
        return "System Quality Number (SQN)"

    def analyze(self) -> dict:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL"
        ).fetchall()
        returns = [r["total_return_pct"] for r in rows]
        if len(returns) < 10:
            return {"sqn": 0, "quality": "样本不足", "num_trades": len(returns)}
        avg_ret = statistics.mean(returns)
        std_ret = statistics.stdev(returns) if len(returns) > 1 else 1
        if std_ret == 0:
            std_ret = 1
        sqn = (avg_ret / std_ret) * (len(returns) ** 0.5)
        if sqn > 6.0:
            quality = "优秀"
        elif sqn > 3.0:
            quality = "良好"
        elif sqn > 1.5:
            quality = "一般"
        else:
            quality = "较差"
        return {"sqn": round(sqn, 2), "quality": quality, "num_trades": len(returns)}
