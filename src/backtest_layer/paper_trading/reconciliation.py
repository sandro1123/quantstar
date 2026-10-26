"""backtest_layer/paper_trading/reconciliation.py — Reconciliation"""


class Reconciliation:
    def __init__(self):
        self.discrepancies: list[dict] = []

    def compare(self, paper_results: list[dict], backtest_results: list[dict]) -> list[dict]:
        self.discrepancies = []
        paper_map = {r.get("code", ""): r for r in paper_results}
        backtest_map = {r.get("code", ""): r for r in backtest_results}
        for code in set(paper_map) | set(backtest_map):
            p = paper_map.get(code, {})
            b = backtest_map.get(code, {})
            diff = {
                "code": code,
                "paper_return": p.get("total_return", 0),
                "backtest_return": b.get("total_return", 0),
                "return_diff": p.get("total_return", 0) - b.get("total_return", 0),
            }
            self.discrepancies.append(diff)
        return self.discrepancies
