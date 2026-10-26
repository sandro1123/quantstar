#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_marimo_report.py — Phase 2 Marimo 每日深钻报告编排器（09 层呈现）.

运行：~/qs-marimo-venv/bin/python scripts/build_marimo_report.py
纪律：marimo 不进生产 .venv（独立 venv 解释器）；读 qs.db 只读，唯一写表 delivery_log（铁律#12 留痕）；
     铁律#11 脚本自载 dotenv；Q-D4 裁定 HTML 归档只留 host reports/html/，信号数据不出机，DM 摘要卡照发。

退出码：0=全部成功 / 1=配置缺失或全部页面失败 / 2=PARTIAL（部分页面或 DM 失败）。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

QS = Path(os.path.expanduser("~/qs"))
SCRIPT_DIR = Path(__file__).resolve().parent
PAGES_DIR = SCRIPT_DIR / "marimo_pages"
MARIMO_BIN = os.path.expanduser("~/qs-marimo-venv/bin/marimo")
API = "https://open.feishu.cn/open-apis"
DB_URI_RW = "file:~/qs/data/qs.db"
PAGE_TIMEOUT_S = 120

PAGES = [
    ("p0_overview", "P0 总览 + 行动卡"),
    ("p1_market", "P1 市场环境"),
    ("p2_factors", "P2 因子健康"),
    ("p3_performance", "P3 模型与 Agent 绩效"),
    ("p4_gaps", "P4 回测与纸面交易（缺口占位）"),
    ("p5_quality", "P5 数据新鲜度与质量"),
    ("p6_ops", "P6 运维全景"),
]

# #76 周期分型：content_type 对齐 ops.yaml 既有管道名（yaml 预定义零 pipeline_runs
# = 三段断链证据：yaml 建了 / 脚本无周期参数 / crontab 无行。本卡补齐中间段。
# dm_label：daily 逐字保持现网标题（向后兼容）；weekly/monthly 用周报/月报。
PERIODS = {
    "daily": {"content_type": "marimo_report", "dm_label": "每日深钻报告"},
    "weekly": {"content_type": "marimo_weekly_report", "dm_label": "周报"},
    "monthly": {"content_type": "marimo_monthly_report", "dm_label": "月报"},
}

load_dotenv(QS / ".env")
sys.path.insert(0, str(PAGES_DIR))
import _common as C  # noqa: E402  dotenv 须先加载

# TASK_20260922_01 Phase B (裁决3): DM 卡理由位与晚报走同一 _pick_reason 单点。
# 生产跑在 marimo venv (依赖差, 无法 import report_layer 包), report_reason
# 是 src 顶层零依赖模块; PYTHONPATH 未含 src 时回退插仓内 src 路径。
try:
    from report_reason import _pick_reason_card as _pick_reason  # noqa: E402,F401
except ImportError:  # pragma: no cover - 生产 marimo venv 走此路
    sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))
    from report_reason import _pick_reason_card as _pick_reason  # noqa: E402,F401


def parse_args(argv=None):
    """#76：参数解析抽出为纯函数（原 main() 内联 argparse），供测试直接调用。

    --pages 未知页在此即拒（argparse choices 派生自 PAGES 键），保留 main()
    rc=1 双保险语义：显式 --pages 仍走 main() 的白名单复核（早于本卡的调用
    习惯不变）。"""
    _page_keys = [k for k, _ in PAGES]

    class _PagesAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            want = [p.strip() for p in str(values).split(",") if p.strip()]
            bad = [p for p in want if p not in _page_keys]
            if bad:
                parser.error(f"未知页: {bad}；可用={_page_keys}")
            setattr(namespace, self.dest, ",".join(want))

    ap = argparse.ArgumentParser(description="Marimo 每日深钻报告编排")
    ap.add_argument("--period", default="daily", choices=sorted(PERIODS),
                    help="报告周期（#76：daily/weekly/monthly，默认 daily 完全向后兼容）")
    ap.add_argument("--date", default=None,
                    help="数据截至日（默认取 pipeline_runs MAX date）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只生成归档与 index，不发 DM 摘要卡")
    ap.add_argument("--pages", default=None, action=_PagesAction,
                    help="逗号分隔页键子集（默认全 7 页；未知页即拒）")
    return ap.parse_args(argv)


def outdir_for(period, asof):
    """归档路径：reports/html/{period}/{asof}/。daily 与既有路径逐字一致。"""
    return QS / "reports" / "html" / period / asof


def content_type_for(period):
    """delivery_log content_type：对齐 ops.yaml 管道名（三段断链补中段）。"""
    return PERIODS[period]["content_type"]


def dm_title_for(period, asof):
    return f"📊 QS {PERIODS[period]['dm_label']} {asof}"


def export_env_for(period):
    """export 子进程环境：注入 MARIMO_REPORT_PERIOD（页窗口周期感知通道）。"""
    env = os.environ.copy()
    env["MARIMO_REPORT_PERIOD"] = period
    return env


def log_delivery(content_type, channel, status, message_id, error="", duration_ms=0):
    """铁律#12：每段留痕；只写 delivery_log（独立 RW 短连接）。"""
    try:
        conn = sqlite3.connect(DB_URI_RW, uri=True)
        try:
            conn.execute(
                "INSERT INTO delivery_log(message_id, content_type, channel, status,"
                " error, duration_ms, created_at) VALUES (?,?,?,?,?,?,?)",
                (message_id, content_type, channel, status, error, duration_ms, C.bj_now()))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # 留痕失败不阻断主流程，但必须 WARN（不可静默）
        print(f"[WARN] delivery_log 写入失败: {e}", file=sys.stderr)


_token_cache = {}


def _tenant_token():
    import requests
    now = time.time()
    if _token_cache.get("expire", 0) > now + 60:
        return _token_cache["token"]
    r = requests.post(f"{API}/auth/v3/tenant_access_token/internal",
                      json={"app_id": os.environ["FEISHU_APP_ID"],
                            "app_secret": os.environ["FEISHU_APP_SECRET"]},
                      timeout=30)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"tenant_token code={d.get('code')} msg={d.get('msg')}")
    _token_cache["token"] = d["tenant_access_token"]
    _token_cache["expire"] = now + d.get("expire", 3600)
    return _token_cache["token"]


def send_dm(card):
    """DM 摘要卡（Q-D4：只出摘要，全页 HTML 不出机）。返回 (ok, message_id 或 error)。"""
    import requests
    dm_id = os.environ.get("FEISHU_DM_ID", "")
    if not dm_id:
        return False, "FEISHU_DM_ID 缺失"
    payload = {"receive_id": dm_id, "msg_type": "interactive",
               "content": json.dumps(card, ensure_ascii=False)}
    r = requests.post(f"{API}/im/v1/messages?receive_id_type=chat_id",
                      headers={"Authorization": f"Bearer {_tenant_token()}"},
                      json=payload, timeout=30)
    d = r.json()
    if d.get("code") == 0:
        return True, d["data"]["message_id"]
    return False, f"code={d.get('code')} msg={d.get('msg')}"


def export_pages(outdir, keys, period="daily"):
    """逐页 export html（不加 --sandbox，坑点附录 D）。

    #76：period 经 MARIMO_REPORT_PERIOD 注入子进程环境（页内 C.report_period()
    读同一变量调整时间窗），不污染当前进程。返回 [(key, ok, error, ms)]。"""
    results = []
    env = export_env_for(period)
    for key in keys:
        src = PAGES_DIR / f"{key}.py"
        out = outdir / f"{key}.html"
        t0 = time.time()
        try:
            p = subprocess.run(
                [MARIMO_BIN, "export", "html", str(src), "-o", str(out)],
                cwd=str(PAGES_DIR), capture_output=True, text=True,
                timeout=PAGE_TIMEOUT_S, env=env)
            ms = int((time.time() - t0) * 1000)
            if p.returncode == 0 and out.exists():
                results.append((key, True, "", ms))
            else:
                err = (p.stderr or p.stdout or f"rc={p.returncode}").strip()[-400:]
                results.append((key, False, err, ms))
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            results.append((key, False, str(e)[-400:], ms))
        key2, ok, err, ms = results[-1]
        log_delivery(content_type_for(period), "host_archive",
                     "success" if ok else "failed", uuid.uuid4().hex[:12],
                     error=err, duration_ms=ms)
        print(f"[{'OK' if ok else 'FAIL'}] {key2} ({ms}ms)"
              + ("" if ok else f" {err[:200]}"))
    return results


def build_index(outdir, asof, stale, results, period="daily"):
    """index.html 导航枢纽（带水印，铁律#12）。"""
    titles = dict(PAGES)
    items = "\n".join(
        f"<li><a href='{key}.html'>{titles[key]}</a>"
        + ("" if ok else " <b style='color:#c00;'>（生成失败）</b>") + "</li>"
        for key, ok, _err, _ms in results)
    stale_html = "；".join(stale) if stale else "无"
    html = f"""<!DOCTYPE html>
<html lang='zh'><head><meta charset='utf-8'>
<title>QS {PERIODS[period]['dm_label']} {asof}</title>
<style>
 body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:760px;
      margin:32px auto;padding:0 16px;color:#222;}}
 a{{color:#0550ae;text-decoration:none;}} a:hover{{text-decoration:underline;}}
 .wm{{border-top:1px solid #ddd;margin-top:24px;padding-top:12px;
     font-size:12px;color:#666;}}
 .stale{{color:#8a8a8a;}}
</style></head><body>
<h1>QS {PERIODS[period]['dm_label']}（数据截至 {asof}）</h1>
<ul>
{items}
</ul>
<div class='wm'>生成时刻：{C.bj_now()}｜数据截至日：{asof}<br>
陈旧项：<span class='stale'>{stale_html}</span><br>
归档位置：host reports/html/{period}/{asof}/（Q-D4 裁定：信号数据不出机）</div>
</body></html>"""
    (outdir / "index.html").write_text(html, encoding="utf-8")


# 审批状态展示层中文化 (仅 DM 卡展示, human_review_queue 库值不变)
_REVIEW_STATUS_CN = {"pending": "待审批", "approved": "已批准",
                     "rejected": "已驳回", "invalid": "已失效",
                     "not_queued": "未送审"}


def build_dm_card(asof, ok_count, total, outdir, period="daily"):
    """DM 摘要卡：运维一行 + 行动卡 Top3 (中文化措辞, 剔除内部通报)。

    措辞对齐晚报: 止损→参考卖出线; score→综合分; 审批状态中文。
    ok_count/total 保留在签名 (调用面不变), 但「N/M 页生成成功」属运维计数,
    不进用户卡片 (运维面由 ops_oneliner 与 delivery_log 承担)。
    """
    del ok_count, total, outdir  # 页数/归档路径不进用户卡 (签名保留向后兼容)
    lines = [f"**数据截至日**：{asof}｜生成 {C.bj_now()}", ""]
    try:
        lines.append(C.ops_oneliner())
    except Exception as e:
        lines.append(f"（运维摘要读取失败：{e}）")
    lines += ["", "**行动卡 Top3**"]
    try:
        sig = C.latest_signals()
    except Exception as e:
        sig = None
        lines.append(f"（行动卡读取失败：{e}）")
    if sig is not None and len(sig):
        for _, r in sig.head(3).iterrows():
            review = _REVIEW_STATUS_CN.get(str(r.get("review_status", "")),
                                           str(r.get("review_status", "?")))
            # 理由位 = 单点闸 (裁决3): advisor_text 存在即用, 排名模板降级
            # 「量化评分入选，个股依据生成中」, 永不回退排名话术充数。
            # P0-1 (2026-09-28): 论证卡 — rationale 通电后自动升级多字段卡
            why = _pick_reason({"advisor_text": r.get("advisor_text"),
                                "added_reason": r.get("one_line_reason", "")})
            lines.append(
                f"{r.get('rank', '?')}. **{r.get('code', '?')} {r.get('name', '?')}**"
                f"｜综合分 {r.get('composite_score', r.get('score', '?'))}"
                f"｜买区间 {r.get('buy_range', '?')}"
                f"｜参考卖出线 {r.get('stop_loss', '?')}"
                f"｜审批 {review}｜{why}")
    else:
        lines.append("（近 14 日无待入场信号——空窗如实报）")
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": dm_title_for(period, asof)},
                       "template": "blue"},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md",
                                   "content": "\n".join(lines)}}]}


def main():
    args = parse_args()

    keys = [k for k, _ in PAGES]
    if args.pages:
        want = [p.strip() for p in args.pages.split(",") if p.strip()]
        bad = [p for p in want if p not in keys]
        if bad:
            print(f"[ERR] 未知页: {bad}；可用={keys}", file=sys.stderr)
            return 1
        keys = want

    if not Path(MARIMO_BIN).exists():
        print(f"[ERR] marimo CLI 缺失: {MARIMO_BIN}", file=sys.stderr)
        return 1

    asof = args.date or C.data_asof()
    stale = C.staleness_report(asof)
    outdir = outdir_for(args.period, asof)
    outdir.mkdir(parents=True, exist_ok=True)

    results = export_pages(outdir, keys, period=args.period)
    ok = [k for k, o, _e, _m in results if o]
    build_index(outdir, asof, stale, results, period=args.period)

    if not ok:
        print("[ERR] 全部页面失败", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY-RUN] 归档完成: {outdir}；跳过 DM 发送")
        return 0 if len(ok) == len(keys) else 2

    t0 = time.time()
    try:
        card = build_dm_card(asof, len(ok), len(keys), outdir, period=args.period)
        dm_ok, dm_info = send_dm(card)
    except Exception as e:
        dm_ok, dm_info = False, str(e)[-400:]
    log_delivery(content_type_for(args.period), "feishu_dm",
                 "success" if dm_ok else "failed",
                 dm_info if dm_ok else uuid.uuid4().hex[:12],
                 error="" if dm_ok else dm_info,
                 duration_ms=int((time.time() - t0) * 1000))

    if len(ok) == len(keys) and dm_ok:
        print(f"[DONE] {len(ok)}/{len(keys)} 页 + DM 卡已发（message_id={dm_info}）")
        return 0
    print(f"[PARTIAL] pages={len(ok)}/{len(keys)} "
          f"dm={'ok' if dm_ok else 'FAIL ' + str(dm_info)[:200]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
