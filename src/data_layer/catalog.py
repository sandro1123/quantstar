"""data_layer/catalog.py — DL-30 数据目录: 从代码/注册表自动派生, 不手工维护

信息源全部复用现成注册表与常量 (不建第二份清单):
- DataLayer 公开读方法: inspect 反射 + docstring 首行 (签名/返回类型自动派生)
- PipelineRegistry.get_all(): 管道 name/requires/produces
- DataReadiness.REQUIRED_TABLES / COLLECT_TABLES: 表清单 + 日期列
- freshness_audit._days_behind: 新鲜度判据 (不造第二套)

只读模块: 不写库, 不改现有方法体 (会话B/C 并发改动面零冲突)。
"""

from __future__ import annotations

import inspect

from common.database import Database
from data_layer.health.readiness import DataReadiness
from data_layer.pipelines.freshness_audit import _days_behind

# DataLayer 上的写/生命周期方法 — 目录只列读 API, 这些排除
_NON_READ_METHODS = {
    "shutdown",
    "update_factor_daily",
    "persist_model_predictions",
    "backfill_fwd_returns",
}

# 必需表的日期列 (stock_info 为静态表无日期维度, 与 readiness.check 口径一致)
_REQUIRED_DATE_COLS: dict[str, str | None] = {
    t: ("date" if t != "stock_info" else None)
    for t in DataReadiness.REQUIRED_TABLES
}


def _first_doc_line(obj) -> str:
    doc = inspect.getdoc(obj) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def read_api_catalog(data_layer) -> list[dict]:
    """反射 DataLayer 公开读方法: 名称 + 签名 + 返回类型 + docstring 首行。

    薄委托方法 (如 snapshot) 自身无 docstring 时, 回退取 loader 同名方法
    的 docstring — 仍是自动派生, 不手工维护。
    """
    loader = getattr(data_layer, "loader", None)
    entries = []
    for name, member in inspect.getmembers(data_layer, inspect.ismethod):
        if name.startswith("_") or name in _NON_READ_METHODS:
            continue
        try:
            sig = inspect.signature(member)
        except (TypeError, ValueError):
            continue
        ret = sig.return_annotation
        ret_str = "Any" if ret is inspect.Signature.empty else str(ret)
        params = [p for p in sig.parameters if p != "self"]
        desc = _first_doc_line(member)
        if not desc and loader is not None:
            lm = getattr(loader, name, None)
            if lm is not None:
                desc = _first_doc_line(lm)
        entries.append({
            "name": name,
            "signature": f"{name}({', '.join(params)})",
            "return_type": ret_str,
            "description": desc,
        })
    return sorted(entries, key=lambda e: e["name"])


def pipeline_catalog(data_layer) -> list[dict]:
    """PipelineRegistry 自动派生: 管道名/依赖/产出。"""
    entries = []
    for pipe in data_layer.pipelines.get_all():
        entries.append({
            "name": pipe.name,
            "requires": list(getattr(pipe, "requires", []) or []),
            "produces": list(getattr(pipe, "produces", []) or []),
            "description": _first_doc_line(type(pipe)),
        })
    return sorted(entries, key=lambda e: e["name"])


def table_catalog(db: Database, audit_date: str) -> list[dict]:
    """表目录: 行数 + max(date) + 落后天数 + 状态。

    状态判据与 freshness_audit._write_row 完全一致 (复用 _days_behind, 不造第二套):
    无日期维度但有数据 → ok (静态表); behind==0 → ok; behind>0 → delayed; 无数据 → missing。
    """
    conn = db.get_connection()
    date_cols = dict(_REQUIRED_DATE_COLS)
    date_cols.update(DataReadiness.COLLECT_TABLES)
    # W2 卡①: T+1 源 (index_daily/lhb_daily) 也入目录 — 此处用 MAX(date)
    # 口径 (behind>0 → delayed), 与 readiness 的当日/上一日行数判定不同源,
    # T+1 天然滞后 1 天会显示 delayed(1) 属正常水位, 不是假阳。
    date_cols.update(DataReadiness.T1_COLLECT_TABLES)

    entries = []
    for table, date_col in date_cols.items():
        entry: dict = {"table": table, "date_column": date_col,
                       "row_count": 0, "max_date": None,
                       "days_behind": None, "status": "missing"}
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            entry["row_count"] = row[0] if row else 0
        except Exception:
            entries.append(entry)  # 表不存在 (管线未首跑) → missing
            continue
        if date_col:
            try:
                drow = conn.execute(
                    f"SELECT MAX({date_col}) FROM {table}").fetchone()
                entry["max_date"] = drow[0] if drow else None
            except Exception:
                entry["max_date"] = None
        behind = _days_behind(audit_date, entry["max_date"])
        if behind is None:
            if entry["row_count"] > 0:
                entry["status"], entry["days_behind"] = "ok", 0  # 静态表
            else:
                entry["status"] = "missing"
        elif behind == 0:
            entry["status"], entry["days_behind"] = "ok", 0
        else:
            entry["status"], entry["days_behind"] = "delayed", behind
        entries.append(entry)
    return sorted(entries, key=lambda e: e["table"])


def build_catalog(data_layer, audit_date: str) -> dict:
    """汇总目录: 读 API + 表 + 管道 + 停滞表清单。"""
    tables = table_catalog(data_layer.db, audit_date)
    stale = [t["table"] for t in tables if t["status"] != "ok"]
    return {
        "audit_date": audit_date,
        "read_apis": read_api_catalog(data_layer),
        "tables": tables,
        "pipelines": pipeline_catalog(data_layer),
        "stale_tables": stale,
    }
