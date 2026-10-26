"""Weight Optimizer — IC-driven factor weight optimization"""

class WeightOptimizer:
    def optimize(self, current_weights: dict, ic_history: list[dict]) -> dict:
        weights = dict(current_weights)
        for entry in ic_history[-20:]:
            factor = entry.get("factor", "")
            ic = entry.get("ic", 0)
            if factor in weights:
                if ic < -0.05:
                    weights[factor] *= 0.5
                elif ic > 0.1:
                    weights[factor] = min(weights[factor] * 1.1, 0.5)
        total = sum(weights.values()) or 1.0
        return {k: round(v / total, 4) for k, v in weights.items()}
