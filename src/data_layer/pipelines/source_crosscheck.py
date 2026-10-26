"""pipelines/source_crosscheck.py — P0-5 源健康第三方对拍 (新浪参照)

统筹裁定 (2026-08-27): efinance 撞 D-43 东财反爬撤销, 参照源改新浪 hq.sinajs.cn
(实测 200 可达)。抽样核对当日收盘/成交量 vs 主源 (daily_klines), 差异落表+告警,
作为源健康的独立通道证据 — 对拍的价值在独立通道, 不在字段多。

设计:
- 抽样: 从 daily_klines 当日有行的 code 随机抽 N 只 (默认 50, cfg 可配),
  不全量 (S 级不膨胀); 北交所 code (8/4 开头) 新浪不覆盖, 抽样时排除。
- 参照: 新浪 hq.sinajs.cn 批量行情 (须带 Referer 头, GBK 编码, 实测格式:
  var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,...,成交量(股),成交额,...,日期,时间,...")
  字段 [3]=现价 (收盘后=收盘价), [8]=成交量 (股) — 与 daily_klines 单位一致
  (2026-08-27 实测 600519 close=1292.3/vol=2476747 双侧精确吻合)。
- 判定: close 差 > 0.011 (1 分钱舍入容差) 或 volume 相对差 > 1% → 差异行落
  source_crosscheck_diffs 表; 差异率超阈值 → handler 发 warning 告警。
- 停牌/缺失: 新浪返回空串或当日无行 → 计 missing, 不算差异 (停牌股本就无成交)。

红线自查: 只读 daily_klines + 写独立 diffs 表 (不动主表); 新浪请求带 timeout,
失败降级 partial 不阻塞 (对拍是观测件不是主链)。
"""

from __future__ import annotations

import random

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.source_crosscheck")

_SINA_URL = "https://hq.sinajs.cn/list="
_SINA_HEADERS = {
    # 新浪 hq.sinajs.cn 必须带 Referer (源码实证), 否则 403
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}

# 差异判定容差
CLOSE_TOL = 0.011          # 收盘价绝对差容差 (1 分钱舍入)
VOLUME_REL_TOL = 0.01      # 成交量相对差容差 (1%)

DEFAULTS = {
    "sample_size": 50,      # 抽样只数
    "diff_rate_alert": 0.05,  # 差异率超此值发 warning 告警
    "timeout_s": 15,        # 新浪请求超时
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_crosscheck_diffs (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    field TEXT NOT NULL,
    main_value REAL,
    ref_value REAL,
    diff REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (date, code, field)
);
"""


def _to_sina_symbol(code: str) -> str | None:
    """6 位 code → 新浪带前缀代码; 北交所 (8/4 开头) 新浪不覆盖返回 None。"""
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    return None


def _parse_sina_response(text: str) -> dict[str, dict]:
    """解析新浪批量行情文本 → {6位code: {"close": float, "volume": float}}。

    每行格式: var hq_str_sh600519="贵州茅台,今开,昨收,现价,...,成交量,...";
    GBK 解码由调用方完成; 空串 (停牌/无效) 跳过。
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        try:
            sym_part, payload = line.split("=", 1)
            sina_sym = sym_part.replace("var hq_str_", "")
            code = sina_sym[2:]  # 去 sh/sz 前缀
            payload = payload.strip().strip('"').strip(";").strip('"')
            if not payload:
                continue  # 停牌/无效 → 空串
            fields = payload.split(",")
            if len(fields) < 9:
                continue
            close = float(fields[3])
            volume = float(fields[8])
            if close <= 0:
                continue
            out[code] = {"close": close, "volume": volume}
        except (ValueError, IndexError):
            continue
    return out


class SourceCrosscheckPipeline(BasePipeline):
    """源健康第三方对拍 — 抽样核对主源 vs 新浪, 差异落表。"""

    name = "source_crosscheck"
    requires = []
    produces = ["source_crosscheck_diffs"]

    def __init__(self, db=None):
        self.db = db

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        conn.executescript(_SCHEMA)

        cfg = {**DEFAULTS, **(input.config or {})}
        date = input.date
        warnings: list[str] = []

        # 1. 抽样: 当日有行的 code, 排除北交所 (新浪不覆盖)
        rows = conn.execute(
            "SELECT code, close, volume FROM daily_klines WHERE date = ?",
            (date,),
        ).fetchall()
        candidates = [r for r in rows if _to_sina_symbol(r["code"]) is not None]
        if not candidates:
            return PipelineResult(
                name=self.name, date=date, status="success",
                warnings=[f"daily_klines {date} 无可对拍 code"],
                metadata={"n_sampled": 0, "n_diff": 0, "skipped": True},
            )
        sample_size = min(int(cfg["sample_size"]), len(candidates))
        sample = random.sample(candidates, sample_size)

        # 2. 拉新浪参照 (批量一次请求)
        sina_syms = [_to_sina_symbol(r["code"]) for r in sample]
        try:
            import requests
            resp = requests.get(
                _SINA_URL + ",".join(sina_syms),
                headers=_SINA_HEADERS, timeout=cfg["timeout_s"],
            )
            resp.raise_for_status()
            ref = _parse_sina_response(resp.content.decode("gbk", errors="replace"))
        except Exception as e:  # noqa: BLE001
            # 参照源不可达 → partial (对拍是观测件, 不阻塞主链, 铁律#12 留痕)
            return PipelineResult(
                name=self.name, date=date, status="partial",
                warnings=[f"新浪参照源不可达: {e}"],
                metadata={"n_sampled": sample_size, "n_diff": 0,
                          "ref_unavailable": True},
            )

        # 3. 逐只对拍 close + volume
        n_diff = 0
        n_missing = 0
        for r in sample:
            code = r["code"]
            ref_row = ref.get(code)
            if ref_row is None:
                n_missing += 1  # 停牌/新浪无此 code, 不算差异
                continue
            for field, main_val, ref_val, tol in (
                ("close", r["close"], ref_row["close"], CLOSE_TOL),
                ("volume", r["volume"], ref_row["volume"], VOLUME_REL_TOL),
            ):
                if main_val is None:
                    continue
                if field == "close":
                    is_diff = abs(main_val - ref_val) > tol
                    diff_val = round(main_val - ref_val, 4)
                else:
                    denom = max(ref_val, 1.0)
                    is_diff = abs(main_val - ref_val) / denom > tol
                    diff_val = round(main_val - ref_val, 1)
                if is_diff:
                    n_diff += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO source_crosscheck_diffs "
                        "(date, code, field, main_value, ref_value, diff) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (date, code, field, main_val, ref_val, diff_val),
                    )
        conn.commit()

        n_compared = sample_size - n_missing
        diff_rate = (n_diff / n_compared) if n_compared else 0.0
        if n_missing:
            warnings.append(f"{n_missing} 只新浪无数据 (停牌/不覆盖), 未计入差异")

        return PipelineResult(
            name=self.name, date=date, status="success",
            records_read=sample_size, records_written=n_diff,
            warnings=warnings,
            metadata={
                "n_sampled": sample_size,
                "n_compared": n_compared,
                "n_diff": n_diff,
                "diff_rate": round(diff_rate, 4),
                "diff_rate_alert": cfg["diff_rate_alert"],
                "ref_source": "sina_hq",
            },
        )
