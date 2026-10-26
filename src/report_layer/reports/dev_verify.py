"""Dev验证报告 — 预算内轻量验证切片 (案A重设计 2026-09-02)

旧版 DevCompareSection 调 strategy.run_daily 全量链 (factor→…→signal),
与 60s 预算错配 — 4 天 6 连整秒超时 (08-27~09-01 pipeline_runs 实证)。
重设计原则: dev_verify 的本职是「验证」, 不是「重跑」—
核心链当日产出新鲜度直查 (factor_daily/daily_klines/index_daily/
stock_valuation/stock_pool 五核心面), 零 run_daily、零计算链。

旧 DevCompareSection 类保留 (存量测试 test_dev_verify_builder_serializes
仍消费), 仅移出编排。

墙钟预算: 直查面实测 <1s (worktree qs-dvl/logs/dv_smoke_*.txt),
orchestrator 60s 预算裕度 >98%; 设计目标墙钟 ≤40s 的富余即来自
不再镜像全量链本身, 而非调大预算。
"""
from delivery_layer.message import Message
from report_layer.reports.base import ReportBuilder
from report_layer.sections.base import Section, SectionResult

# 核心链验证面: (表名, 日期列, 语义) — 当日行数>0 且 MAX(日期列) 即绿。
# stock_pool 无 date 列 (entry_date/created_at 语义不同), 单独口径: 总行数>0。
# 周末/休市由下游 freshness_audit 判定口径兜底, 本切片只做当日截面。
CORE_TABLES = [
    ("factor_daily", "date", "因子日面"),
    ("daily_klines", "date", "K线日面"),
    ("stock_valuation", "date", "估值日面"),
    ("index_daily", "date", "指数日面"),
]


class DevVerifyBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "dev_verify"

    @property
    def title(self) -> str:
        return "🔬 QuantStar Dev 环境验证（轻量切片）"

    @property
    def severity(self) -> str:
        return "info"

    def sections(self) -> list:
        return [DevLightweightSection()]

    def build(self, context) -> Message | None:
        return super().build(context)


class DevLightweightSection(Section):
    """预算内验证切片: 五核心表当日行数+MAX(date) 直查, 零 run_daily。"""

    name = "dev_lightweight"

    def build(self, context) -> SectionResult | None:
        body_lines = [f"验证日期: {context.date}"]

        data = getattr(context, "data_layer", None)
        conn = None
        try:
            if data is not None and hasattr(data, "get_connection"):
                conn = data.get_connection()
        except Exception as e:
            body_lines.append(f"\n❌ 数据层不可用: {e}")
            return SectionResult(name=self.name, title="核心链当日截面", body="\n".join(body_lines))

        if conn is None:
            body_lines.append("\n❌ 无法获取数据库连接")
            return SectionResult(name=self.name, title="核心链当日截面", body="\n".join(body_lines))

        body_lines.append("\n📊 核心链当日截面 (直查, 非重跑):")
        degraded = 0
        for table, date_col, label in CORE_TABLES:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*), MAX({date_col}) FROM {table} WHERE {date_col}=( "
                    f"SELECT MAX({date_col}) FROM {table} )"
                ).fetchone()
                count, max_date = (row[0], row[1]) if row else (0, None)
            except Exception as e:
                body_lines.append(f"  ❌ {label}: 查询失败 {e}")
                degraded += 1
                continue
            if count is not None and count > 0:
                body_lines.append(f"  ✅ {label}: {count} 行 @ {max_date}")
            else:
                body_lines.append(f"  ⚠ {label}: 零行 (最新 {max_date})")
                degraded += 1

        if degraded:
            body_lines.append(f"\n⚠ {degraded} 项核心面降级 — 详见 16:22 freshness_audit")
        else:
            body_lines.append("\n✅ 四核心日面全绿")

        # 股票池单独口径: 无 date 列, 总行数>0 即绿 (活仓/候选台账面)
        try:
            pool_n = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
            body_lines.append(f"\npool 台账: {pool_n} 行")
        except Exception as e:
            body_lines.append(f"\n❌ 股票池: 查询失败 {e}")

        return SectionResult(name=self.name, title="核心链当日截面", body="\n".join(body_lines))


# ---- 旧版保留 (存量测试 test_dev_verify_builder_serializes 消费; 已移出编排) ----

class DevCompareSection(Section):
    name = "dev_compare"

    def build(self, context) -> SectionResult | None:

        body_lines = [f"对比日期: {context.date}"]

        try:
            strategy = getattr(context, 'strategy_layer', None)
            if not strategy:
                body_lines.append("\n❌ 无法获取 StrategyLayer 实例")
                return SectionResult(name=self.name, title="Dev vs Prod 对比", body="\n".join(body_lines))
            cache_key = f"run_daily_{context.date}"
            if cache_key not in context._cache:
                context._cache[cache_key] = strategy.run_daily(context.date)
            result = context._cache[cache_key]
            signals = result.signals
            body_lines.append("\n📊 策略运行结果:")
            body_lines.append(f"  股票池: {result.stock_count} 只")
            body_lines.append(f"  生成信号: {len(signals)} 个")
            body_lines.append(f"  市场状态: {result.regime}")
            if signals:
                body_lines.append("\n🏆 Top 5 候选:")
                for s in signals[:5]:
                    body_lines.append(f"  {s.code}: {s.action} (强度: {s.strength:.1%})")
        except Exception as e:
            body_lines.append(f"\n❌ 对比失败: {e}")

        return SectionResult(name=self.name, title="Dev vs Prod 对比", body="\n".join(body_lines))
