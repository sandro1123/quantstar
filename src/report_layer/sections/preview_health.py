# -*- coding: utf-8 -*-
"""明日预演+系统健康 (运维日报并入面, 3 行口径)。"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.preview_health")


class PreviewHealthSection(Section):
    name = "preview_health"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            lines = ["🗓 明日预演 + 系统健康", "=" * 40]
            # 明日预演: 池内待行动面
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM stock_pool WHERE status = 'watch' "
                    "AND (valid_until IS NULL OR valid_until >= ?)",
                    (context.date,)).fetchone()
                lines.append(f"明日观察：{row[0]} 只池内候选待量能与价区确认")
            except Exception:
                lines.append("明日观察：池面数据积累中")
            # 系统健康 2 行 (ops 并入, 精简)
            try:
                runs = conn.execute(
                    "SELECT COUNT(*) FROM pipeline_runs WHERE date = ? "
                    "AND status = 'failed'", (context.date,)).fetchone()[0]
                lines.append(f"系统健康：今日失败管道 {runs} 个"
                             + ("（已告警）" if runs else "（绿）"))
            except Exception:
                lines.append("系统健康：运行面数据积累中")
            lines.append("数据链：K 线/因子/估值按夜间窗时序更新，明晨核验")
            return SectionResult(title="🗓 明日预演 + 系统健康",
                                 body="\n".join(lines), name=self.name,
                                 sources=["stock_pool", "pipeline_runs"])
        except Exception as e:
            logger.warning(f"PreviewHealthSection build failed: {e}")
            return None
