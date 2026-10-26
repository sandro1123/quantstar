# -*- coding: utf-8 -*-
"""D0 卡 宇宙漂移哨兵 universe_sentinel — factor_daily 日行数环比台阶/锯齿判读轻哨。

治什么: factor_daily 静默缩宇宙零告警 (07-09 半日/07-21 跌 400/08-10 跌 290,
5969→5549 三台阶, 全史行数洞普查见 /tmp/d0_census_draft.md)。

口径 (派单 sm-10fc62f9 + 普查件):
  / 只读 — ro 连接零写库 (freshness_sentinel #116 同族轻哨形态)。
  / 判据 — T 日行数 vs 前 5 交易日均值跌幅 > 2% (UNIVERSE_DROP_PCT) → 疑似;
    T+1 回弹 ≥ 基线 95% → 瞬态锯齿轻提示 (不打扰); T+1 未回弹 → 永久台阶正式告警 rc=1。
  / 基线 = 前 5 交易日均值 (非前一日 — 锯齿日不进基线, 防污染)。
  / 历史不足 5 日 (新装机/表空) → 轻提示不告警 (诚实形态)。
  / 飞书告警 best-effort, 失败静默不哑哨。

用法 (cron 直驱, 参照 freshness_sentinel 行形态):
  PYTHONPATH=src CONFIG_ENV=prod python scripts/universe_sentinel.py [--date YYYY-MM-DD]

落档: 无文件落档 (行数面实时读库, 哨兵零冗余)。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

UNIVERSE_DROP_PCT = 2.0      # 环比跌幅阈值 (%)
BASELINE_DAYS = 5            # 基线窗口 (前 5 交易日)
REBOUND_RATIO = 0.99        # 锯齿自愈判据: T+1 ≥ base_used×99% — 真锯齿 T+1 几乎完全回弹;
                            # 95% 太宽, 台阶新稳态 (如 5539 vs 峰值 5830 -5%) 会卡线误判自愈


def _connect_ro():
    """ro 连接 — 与 freshness_sentinel._resolve_db_path 同源解析, 零写。"""
    import sqlite3
    db_path = _resolve_db_path()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    return conn


def _resolve_db_path() -> str:
    """与 Database() 同源解析, 但带存在性回落 — Database() 的相对路径随 cwd 走
    (worktree 形态下解析成 worktree 自身 data/qs.db 不存在, ro open 即炸)。
    回落序: Database() 解析 → 哨兵所在仓 data/qs.db → 生产 ~/qs/data/qs.db。
    禁 quantstar.db 空壳优先序 (历史坑)。"""
    from common.database import Database
    cands = []
    try:
        cands.append(Database().db_path)
    except Exception:
        pass
    cands.append(os.path.join(REPO_ROOT, "data", "qs.db"))
    cands.append(os.path.expanduser("~/qs/data/qs.db"))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return cands[-1]


def count_on(conn, date: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM factor_daily WHERE date=?", (date,)).fetchone()[0]


def recent_dates(conn, before: str, limit: int) -> list:
    """before 之前 (不含) 的最近 limit 个有行数记录日 (降序取, 返回升序)。"""
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM factor_daily WHERE date < ? "
        "GROUP BY date ORDER BY date DESC LIMIT ?",
        (before, limit)).fetchall()
    return [r[0] for r in rows][::-1]


def baseline_for(conn, target: str) -> float:
    """前 5 交易日**中位数**基线 — 均值口径会被窗口内锯齿日拉低
    (09-07 锯齿 5400 混入 4×5800 → 均值 5720), 中位数稳健。
    返回 0.0 表示历史不足。"""
    days = recent_dates(conn, target, BASELINE_DAYS)
    if len(days) < BASELINE_DAYS:
        return 0.0
    counts = sorted(count_on(conn, d) for d in days)
    mid = len(counts) // 2
    if len(counts) % 2 == 1:
        return float(counts[mid])
    return (counts[mid - 1] + counts[mid]) / 2.0


def peak_baseline_for(conn, target: str) -> float:
    """前 5 交易日**峰值**基线 — 反基线爬行面: 台阶落地后低行数日混入窗口,
    中位基线下移会吞掉后续台阶 (08-10 真实回放: 5830→5538 只报 -2.1%)。
    跌幅判据 = 中位口径 OR 峰值口径, 任一过阈即疑似。返回 0.0 表示历史不足。"""
    days = recent_dates(conn, target, BASELINE_DAYS)
    if len(days) < BASELINE_DAYS:
        return 0.0
    return float(max(count_on(conn, d) for d in days))


def _prev_date(d: str) -> str:
    """前一自然日 (哨兵读 T-1 面与 T-2/T-1 回弹面; 周末无行自然落空)。"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def send_alert(title: str, body: str) -> None:
    """飞书告警 — best-effort, 失败静默 (哨兵不能因告警失败而哑)。
    复用 freshness_sentinel 同构 (delivery_layer feishu_dm 通道)。"""
    try:
        from config_layer import get_config
        from delivery_layer import delivery
        from delivery_layer.message import Message
        cfg = get_config()
        webhook = cfg.get("delivery.feishu_webhook")
        if not webhook:
            return
        content = [{
            "tag": "lark_md",
            "content": body,
        }]
        delivery.registry.mark_healthy("feishu_dm")
        delivery.send(Message(content=content, content_type="universe_sentinel_alert",
                              title=title, force_channel="feishu_dm"))
    except Exception:
        pass  # best-effort: 告警失败不哑哨 (freshness_sentinel 同款纪律)


def run(target_date: str, notify_disabled: bool = False) -> int:
    """主判读: 读 target_date 及其前一日行数面, 判锯齿/台阶, 返回 rc。
    rc=0 正常或轻提示; rc=1 永久台阶正式告警。"""
    conn = _connect_ro()
    try:
        n_t = count_on(conn, target_date)
        prev_d = _prev_date(target_date)
        n_prev = count_on(conn, prev_d)
        base = baseline_for(conn, prev_d)
        if base == 0.0:
            print(f"[universe_sentinel] {target_date} 历史不足 {BASELINE_DAYS} 日, 轻提示不告警 "
                  f"(n={n_t})")
            return 0
        if n_prev == 0:
            print(f"[universe_sentinel] {prev_d} 无行 (假日/停跑), 跳过")
            return 0
        # 双口径基线: 中位 (抗锯齿污染) + 峰值 (抗基线爬行吞二次台阶), 任一过阈即疑似
        peak = peak_baseline_for(conn, prev_d)
        drop_mid = (base - n_prev) / base * 100.0 if base else 0.0
        drop_peak = (peak - n_prev) / peak * 100.0 if peak else 0.0
        drop_pct = max(drop_mid, drop_peak)   # 取更严口径; 显示用
        base_used = peak if drop_peak > drop_mid else base
        if drop_pct <= UNIVERSE_DROP_PCT:
            print(f"[universe_sentinel] OK {target_date}: n_prev={n_prev} "
                  f"base_mid={base:.0f} base_peak={peak:.0f} drop={drop_pct:+.2f}% "
                  f"(阈值 {UNIVERSE_DROP_PCT}%)")
            return 0
        lost = base_used - n_prev
        # 回弹面: T (target_date) 行未落库 (晨跑形态, n_t=0) → 无回弹证据,
        # 跌幅已过阈值 → 疑似台阶告警+待复判 (不能因当日未落库而静默)
        if n_t == 0:
            msg = (f"[universe_sentinel] ⚠️ 疑似台阶告警 (待复判): {prev_d} 行数 {n_prev} "
                   f"vs 基线 (中位 {base:.0f} / 峰值 {peak:.0f}) 跌 {lost:.0f} 只 (-{drop_pct:.1f}%), "
                   f"{target_date} 行未落库 — 若明日回弹≥基线95%则销案为锯齿, "
                   f"否则升级永久台阶 (请查 K线断供/资格闸门/源切换)")
            print(msg)
            if not notify_disabled:
                send_alert("⚠️ QS 宇宙漂移疑似台阶告警 (待复判)", msg)
            return 1
        rebound = n_t >= base_used * REBOUND_RATIO
        if rebound:
            msg = (f"[universe_sentinel] {prev_d} 瞬态锯齿自愈: 跌 {lost:.0f} 只 "
                   f"(-{drop_pct:.1f}%), {target_date} 已回弹 n={n_t} ≥ 基线95% — 轻提示不告警")
            print(msg)
            return 0
        msg = (f"[universe_sentinel] ⚠️ 永久台阶告警: {prev_d} 行数 {n_prev} vs 基线 "
               f"(中位 {base:.0f} / 峰值 {peak:.0f}) 跌 {lost:.0f} 只 (-{drop_pct:.1f}%), "
               f"{target_date} 未回弹 (n={n_t}, 回弹线 {base_used * REBOUND_RATIO:.0f}) — "
               f"疑似宇宙静默收缩, 请查 K线断供/资格闸门/源切换")
        print(msg)
        if not notify_disabled:
            send_alert("⚠️ QS 宇宙漂移台阶告警", msg)
        return 1
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="判读日 YYYY-MM-DD (缺省=今日)")
    parser.add_argument("--no-notify", action="store_true", help="禁飞书 (测试/预演)")
    args = parser.parse_args()
    d = args.date or datetime.now().strftime("%Y-%m-%d")
    return run(d, notify_disabled=args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
