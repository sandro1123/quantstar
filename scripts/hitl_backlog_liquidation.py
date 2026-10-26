#!/usr/bin/env python3
"""HITL 积压清算脚本 (Phase 1.6, 统筹 22:05 窗口执行)

处置 28 条 pending 积压 (08-27 实测):
  A批 id 42-50 (07-27, 无入场参数)  -> mark_invalid 过期作废
  B批 id 71-80 (08-14, 无入场参数)  -> mark_invalid 过期作废 (含 e2e 探针 71)
  C批 id 81-89 (08-26, 带入场参数)  -> approve(reviewer="auto-paper") + 台账行
      由下次 daily_update 的 consume_approved 消费; 入场区间基于 08-26 价格,
      entry_engine 价格守卫天然防追高 (离区间不触发入场)。

安全:
  * 默认 dry-run, 只打印处置计划不触库; --apply 才执行。
  * 只动 status='pending' 的行 (approve/mark_invalid 自带状态条件), 幂等。
  * A/B 批即使误批也会被 consume_approved 的 EX-01 缺参数闸拦下 (双保险)。
  * C批批准后发一条汇总 DM (不发 9 张卡)。

用法:
  CONFIG_ENV=prod ~/qs/.venv/bin/python \\
      scripts/hitl_backlog_liquidation.py            # dry-run
  CONFIG_ENV=prod ~/qs/.venv/bin/python \\
      scripts/hitl_backlog_liquidation.py --apply    # 执行
"""
import os
import sys

QS = os.path.expanduser("~/qs")
sys.path.insert(0, os.path.join(QS, "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(QS, ".env"))

from common.database import Database  # noqa: E402
from execution_layer.core.human_in_loop import HumanInLoop  # noqa: E402

EXPIRE_NOTE = "过期作废: 无入场参数且价格失效 (Phase 1.6 积压清算)"
AUTO_NOTE = "积压清算: 模拟盘自动批准"

# 批次定义来自 08-27 生产库实测; 执行时再按 status='pending' 过滤, 已处理的自动跳过
EXPIRE_IDS = list(range(42, 51)) + list(range(71, 81))   # A批 + B批
AUTO_IDS = list(range(81, 90))                            # C批


def main() -> int:
    apply = "--apply" in sys.argv
    hitl = HumanInLoop(Database())

    print(f"模式: {'APPLY (真写库)' if apply else 'DRY-RUN (不触库)'}")
    print("=" * 60)

    expired, approved, skipped = [], [], []

    for rid in EXPIRE_IDS:
        row = hitl.get(rid)
        if not row or row["status"] != "pending":
            skipped.append((rid, (row or {}).get("status", "missing")))
            continue
        print(f"[作废] #{rid} {row['code']} {row['decision_type']} "
              f"conf={row.get('confidence')} created={row.get('created_at')}")
        if apply and hitl.mark_invalid(rid, EXPIRE_NOTE):
            expired.append(rid)

    for rid in AUTO_IDS:
        row = hitl.get(rid)
        if not row or row["status"] != "pending":
            skipped.append((rid, (row or {}).get("status", "missing")))
            continue
        has_params = all(row.get(k) for k in
                         ("entry_low", "entry_high", "stop_loss", "take_profit"))
        if not has_params:
            # 与实测不符的安全闸: 缺参数的一律不作自动批 (EX-01 也会拦, 双保险)
            print(f"[跳过] #{rid} {row['code']} 缺入场参数, 不自动批 (转人工裁定)")
            skipped.append((rid, "no_params"))
            continue
        print(f"[自动批] #{rid} {row['code']} {row['decision_type']} "
              f"conf={row.get('confidence')} entry=[{row.get('entry_low')},"
              f"{row.get('entry_high')}] sl={row.get('stop_loss')} "
              f"tp={row.get('take_profit')}")
        if apply and hitl.approve(rid, reviewer="auto-paper", note=AUTO_NOTE):
            approved.append((rid, row))

    print("=" * 60)
    print(f"作废: {len(expired)}  自动批: {len(approved)}  跳过: {len(skipped)}")
    if skipped:
        print("跳过明细:", skipped)

    if not apply:
        print("\nDRY-RUN 结束。确认无误后加 --apply 执行。")
        return 0

    # 台账行 (best-effort, 失败不阻断)
    try:
        from delivery_layer.hitl_card_templates import build_signal_summary
        from delivery_layer.hitl_ledger import write_bitable_ledger
        for rid, row in approved:
            write_bitable_ledger(
                rid, row["code"], row["decision_type"], "approve", "auto-paper",
                AUTO_NOTE, "auto_paper",
                build_signal_summary(row["code"], row["decision_type"],
                                     float(row.get("score") or 0),
                                     float(row.get("confidence") or 0),
                                     row.get("reason") or ""))
    except Exception as e:
        print(f"台账写入异常 (不阻断): {e}")

    # 汇总 DM (一条, 不发 9 张卡)
    if approved:
        try:
            from config_layer import get_config
            from delivery_layer import DeliveryLayer, Message
            cfg = get_config()
            if not cfg._loaded:
                cfg.load()
            d = DeliveryLayer(config=cfg)
            d.registry.mark_healthy("feishu_dm")
            lines = [f"#{rid} {row['code']} conf={row.get('confidence')}"
                     for rid, row in approved]
            d.send(Message(
                content={"msg_type": "text", "content": {
                    "text": (f"【HITL积压清算】{len(approved)} 条信号已自动批准[模拟盘] "
                             f"(reviewer=auto-paper), 将由下次日更消费入场:\n"
                             + "\n".join(lines)
                             + f"\n另有 {len(expired)} 条过期信号作废。"
                               f"否决: qs review veto <id>")}},
                content_type="execution_alert",
                title="HITL积压清算完成",
                force_channel="feishu_hitl_dm",   # Phase 2: HITL 流量走新 app
            ))
            print("汇总 DM 已发送")
        except Exception as e:
            print(f"汇总 DM 发送异常 (不阻断): {e}")

    print("清算完成。C批将由下次 daily_update 的 consume_approved 消费。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
