# -*- coding: utf-8 -*-
"""记忆层案A通电 — 消费面注入器 + 喂料面写手 + 双向流量计数 (2026-09-09)。

治什么 (派单 sm-6ddacf7b): MemoryStore.read() 建了没通电 — 全库零消费,
记忆层只有 evaluator 一条写入线, 是「建了没通电」家族最新成员。

四件:
  ① 消费面 inject_memories — 六专家决策前读 stock:<code> + factor:<name>
     域历史教训拼进 data_summary (「这股上次骗过我」)。调用点:
     agent_layer/debate/experts.py 四专家 analyze() 组 data 处 (一处
     _llm_expert_opinion 是公共 LLM 腿, 注入必须在 data 组装层让规则腿
     也吃到); B席 build_request 工厂落地后对齐。
  ② 喂料面 record_ic_lessons — ic_feedback 结算后 IC 转负因子写
     factor:<name> lesson (evidence 必填 = 三铁律闸门在位)。
  ③ 双向流量可观测 — memory_metrics 表: read_hits/write_hits 计数,
     get_metrics 晨验可查 (验收判据=读写双向>0)。
  ④ 案C PIT 留口 — MemoryStore.write 增 ingested_at 参数+列位
     (不施工 PIT 本体, 只留字段; store 内建表带列+ALTER 守卫)。

纪律: 全部诚实降级 — 注入/计数失败只 warning, 绝不阻断专家研判与主管道。
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("memory.wire")

# 注入预算: data_summary 拼接段 token 上限 (中文 ≈ len/1.5)
_MAX_INJECT_CHARS = 300


# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
def _metrics_table(conn) -> None:
    """memory_metrics 计数表 (幂等建表, 双向流量可观测面)。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_metrics ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "read_hits INTEGER NOT NULL DEFAULT 0, "
        "write_hits INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT DEFAULT (datetime('now'))) "
    )
    conn.execute("INSERT OR IGNORE INTO memory_metrics (id) VALUES (1)")
    conn.commit()


# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
def _bump(conn, field: str) -> None:
    """计数 +1 (诚实降级: 失败只 warning)。"""
    try:
        _metrics_table(conn)
        conn.execute(
            f"UPDATE memory_metrics SET {field} = {field} + 1, "
            f"updated_at = datetime('now') WHERE id = 1")
        conn.commit()
    except Exception as e:
        logger.warning(f"memory_metrics bump {field} 失败 (不阻断): {e}")


def get_read_hits(db) -> int:
    conn = db.get_connection()
    _metrics_table(conn)
    return conn.execute(
        "SELECT read_hits FROM memory_metrics WHERE id=1").fetchone()[0]


def get_metrics(db) -> dict:
    """晨验可查形态: {'read_hits': N, 'write_hits': N}。"""
    try:
        conn = db.get_connection()
        _metrics_table(conn)
        r = conn.execute(
            "SELECT read_hits, write_hits FROM memory_metrics WHERE id=1"
        ).fetchone()
        return {"read_hits": r[0], "write_hits": r[1]}
    except Exception as e:
        logger.warning(f"get_metrics 失败: {e}")
        return {"read_hits": 0, "write_hits": 0}


# ---------------------------------------------------------------------------
# ① 消费面: 注入器
# ---------------------------------------------------------------------------

def inject_memories(db, data: dict) -> dict:
    """六专家决策前注入历史教训 — stock:<code> + factor:<name> 双域。

    data: 专家腿的组包 dict (code/date/data_summary/factors?);
    返回同 dict (data_summary 已拼段)。无记忆/读失败 → 原样返回零扰动。
    命中即 bump read_hits (③ 可观测)。
    """
    try:
        from memory_layer.store import MemoryStore
        ms = MemoryStore(db)
        scopes: list[str] = []
        code = data.get("code") or ""
        if code:
            scopes.append(f"stock:{code}")
        # factor 域: data.get("factors") 或 data_summary 里的因子名线索
        for f in (data.get("factors") or [])[:5]:
            if f:
                scopes.append(f"factor:{f}")
        lines: list[str] = []
        for scope in scopes:
            try:
                mems = ms.read(scope=scope, kind="lesson", top_k=2)
            except Exception:
                continue
            for m in mems:
                lines.append(f"[{m.scope}] {m.content}")
        if not lines:
            return data  # 零记忆零扰动
        blob = "\n".join(lines)[:_MAX_INJECT_CHARS]
        data["data_summary"] = (
            f"{data.get('data_summary', '')}\n\n=== 历史教训 (memory_layer) ===\n{blob}"
        ).strip()
        _bump(db.get_connection(), "read_hits")
        return data
    except Exception as e:
        logger.warning(f"inject_memories 失败 (诚实降级, 原样返回): {e}")
        return data


# ---------------------------------------------------------------------------
# ② 喂料面: ic_feedback 结算后 factor lesson 写手
# ---------------------------------------------------------------------------

def record_ic_lessons(db, daily_ic: dict[str, float], sample_size: int = 0,
                      calc_date: str = "") -> int:
    """IC 转负因子 → factor:<name> lesson (evidence 必填)。

    只写 mean_ic < 0 的因子 (正 IC 不造教训); evidence 带 calc_date +
    mean_ic + sample (可溯源三铁律); 每因子一行, 返回写入数。
    写入命中 bump write_hits (③ 可观测)。
    """
    n = 0
    try:
        from memory_layer.store import MemoryStore
        ms = MemoryStore(db)
        for factor, ic in (daily_ic or {}).items():
            if ic is None or float(ic) >= 0:
                continue
            content = (f"factor {factor} {calc_date or '当日'} IC 转负 "
                       f"(mean_ic={float(ic):.4f}, n={sample_size}), "
                       f"近端信号失效 — 降权/复核候选")
            evidence = (f"ic_feedback:{calc_date or 'na'}:factor={factor},"
                        f"mean_ic={float(ic):.4f},sample={sample_size}")
            mid = ms.write(f"factor:{factor}", "lesson", content,
                           evidence=evidence, confidence=0.6)
            if mid is not None:
                n += 1
        if n:
            _bump(db.get_connection(), "write_hits")
        return n
    except Exception as e:
        logger.warning(f"record_ic_lessons 失败 (诚实降级): {e}")
        return n
