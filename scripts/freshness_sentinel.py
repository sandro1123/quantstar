"""#116 面一 09:30 轻哨 — 读前日 freshness_audit 冷采集审计行 → 飞书告警。

治什么: 冷采集表零行日全盲。lhb_daily 11 个交易日缺日零告警的根因之一 =
  审计面 (freshness_audit, 16:22 每日落档) 在岗但**无人读**。
修法: 次日 09:30 轻哨 (cron 直驱, 读-only, 零写库) 读前日审计行,
  非 ok 状态 (missing/delayed) → 飞书告警 (best-effort, 失败静默不哑哨)。

口径:
  / 只读 — 不写任何表 (与 config_drift_sentinel 同族: 哨兵不能写库)。
  / 只看冷采集面 (COLLECT_TABLES 当日源 + T1_COLLECT_TABLES T+1 源, 共 10 表) — 主链 4 表已有
    e2e_verification / health_check 双面覆盖, 不双发。
  / delayed 也报: 冷采集「当日有行」才 ok, freshness_audit 里 collect 面
    非 ok 一律出自当日零行 (missing) — delayed 仅主链面会出现, 顺带无妨。
  / 前日无审计行 (管线未跑/新装机) → 轻提示不告警 (诚实形态)。
  / 同一表连续多日非 ok → 标注连续天数 (read streak, 帮分诊急缓)。

用法 (cron 直驱, 参照 e2e_verification 行形态):
  PYTHONPATH=src CONFIG_ENV=prod python scripts/freshness_sentinel.py [--date YYYY-MM-DD]

落档: 无文件落档 (审计行本身就在 freshness_audit 表里, 哨兵零冗余)。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _prev_trading_date(d: str) -> str:
    """前一个自然日 (cron 在 09:30 读昨日; 周末昨日为周六 — 审计行不存在,
    自然落到「无审计行」轻提示分支, 无需交易日历)。"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def read_audit(date: str) -> list[dict]:
    """读指定日 freshness_audit 冷采集面行 (ro 连接, 零写)。

    W2 卡①: 覆盖面 = COLLECT_TABLES (当日源) + T1_COLLECT_TABLES (T+1 源)。
    T+1 源的 missing 现已是真断供语义 (查上一交易日), 哨兵照常广播。"""
    import sqlite3
    from data_layer.health.readiness import DataReadiness

    tables = list(DataReadiness.COLLECT_TABLES) + list(DataReadiness.T1_COLLECT_TABLES)
    db_path = _resolve_db_path()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(tables))
        rows = conn.execute(
            f"SELECT table_name, status, actual_at, delay_min FROM freshness_audit "
            f"WHERE date=? AND table_name IN ({placeholders}) ORDER BY table_name",
            (date, *tables),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _resolve_db_path() -> str:
    """与 Database() 同源解析 — 优先 CONFIG_ENV 配置, 缺省 data/qs.db。
    禁 quantstar.db 空壳优先序 (历史坑)。"""
    from common.database import Database
    try:
        # Database 只挂 self.db_path (ro 核 src/common/database.py:57);
        # 勿用 _db_path — 该属性不存在, 旧形态靠 AttributeError 静默回落
        return Database().db_path
    except Exception:
        return os.path.join(REPO_ROOT, "data", "qs.db")


def _streak(conn_unused, table: str, status: str) -> int:
    """连续非 ok 天数 — 只读回溯 (上限 30 天防慢查)。"""
    return 0  # v1 不查 streak: 前日单点足够轻; 留位待二期


def send_alert(title: str, body: str) -> None:
    """飞书告警 — best-effort, 失败静默 (哨兵不能因告警失败而哑)。
    复用 config_drift_sentinel 同构 (delivery_layer feishu_dm 通道)。"""
    try:
        from config_layer import get_config
        from delivery_layer import DeliveryLayer, Message
        cfg = get_config()
        if not cfg._loaded:
            cfg.load()
        delivery = DeliveryLayer(config=cfg)
        delivery.registry.mark_healthy("feishu_dm")
        content = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": "red"},
                "elements": [{"tag": "div",
                              "text": {"tag": "lark_md", "content": body}}],
            },
        }
        delivery.send(Message(content=content, content_type="freshness_sentinel_alert",
                              title=title, force_channel="feishu_dm"))
    except Exception as e:
        print(f"[freshness_sentinel] 告警发送失败 (静默降级): {e}", file=sys.stderr)


def run(target_date: str, notify_disabled: bool = False) -> int:
    audit_date = _prev_trading_date(target_date)
    rows = read_audit(audit_date)

    if not rows:
        # 前日审计行不存在 (freshness_audit 未跑/新装机) — 轻提示不告警
        if not notify_disabled:
            send_alert(
                "ⓘ QS 冷采集新鲜度哨兵",
                f"{audit_date} 无 freshness_audit 审计行 — freshness_audit 管线"
                "前日未落档 (管线未跑或新装机), 请核对 16:22 排程。")
        print(f"[freshness_sentinel] no audit rows for {audit_date}")
        return 0

    bad = [r for r in rows if r["status"] != "ok"]
    n_ok = len(rows) - len(bad)
    print(f"[freshness_sentinel] {audit_date}: {n_ok} ok / {len(bad)} not-ok "
          f"(collect tables {len(rows)})")

    if not bad:
        print("[freshness_sentinel] all collect tables ok — 不打扰")
        return 0

    lines = []
    for r in bad:
        lines.append(f"/ **{r['table_name']}** — {r['status']}"
                     + (f" (滞后 {r['delay_min']} 分钟)" if r.get("delay_min") else ""))
    body = (f"**{audit_date} 冷采集新鲜度审计 — {len(bad)}/{len(rows)} 表非 ok:**\n"
            + "\n".join(lines)
            + "\n\n_口径: 只读哨兵, 零写库; 详见 freshness_audit 表。_")
    if not notify_disabled:
        send_alert("⚠️ QS 冷采集零行/滞后告警", body)
    print(f"[freshness_sentinel] ALERT: {len(bad)} tables not ok")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="09:30 冷采集新鲜度轻哨 (只读)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="哨兵日 (默认今日; 读的是前一日审计行)")
    parser.add_argument("--no-notify", action="store_true", help="只打印不推送")
    args = parser.parse_args()
    return run(args.date, notify_disabled=args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
