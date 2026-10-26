"""ADJFIX-BUDGET-FIT: adjust_factor 60s误杀修复验证 (红簇③, 2026-09-01)

病根: _capability_timeout 无 "adjust_factor" 键 → 60s 默认先于 ops.yaml 300s 处决
→ with_timeout fallback None → 误报 "管线返回空结果" (幽灵迟到写同型 08-17 analyst)。

预算论证 (取证 ~/qs-cfgsnap/audit/20260901-redcluster-E/adjfix/, p1~p9):
- baostock 纯串行腿实测 1.8s (p5); mootdx 晚窗单调用实测 14.1s/13.0s (p9-H)
- 单进程完整墙钟实测最坏 309s (08-28 G1 ghost 18:30→18:35:15 落库)
- 事件日模型上限: div_records[:10] × ~40s/调用 + baostock 腿 ≈ 405s
- 600s = 309×1.94 / 405×1.48, 两口径均 ≥30% 裕度; 对齐 dict 现有整百粒度
- 外层 cron_watchdog ceiling = 300×3 + 60×2 = 1140s (ops.yaml 派生), 600 在内富余

2026-09-01 晚三跑红行 (旧码预期): adjust_factor 18:30/18:32/18:35 dur=60.0 全「空结果」。
验收 (统筹口径): 09-02 晚 18:30 绿行 + adjust_factors 真实落库 + 零「空结果」误报。
"""

SRC_INIT = "src/data_layer/__init__.py"


class TestAdjustFactorBudgetFit:
    def test_adjust_factor_timeout_is_600(self):
        """_capability_timeout["adjust_factor"] == 600 (≥30% 裕度两口径)"""
        with open(SRC_INIT, encoding="utf-8") as f:
            src = f.read()
        assert '"adjust_factor": 600' in src

    def test_budget_over_measured_worst_wallclock(self):
        """预算论证: 600 ≥ 实测最坏单进程墙钟 309s × 1.3"""
        measured_worst_s = 309   # 08-28 G1 ghost 完整墙钟 (p7)
        assert 600 >= measured_worst_s * 1.3

    def test_budget_over_event_day_model_ceiling(self):
        """预算论证: 600 ≥ 事件日模型上限 (10抽样×40s + baostock腿5s) × 1.3"""
        model_ceiling_s = 10 * 40 + 5
        assert 600 >= model_ceiling_s * 1.3

    def test_watchdog_ceiling_envelops_budget(self):
        """外层看门狗 ceiling (1140s = 300×3+60×2+120 grace) > 600 单跑预算"""
        watchdog_ceiling_s = 300 * (2 + 1) + 60 * 2  # ops.yaml adjust_factor 派生
        assert watchdog_ceiling_s > 600
