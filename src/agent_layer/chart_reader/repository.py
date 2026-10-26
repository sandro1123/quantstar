# -*- coding: utf-8 -*-
"""chart_reader 结论落库 — chart_analysis 表仓储 (#101 切1)

层间契约 (§1.5): 09 答题工人只写自家结论表, 表即跨层界面;
唯一写者条款: 永不写 signal / pending_entry (那是 02 的地盘)。

source 枚举 (设计稿 §4):
- "atr_floor": LegA levels + 纯 ATR 公式带 (02 SignalGenerator 同式镜像, 切1 即产)
- "llm_chart": LLM 读图结论 (切2 产, 届时经通用 write 接口落库)
双录对照 = 同 (code, date) 两行并存, UNIQUE(code, date, source)。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.database import Database

# 02 SignalGenerator ATR 乘数镜像 (策略层源: signals/generator.py {entry:0.5, stop:2, profit:3})。
# §1.5 禁函数级跨层调用 → 常量镜像不走 import; 漂移由切3 对照评估段捕获 (atr_baseline 双录)。
ATR_ENTRY_MULT = 0.5
ATR_STOP_MULT = 2.0
ATR_PROFIT_MULT = 3.0


def atr_band(current_price: float, atr: float) -> dict:
    """02 同式公式带 (bit-exact): round(price ± mult*atr, 2)。

    atr<=0 / 价缺失 → 全 None (宁缺毋滥; 02 的 2% 退化路径不镜像, 09 只对照主公式)。
    """
    if current_price is None or atr is None or atr <= 0:
        return {"entry_low": None, "entry_high": None, "stop": None, "profit": None}
    return {
        "entry_low": round(current_price - ATR_ENTRY_MULT * atr, 2),
        "entry_high": round(current_price + ATR_ENTRY_MULT * atr, 2),
        "stop": round(current_price - ATR_STOP_MULT * atr, 2),
        "profit": round(current_price + ATR_PROFIT_MULT * atr, 2),
    }


class ChartAnalysisRepository:
    """chart_analysis 仓储。全部写入走本类, 幂等 (UNIQUE 冲突即替换)。"""

    def __init__(self, db: "Database"):
        self.db = db

    def write_baseline(self, code: str, date: str, levels: dict,
                       current_price: float, atr: float) -> None:
        """切1 落库: atr_floor 行 = LegA levels_json + ATR 公式带。"""
        band = atr_band(current_price, atr)
        conn = self.db.get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO chart_analysis "
            "(code, date, pattern, entry_low, entry_high, stop, profit, "
            " confidence, reasons, source, levels_json, atr_baseline, llm_raw) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, 'atr_floor', ?, ?, NULL)",
            (code, date, band["entry_low"], band["entry_high"], band["stop"],
             band["profit"], json.dumps(levels, ensure_ascii=False), atr),
        )
        conn.commit()

    def write_llm(self, code: str, date: str, pattern, entry_low, entry_high,
                 stop, profit, confidence, reasons, levels: dict,
                 atr: float, llm_raw) -> None:
        """切2 落库: llm_chart 行 — 合法→LLM 值; degraded→ATR 公式值 +
        pattern/confidence=NULL (判别键: pattern IS NULL ⇔ degraded, 双录留痕)。"""
        conn = self.db.get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO chart_analysis "
            "(code, date, pattern, entry_low, entry_high, stop, profit, "
            " confidence, reasons, source, levels_json, atr_baseline, llm_raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm_chart', ?, ?, ?)",
            (code, date, pattern, entry_low, entry_high, stop, profit,
             confidence, json.dumps(reasons, ensure_ascii=False),
             json.dumps(levels, ensure_ascii=False), atr, llm_raw),
        )
        conn.commit()

    def get(self, code: str, date: str, source: str):
        """取一行结论 (无则 None)。"""
        return self.db.get_connection().execute(
            "SELECT * FROM chart_analysis WHERE code=? AND date=? AND source=?",
            (code, date, source),
        ).fetchone()

    def count_for_date(self, date: str) -> int:
        """当日结论行数 (双录时代每 code 2 行)。"""
        return self.db.get_connection().execute(
            "SELECT COUNT(*) FROM chart_analysis WHERE date=?", (date,)
        ).fetchone()[0]
