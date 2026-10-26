"""data_layer/quality.py — DL-29 数据质量把关: 异常值标记 + 轻量 DLQ

设计依据: layer-01-数据层.md §14 异常数据标记 (Q028) + D97 坏数据隔离 + D-44 轻量 DLQ。

两个通道 (均不改原值):
1. 异常标记 → data_quality_flags 独立表 (不动 daily_klines 1275 万行 schema;
   加列需写库三步曲+GO, 独立表可自主, 且消费侧日后可 join)
2. DLQ → logs/dlq/<date>.jsonl (不建表, 与 P0-5 observability_fallback 同思路;
   含原始记录/来源/原因/时间戳, 配 scripts/replay_dlq.py 可重放)

规则集配置化 (config/defaults/data.yaml data.quality), 不硬编码:
- pre_close <= 0
- change_pct 与 (close/pre_close-1)*100 背离超阈值
- close 越界 [low, high]
- volume < 0 / high < low
- 批内 change_pct 超 3 倍 MAD
- 批内 change_pct=0 占比超阈值 (07-29 整批清零模式; 平盘股局部自洽但整批异常)

消费侧 (snapshot/get_history) 本卡不改 — 只标记不过滤, 改动属跨层,
已在 跨层契约变更.md 记录待策略层消费。
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime

from common.database import Database
from common.logging import get_logger

logger = get_logger("data.quality")

# 规则默认值 (data.yaml data.quality 可覆盖)
DEFAULTS = {
    "enabled": True,
    "change_pct_divergence": 1.0,   # 百分点: change_pct 与重算值背离阈值
    "mad_multiplier": 3.0,          # 批内 change_pct 超 N 倍 MAD 判 outlier
    "mad_field": "change_pct",
    "zero_pct_batch_ratio": 0.5,    # 批内 change_pct=0 占比超此值判整批清零异常
    # 判据 #186 量/额维度不变量: 额[元]/量[股] 应 ~= 股价。
    # 经验区间取 [vap_ratio_lo, vap_ratio_hi] 倍 close 的宽包络:
    # 实测 5 天干净行 0 假阳 / 3 天污染行命中 98.95%。
    "vap_ratio_lo": 0.3,            # (amount/volume)/close 下限
    "vap_ratio_hi": 5.0,            # (amount/volume)/close 上限
}

_FLAGS_DDL = """
CREATE TABLE IF NOT EXISTS data_quality_flags (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    table_name TEXT NOT NULL DEFAULT 'daily_klines',
    field TEXT NOT NULL,
    flag TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (code, date, table_name, field, rule)
);
"""


def _dlq_dir() -> str:
    override = os.environ.get("QS_DLQ_DIR")
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "logs", "dlq")


def load_quality_config(config=None) -> dict:
    """读 data.quality 配置, 缺失项回退默认 (配置而非硬编码)。"""
    cfg = dict(DEFAULTS)
    try:
        if config is None:
            from config_layer import Config
            config = Config()
            config.load()
        raw = config.get("data.quality") or {}
        for k in cfg:
            if k in raw:
                cfg[k] = raw[k]
    except Exception as e:
        logger.warning(f"quality config load failed, using defaults: {e}")
    return cfg


class FlagStore:
    """data_quality_flags 独立表写入 (标记保留原值, 不碰源表)。"""

    def __init__(self, db: Database):
        self.db = db
        self._ensured = False

    def _ensure(self):
        if self._ensured:
            return
        conn = self.db.get_connection()
        conn.executescript(_FLAGS_DDL)
        conn.commit()
        self._ensured = True

    def write_flags(self, flags: list[dict]) -> int:
        if not flags:
            return 0
        self._ensure()
        conn = self.db.get_connection()
        conn.executemany("""
            INSERT OR IGNORE INTO data_quality_flags
            (code, date, table_name, field, flag, reason, rule)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(f["code"], f["date"], f.get("table_name", "daily_klines"),
               f["field"], f["flag"], f["reason"], f["rule"]) for f in flags])
        conn.commit()
        return len(flags)

    def get_flags(self, date: str | None = None, code: str | None = None) -> list[dict]:
        self._ensure()
        conn = self.db.get_connection()
        sql = "SELECT * FROM data_quality_flags WHERE 1=1"
        args: list = []
        if date:
            sql += " AND date = ?"
            args.append(date)
        if code:
            sql += " AND code = ?"
            args.append(code)
        return [dict(r) for r in conn.execute(sql + " ORDER BY code, date", args).fetchall()]


class DLQ:
    """轻量死信队列 — logs/dlq/<date>.jsonl, 不建表 (D-44)。"""

    @staticmethod
    def append(date: str, source: str, record: dict, flags: list[dict]) -> None:
        path = os.path.join(_dlq_dir(), f"{date}.jsonl")
        event = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": date,
            "source": source,
            "record": record,
            "flags": flags,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:  # 观测通道失败不阻塞写入路径, 但必须出声 (铁律 #12)
            print(f"[quality][DLQ-FAILED] 无法写 {path}: {e}; record={record}", flush=True)


def check_kline_batch(records: list[dict], cfg: dict | None = None,
                      exempt_pre_close: set | None = None) -> dict[str, list[dict]]:
    """对一批 kline 记录跑规则集, 返回 {code: [flag,...]}。不改 records 原值。

    规则 (全部配置化): pre_close<=0 / change_pct 背离 / close 越界 /
    volume<0 / high<low / 批内 MAD。

    exempt_pre_close: {(code, date)} 集合 — 已在 data_quality_flags 标记为
    rule='pre_close_unrecoverable' 的存量不可回补行 (DL-38)。对这些行跳过
    pre_close<=0 规则, 避免重写时反复报 suspicious (狼来了)。其余规则照常。
    """
    cfg = cfg or dict(DEFAULTS)
    exempt_pre_close = exempt_pre_close or set()
    flags_by_code: dict[str, list[dict]] = {}

    def add(r: dict, field: str, flag: str, reason: str, rule: str):
        flags_by_code.setdefault(r.get("code", "?"), []).append({
            "code": r.get("code", "?"), "date": str(r.get("date", ""))[:10],
            "table_name": "daily_klines", "field": field,
            "flag": flag, "reason": reason, "rule": rule,
        })

    div_thr = float(cfg.get("change_pct_divergence", DEFAULTS["change_pct_divergence"]))
    mad_mul = float(cfg.get("mad_multiplier", DEFAULTS["mad_multiplier"]))
    mad_field = cfg.get("mad_field", DEFAULTS["mad_field"])

    for r in records:
        pre_close = r.get("pre_close") or 0
        close = r.get("close") or 0
        high = r.get("high") or 0
        low = r.get("low") or 0
        volume = r.get("volume")
        change_pct = r.get("change_pct")

        if pre_close is not None and pre_close <= 0:
            # DL-38: 已标记不可回补的存量行 (上市首日/采集起点) 豁免, 不反复报
            if (r.get("code", "?"), str(r.get("date", ""))[:10]) in exempt_pre_close:
                pass
            else:
                add(r, "pre_close", "suspicious", f"pre_close={pre_close} <= 0", "pre_close_nonpositive")
        if pre_close and pre_close > 0 and close and change_pct is not None:
            expected = (close / pre_close - 1) * 100
            if abs(change_pct - expected) > div_thr:
                add(r, "change_pct", "suspicious",
                    f"change_pct={change_pct} 与重算值 {expected:.2f} 背离超 {div_thr}",
                    "change_pct_divergence")
        if high and low and close and not (low <= close <= high):
            add(r, "close", "outlier", f"close={close} 越界 [low={low}, high={high}]", "close_out_of_range")
        if volume is not None and volume < 0:
            add(r, "volume", "outlier", f"volume={volume} < 0", "volume_negative")
        if high and low and high < low:
            add(r, "high", "outlier", f"high={high} < low={low}", "high_below_low")

        # 判据 #186: 量/额维度物理不变量 (额/量 应 ~= 股价, 单位元/股)
        # 互换签名 = 该比值跌到 ~1/close。旧规则集对此全盲, 见 card 注释。
        amount = r.get("amount")
        vap_lo = float(cfg.get("vap_ratio_lo", DEFAULTS["vap_ratio_lo"]))
        vap_hi = float(cfg.get("vap_ratio_hi", DEFAULTS["vap_ratio_hi"]))
        if (amount is not None and volume is not None and volume > 0
                and isinstance(amount, (int, float)) and isinstance(volume, (int, float))
                and close and close > 0 and low and high):
            unit_price = float(amount) / float(volume)
            ratio = unit_price / close
            if not (vap_lo <= ratio <= vap_hi):
                add(r, "amount", "suspicious",
                    f"amount/volume={unit_price:.4f} 与 close={close} 比={ratio:.4f} "
                    f"越出 [{vap_lo},{vap_hi}] (疑似量/额两列倒置, 判据 #186)",
                    "amount_volume_ratio_close_out_of_tier")

    # 批内整批清零规则 (07-29 模式: 平盘股局部自洽, 但整批 change_pct=0 必是坏数据)
    zero_thr = float(cfg.get("zero_pct_batch_ratio", DEFAULTS["zero_pct_batch_ratio"]))
    recomputable = [r for r in records
                    if r.get("pre_close") and r.get("pre_close") > 0 and r.get("close")]
    zeros = [r for r in recomputable if r.get("change_pct") == 0]
    if recomputable and len(zeros) / len(recomputable) > zero_thr:
        ratio = len(zeros) / len(recomputable)
        for r in zeros:
            add(r, "change_pct", "suspicious",
                f"change_pct=0 且批内清零占比 {ratio:.1%} 超 {zero_thr:.0%} (疑似整批清零异常)",
                "batch_change_pct_zeroed")

    # 批内 MAD 规则 (需要整批上下文)
    vals = [r.get(mad_field) for r in records
            if isinstance(r.get(mad_field), (int, float))]
    if len(vals) >= 10:
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        if mad > 1e-9:
            for r in records:
                v = r.get(mad_field)
                if isinstance(v, (int, float)) and abs(v - med) > mad_mul * mad:
                    add(r, mad_field, "outlier",
                        f"{mad_field}={v} 超 {mad_mul} 倍 MAD (median={med:.2f}, MAD={mad:.2f})",
                        "mad_outlier")

    return flags_by_code


class KlineQualityGate:
    """kline 写入路径质量闸: 检查 → 标记入 flag 表 + 落 DLQ → 原样放行。

    fail-open: 质量检查自身出错只告警不阻塞写入 (观测不挡数据),
    但错误必须出声 (铁律 #12)。
    """

    def __init__(self, db: Database, config=None):
        self.db = db
        self.cfg = load_quality_config(config)
        self._store = FlagStore(db)

    def _load_unrecoverable_exempt(self) -> set:
        """DL-38: 读 data_quality_flags 中已标记 pre_close 不可回补的 (code, date) 集。

        fail-open: 表不存在/查询失败返回空集 (豁免不生效, 写入照常), 不阻塞。
        定向查询 (rule+field 过滤), 不整表加载。
        """
        try:
            self._store._ensure()
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT code, date FROM data_quality_flags "
                "WHERE rule = 'pre_close_unrecoverable' AND field = 'pre_close'"
            ).fetchall()
            return {(r["code"], r["date"]) for r in rows}
        except Exception as e:
            logger.debug(f"unrecoverable exempt load skipped (fail-open): {e}")
            return set()

    def process(self, records: list[dict], source: str = "kline_write") -> list[dict]:
        """返回 records 原值 (不改); 副作用: 写 flag 表 + DLQ。"""
        if not records or not self.cfg.get("enabled", True):
            return records
        try:
            # DL-38: 仅当本批确有 pre_close<=0 行时才查豁免集 (省一次表扫描)
            has_bad_pre_close = any(
                (r.get("pre_close") or 0) <= 0 for r in records)
            exempt = self._load_unrecoverable_exempt() if has_bad_pre_close else None
            flags_by_code = check_kline_batch(records, self.cfg, exempt_pre_close=exempt)
            if not flags_by_code:
                return records
            all_flags = [f for fl in flags_by_code.values() for f in fl]
            self._store.write_flags(all_flags)
            by_record: dict[str, list[dict]] = {}
            for f in all_flags:
                by_record.setdefault((f["code"], f["date"]), []).append(f)
            for r in records:
                key = (r.get("code", "?"), str(r.get("date", ""))[:10])
                if key in by_record:
                    DLQ.append(key[1], source, r, by_record[key])
            logger.warning(
                f"quality gate: {len(all_flags)} flags on {len(flags_by_code)} records "
                f"(source={source}); 原值未改, 见 data_quality_flags + logs/dlq/")
        except Exception as e:
            logger.error(f"quality gate error (fail-open, 写入继续): {e}")
        return records
