"""pipelines/index_daily.py — DL-23 指数日线采集 (BT-04 基准对比硬前置)

现状: index_daily 表不存在, 回测层基准对比恒 0 假超额 (04 审计 B-04)。
数据源实测 (2026-08-18): akshare 新浪 stock_zh_index_daily 可用——
- sh000300 沪深300: 5972 行全历史 (单次调用, 无限流)
- sz399006 创业板指: 3937 行 (2010-06-01 起)
东财 index_zh_a_hist / stock_zh_index_daily_em 连接被拒 (与 DL-00 kline 结论一致)。

设计: 全量拉取 + INSERT OR REPLACE 幂等 (PK code+date); 首跑即完成历史回填,
此后每日运行自动增量。不建因子不进选股链路, 仅供回测基准/报告大盘段落消费。
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.index_daily")

# 采集标的: (库内代码, akshare 新浪符号, 名称)
INDEX_TARGETS = [
    ("000300.SH", "sh000300", "沪深300"),
    ("399006.SZ", "sz399006", "创业板指"),
]

# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    name TEXT DEFAULT '',
    source TEXT DEFAULT 'akshare_sina',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(date);
"""


def _safe_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


class IndexDailyPipeline(BasePipeline):
    name = "index_daily"
    requires: list[str] = []
    produces = ["index_daily"]

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db:
            from common.database import Database
            input.db = Database()
        conn = input.db.get_connection()
        conn.executescript(_SCHEMA)

        errors = []
        total = 0
        for code, sina_symbol, idx_name in INDEX_TARGETS:
            try:
                rows = self._fetch(sina_symbol)
            except Exception as e:
                errors.append(f"{idx_name}({code}) 拉取失败: {e}")
                logger.warning(f"index_daily {code} fetch failed: {e}")
                continue
            if not rows:
                errors.append(f"{idx_name}({code}) 返回空")
                continue
            written = 0
            has_business_date = False
            for r in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO index_daily "
                    "(code, date, open, high, low, close, volume, name, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'akshare_sina')",
                    (code, str(r["date"]), _safe_float(r.get("open")),
                     _safe_float(r.get("high")), _safe_float(r.get("low")),
                     _safe_float(r.get("close")), _safe_float(r.get("volume")),
                     idx_name),
                )
                written += 1
                if str(r["date"]) == input.date:
                    has_business_date = True
            conn.commit()
            # 全历史回填量不代表请求日已发布；保留源日期，不拿 T-1 冒充当日。
            if not has_business_date:
                errors.append(f"{idx_name}({code}) 缺少业务日 {input.date} 数据")
            total += written
            logger.info(f"index_daily {code} ({idx_name}): {written} rows")

        status = "success" if total > 0 else "failed"
        if total > 0 and errors:
            status = "partial"
        return PipelineResult(
            name=self.name, date=input.date,
            status=status, records_written=total, errors=errors,
        )

    def _fetch(self, sina_symbol: str) -> list[dict]:
        """akshare 新浪指数日线 (全历史单次返回)"""
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=sina_symbol)
        if df is None or df.empty:
            return []
        return df.to_dict("records")
