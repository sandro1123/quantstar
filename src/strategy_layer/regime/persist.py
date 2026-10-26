"""ST-REGIME 留痕与产线接线 — emotion_cycle_daily 表 + 产线跑批 + config 键化 (第二段)。

统筹裁定 (sm-2dc7744a35d6494a):
- 并行新表 emotion_cycle_daily (不扩 daily_regime 存量列 — 扩列刀面大)
- 判定核接产线: factor_update 后跑 (ops.yaml 条目 + 挂行文案归窗官)
- config 键化: 六键升 defaults/strategy.yaml, 代码常量回落缺省
"""
import json

from common.logging import get_logger
from common.utils import bj_date

from strategy_layer.regime.emotion_cycle import classify_emotion_cycle

logger = get_logger("strategy.regime.persist")

EMOTION_CYCLE_DDL = """
CREATE TABLE IF NOT EXISTS emotion_cycle_daily (
    date TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    hit TEXT,
    zt_last INTEGER,
    dt_last INTEGER,
    ladder_last INTEGER,
    zt_slope REAL,
    dt_slope REAL,
    evidence_json TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""


def load_cycle_params(config=None) -> dict:
    """config 键化 (③): strategy.regime.emotion_cycle 六键 — yaml 权威, 常量回落缺省。

    ST-STYLE 同款先例: 代码常量为缺省回落, yaml 键为运行时权威。
    """
    from strategy_layer.regime.emotion_cycle import EMOTION_CYCLE_PARAMS as DEFAULTS
    params = dict(DEFAULTS)
    try:
        if config is not None:
            section = config.get("strategy.regime.emotion_cycle", None)
            if isinstance(section, dict):
                for k in params:
                    if k in section:
                        params[k] = type(params[k])(section[k])
    except Exception as e:
        logger.warning(f"regime 参数 config 读取失败, 回落代码缺省: {e}")
    return params


def build_daily_series(db, date: str, window: int = 5) -> list[dict]:
    """取截至 date 的近 window 日判定原料序列 (升序)。

    原料面 (侦察稿): market_sentiment zt/dt + ladder 梯队规模 (COUNT DISTINCT code)。
    ladder 缺快照日 → 梯队值缺省 — classify 内部未用缺省值判 ice, 由 zt/dt 兜住;
    若 sentiment 也缺行 → 序列不足 → unknown 诚实降级。
    """
    series = []
    try:
        conn = db.get_connection()
        ms = conn.execute(
            "SELECT date, zt_count, dt_count FROM market_sentiment "
            "WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (date, window)).fetchall()
        lad = dict(conn.execute(
            "SELECT snapshot_date, COUNT(DISTINCT code) FROM limit_up_ladder "
            "GROUP BY snapshot_date").fetchall())
        for d, zt, dt in reversed(ms):
            series.append({"date": d, "zt": zt, "dt": dt,
                           "ladder_size": lad.get(d, 250)})
    except Exception as e:
        logger.warning(f"build_daily_series 取原料失败: {e}")
    return series


def run_emotion_cycle_daily(db, date: str | None = None, config=None) -> dict:
    """产线入口 (②): 判定 + 留痕落库 (幂等, date PK)。

    Returns:
        {"date", "stage", "hit", "persisted": bool}
    """
    day = date or bj_date()
    series = build_daily_series(db, day)
    params = load_cycle_params(config)
    result = classify_emotion_cycle(series, params=params)
    ev = result["evidence"]
    stage, hit = result["stage"], ev.get("hit")

    persisted = False
    try:
        conn = db.get_connection()
        conn.execute(EMOTION_CYCLE_DDL)
        conn.execute(
            "INSERT INTO emotion_cycle_daily "
            "(date, stage, hit, zt_last, dt_last, ladder_last, zt_slope, dt_slope, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "stage=excluded.stage, hit=excluded.hit, zt_last=excluded.zt_last, "
            "dt_last=excluded.dt_last, ladder_last=excluded.ladder_last, "
            "zt_slope=excluded.zt_slope, dt_slope=excluded.dt_slope, "
            "evidence_json=excluded.evidence_json, "
            "created_at=datetime('now', '+8 hours')",
            (day, stage, hit, ev.get("zt_last"), ev.get("dt_last"),
             ev.get("ladder_last"), ev.get("zt_slope"), ev.get("dt_slope"),
             json.dumps(ev, ensure_ascii=False)))
        conn.commit()
        persisted = True
        logger.info(f"emotion_cycle_daily persisted: {day} -> {stage} ({hit})")
    except Exception as e:
        logger.warning(f"emotion_cycle_daily 落库失败: {e}")
    return {"date": day, "stage": stage, "hit": hit, "persisted": persisted}
