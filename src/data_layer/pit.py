"""Point-in-Time 数据查询 — 保证数据时效性正确"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from common.utils import validate_identifier

_VALID_TABLES = {"daily_klines", "stock_financials", "stock_valuation", "factor_daily",
                 "stock_moneyflow", "concept_daily", "lhb_daily", "market_sentiment",
                 "analyst_forecasts"}

@dataclass
class PITQuery:
    """PIT查询参数"""
    code: str
    as_of_date: str
    lookback_days: int = 0
    table: str = ""
    columns: list[str] | None = None

@dataclass
class PITResult:
    """PIT查询结果"""
    code: str
    as_of_date: str
    data: list[dict[str, Any]] | None = None
    data_date: str | None = None
    staleness_days: int = 0

def _normalize_period_end(period: str | None) -> str | None:
    """报告期 → YYYY-MM-DD。接受 YYYY-MM-DD / YYYYMMDD / YYYY-MM / YYYYMM (月末)。"""
    if not period:
        return None
    p = str(period).strip()
    digits = p.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 6 and digits.isdigit():
        y, m = int(digits[:4]), int(digits[4:])
        if not 1 <= m <= 12:
            return None
        nxt = datetime(y + (m == 12), m % 12 + 1, 1)
        return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def statutory_disclosure_deadline(period_end: str | None) -> str | None:
    """A 股法定披露截止日: 一季报 4/30, 半年报 8/31, 三季报 10/31, 年报次年 4/30。

    非标准报告期 → 期末 +120 天 (保守)。无法解析 → None。
    """
    iso = _normalize_period_end(period_end)
    if iso is None:
        return None
    y, md = iso[:4], iso[5:]
    if md == "03-31":
        return f"{y}-04-30"
    if md == "06-30":
        return f"{y}-08-31"
    if md == "09-30":
        return f"{y}-10-31"
    if md == "12-31":
        return f"{int(y) + 1}-04-30"
    return (datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=120)).strftime("%Y-%m-%d")


def financial_available_date(period_end: str | None, announced_at: str | None = None,
                             announce_date: str | None = None) -> str | None:
    """财报可得日 (PIT): 真实公告日优先 (announced_at → announce_date), 空则取法定披露截止日。

    原实现公告日为空的行直接放行 (按报告期过滤) = 前视 (报告期末尚未公告)。
    """
    for v in (announced_at, announce_date):
        if v and str(v).strip():
            iso = _normalize_period_end(str(v).strip()[:10])
            if iso:
                return iso
    return statutory_disclosure_deadline(period_end)


class PITDataProvider:
    """Point-in-Time 数据提供者"""

    def __init__(self, db):
        self.db = db
        self._announce_col: bool | None = None

    def _has_announce_date(self, conn) -> bool:
        """stock_financials 是否含 announce_date 列 (DL-47 schema 变更后才存在)。"""
        if self._announce_col is None:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_financials)").fetchall()}
            self._announce_col = "announce_date" in cols
        return self._announce_col

    def query(self, query: PITQuery) -> PITResult:
        """执行PIT查询"""
        if not validate_identifier(query.table, "table") or query.table not in _VALID_TABLES:
            raise ValueError(f"Invalid table name: {query.table!r}")
        if query.columns:
            for col in query.columns:
                if not validate_identifier(col, "column"):
                    raise ValueError(f"Invalid column name: {col!r}")
        conn = self.db.get_connection()
        cols = ", ".join(query.columns) if query.columns else "*"

        if query.lookback_days > 0:
            start = (datetime.strptime(query.as_of_date, "%Y-%m-%d") - timedelta(days=query.lookback_days)).strftime("%Y-%m-%d")
            date_col_already = query.columns and "date" in query.columns
            select_cols = f"{cols}, date" if not date_col_already else cols
            rows = conn.execute(
                f"SELECT {select_cols} FROM {query.table} WHERE code = ? AND date >= ? AND date <= ? ORDER BY date DESC",
                (query.code, start, query.as_of_date),
            ).fetchall()
            return PITResult(
                code=query.code,
                as_of_date=query.as_of_date,
                data=[dict(r) for r in rows] if rows else None,
                data_date=rows[0]["date"] if rows else None,
                staleness_days=0,
            )
        else:
            row = conn.execute(
                f"SELECT {cols}, date FROM {query.table} WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (query.code, query.as_of_date),
            ).fetchone()
            if row:
                d = dict(row)
                data_date = d.pop("date", query.as_of_date)
                staleness = (datetime.strptime(query.as_of_date, "%Y-%m-%d") - datetime.strptime(data_date, "%Y-%m-%d")).days if data_date else 0
                return PITResult(
                    code=query.code,
                    as_of_date=query.as_of_date,
                    data=[d],
                    data_date=data_date,
                    staleness_days=max(0, staleness),
                )
            return PITResult(code=query.code, as_of_date=query.as_of_date)

    def query_financial(self, code: str, as_of_date: str) -> PITResult:
        """PIT查询财务数据 — 按公告日过滤, 消除前视偏差 (DL-47)。

        财报的 ``date`` 是报告期 (如 2026-06-30), ``announce_date`` 是实际公告日
        (如 2026-08-14)。PIT 查询必须只返回在 ``as_of_date`` 之前**已公告**的报告,
        否则回测会用到当时尚不可得的数据 (前视偏差)。

        过滤逻辑:
          - ``date <= as_of_date``: 报告期不得晚于查询时点 (候选前提)。
          - 可得日 <= as_of_date: 可得日 = announced_at / announce_date (真实公告日优先),
            为空时取法定披露截止日 (Q1 4/30, H1 8/31, Q3 10/31, 年报次年 4/30)。
        """
        conn = self.db.get_connection()
        # BT-CORRECTNESS ⑦: 公告日为空不再放行 — 可得日 = announced_at → announce_date →
        # 法定披露截止日 (见 financial_available_date)。按报告期倒序取首个已可得的行。
        rows = conn.execute(
            "SELECT * FROM stock_financials WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 12",
            (code, as_of_date),
        ).fetchall()
        row = None
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else []
            avail = financial_available_date(
                r["date"],
                r["announced_at"] if "announced_at" in keys else None,
                r["announce_date"] if "announce_date" in keys else None,
            )
            if avail is not None and avail <= as_of_date:
                row = r
                break
        if row:
            d = dict(row)
            data_date = d.get("date")
            staleness = 0
            if data_date:
                staleness = (datetime.strptime(as_of_date, "%Y-%m-%d")
                             - datetime.strptime(data_date, "%Y-%m-%d")).days
            return PITResult(
                code=code,
                as_of_date=as_of_date,
                data=[d],
                data_date=data_date,
                staleness_days=max(0, staleness),
            )
        return PITResult(code=code, as_of_date=as_of_date)

    def query_valuation(self, code: str, as_of_date: str) -> PITResult:
        """PIT查询估值数据"""
        return self.query(PITQuery(code=code, as_of_date=as_of_date, table="stock_valuation"))
