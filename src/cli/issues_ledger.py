"""issues Bitable 台账 — 问题台账读写 (批 4)

设计稿: 分层讨论/每日问题报告设计稿-A-20260828.md §4
载体: 统筹自建 base (FEISHU_BITABLE_ISSUES_APP_TOKEN) 的 issues_ledger 表
(FEISHU_BITABLE_ISSUES_TABLE), 9 态状态机 pending→…→closed/dismissed。

缺配置或无权限时优雅降级 (返回 ok:false + code, 不抛不静默) —
台账是交接件, 不是主流程闸门 (与 hitl_ledger 同语义)。
教训 #4: 能读证明不了能写 — 接线验收须真写一行再删 (写探测)。
坑入账: Bitable search 返回 Text 列是分段数组 [{"text":...}], 须 text_of 解包。
"""

import json
import os

import requests

from common.logging import get_logger

logger = get_logger("cli.issues_ledger")

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 9 态状态机 (设计稿 §4.3) — sync 写入的初始态
INITIAL_STATUS = "pending"
# 统筹巡检拾取态 (issues pending 子命令读取范围)
PICKUP_STATUSES = ("pending", "user_escalated", "auto_assigned")
# 不同步的严重度 — info 是折叠汇总 (僵尸批/mass-skip 明细在 evidence), 入台账=噪音
SYNC_SEVERITIES = ("critical", "warning")

def text_of(value) -> str:
    """Bitable Text 列分段数组解包 — [{"text":"a"},{"text":"b"}] → "ab"。

    08-27 仪表盘坑入账: search 返回的 Text 列是分段数组, 直接 str() 会写出
    "[{'text': ...}]" 垃圾导致 dedup_key 比对永远失配 → 幂等破功。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                       for seg in value)
    return str(value)

def _ledger_config() -> tuple[str, str, str] | None:
    """(app_token, table_id, tenant_token) — 缺配置/取 token 失败返回 None。

    base 键: FEISHU_BITABLE_ISSUES_APP_TOKEN (统筹自建 base);
    app 凭据: FEISHU_APP_ID/SECRET (主 app, 须已被加为该 base 协作者可编辑)。
    """
    from dotenv import load_dotenv
    load_dotenv()
    app_token = os.environ.get("FEISHU_BITABLE_ISSUES_APP_TOKEN", "")
    table_id = os.environ.get("FEISHU_BITABLE_ISSUES_TABLE", "")
    if not (app_token and table_id):
        logger.info("issues 台账未配置 (FEISHU_BITABLE_ISSUES_APP_TOKEN/TABLE 缺失)")
        return None
    from delivery_layer.channels.feishu_dm import _get_cached_token
    token = _get_cached_token(
        os.environ.get("FEISHU_APP_ID", ""), os.environ.get("FEISHU_APP_SECRET", ""))
    if not token:
        logger.warning("issues 台账: tenant_access_token 获取失败")
        return None
    return app_token, table_id, token

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _find_by_dedup_key(app_token: str, table_id: str, token: str,
                       dedup_key: str) -> str | None:
    """按 dedup_key 查既有记录 → record_id (幂等 upsert 依据); 查无/出错返回 None。"""
    resp = requests.post(
        f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
        headers=_headers(token),
        json={"filter": {"conjunction": "and", "conditions": [
            {"field_name": "dedup_key", "operator": "is", "value": [dedup_key]}]},
              "page_size": 1},
        timeout=15)
    j = resp.json()
    if j.get("code") != 0:
        raise RuntimeError(f"search code={j.get('code')} msg={j.get('msg')}")
    items = (j.get("data") or {}).get("items") or []
    return items[0]["record_id"] if items else None

def sync_issues(issues: list[dict]) -> dict:
    """问题清单 → 台账幂等 upsert。返回 {ok, created, updated, skipped, errors}。

    - severity=info 跳过 (折叠汇总不入台账);
    - dedup_key 已存在 → 更新 last_seen/occurrences_7d/detail (不重置 status,
      统筹已派单的件不被 sync 打回 pending);
    - 不存在 → 新建 status=pending。
    """
    cfg = _ledger_config()
    if cfg is None:
        return {"ok": False, "code": "LEDGER_NOT_CONFIGURED",
                "message_zh": "issues 台账未配置或凭据缺失 — 检查 .env 三键与 app 协作者权限"}
    app_token, table_id, token = cfg
    created = updated = skipped = 0
    errors = []
    for it in issues:
        if it.get("severity") not in SYNC_SEVERITIES:
            skipped += 1
            continue
        dedup_key = it["dedup_key"]
        try:
            rid = _find_by_dedup_key(app_token, table_id, token, dedup_key)
            if rid:
                resp = requests.put(
                    f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}"
                    f"/records/{rid}",
                    headers=_headers(token),
                    json={"fields": {
                        "last_seen": it.get("first_seen", ""),
                        "occurrences_7d": it.get("occurrences_7d", 1),
                        "detail_zh": it.get("detail_zh", "")[:1000],
                    }}, timeout=15)
                j = resp.json()
                if j.get("code") == 0:
                    updated += 1
                else:
                    errors.append(f"{dedup_key}: update code={j.get('code')}")
            else:
                resp = requests.post(
                    f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                    headers=_headers(token),
                    json={"fields": {
                        "dedup_key": dedup_key,
                        "title_zh": it.get("title_zh", ""),
                        "detail_zh": it.get("detail_zh", "")[:1000],
                        "source": it.get("source", ""),
                        "severity": it.get("severity", ""),
                        "evidence_json": json.dumps(
                            it.get("evidence", {}), ensure_ascii=False)[:2000],
                        "status": INITIAL_STATUS,
                        "first_seen": it.get("first_seen", ""),
                        "last_seen": it.get("first_seen", ""),
                        "occurrences_7d": it.get("occurrences_7d", 1),
                    }}, timeout=15)
                j = resp.json()
                if j.get("code") == 0:
                    created += 1
                else:
                    errors.append(f"{dedup_key}: create code={j.get('code')} {j.get('msg')}")
        except (requests.RequestException, RuntimeError) as e:
            errors.append(f"{dedup_key}: {e}")
    ok = not errors
    return {"ok": ok, "created": created, "updated": updated,
            "skipped_info": skipped, "errors": errors[:10],
            **({} if ok else {"code": "SYNC_PARTIAL",
                              "message_zh": f"{len(errors)} 条写入失败 (前 10 见 errors)"}),
            **({} if cfg else {})}

def read_pending() -> dict:
    """读台账待派单行 (status ∈ pending/user_escalated/auto_assigned) — 统筹巡检用。"""
    cfg = _ledger_config()
    if cfg is None:
        return {"ok": False, "code": "LEDGER_NOT_CONFIGURED",
                "message_zh": "issues 台账未配置或凭据缺失"}
    app_token, table_id, token = cfg
    try:
        resp = requests.post(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            headers=_headers(token),
            json={"filter": {"conjunction": "or", "conditions": [
                {"field_name": "status", "operator": "is", "value": [s]}
                for s in PICKUP_STATUSES]},
                  "page_size": 100},
            timeout=15)
        j = resp.json()
        if j.get("code") != 0:
            return {"ok": False, "code": "SEARCH_FAILED",
                    "message_zh": f"search code={j.get('code')} {j.get('msg')}"}
        rows = []
        for item in (j.get("data") or {}).get("items") or []:
            f = item.get("fields", {})
            rows.append({
                "record_id": item["record_id"],
                "dedup_key": text_of(f.get("dedup_key")),
                "title_zh": text_of(f.get("title_zh")),
                "source": text_of(f.get("source")),
                "severity": text_of(f.get("severity")),
                "status": text_of(f.get("status")),
                "first_seen": text_of(f.get("first_seen")),
                "occurrences_7d": f.get("occurrences_7d"),
            })
        sev_rank = {"critical": 0, "warning": 1, "info": 2}
        rows.sort(key=lambda r: (sev_rank.get(r["severity"], 9), r["first_seen"]))
        return {"ok": True, "total": len(rows), "pending": rows}
    except requests.RequestException as e:
        return {"ok": False, "code": "NETWORK_ERROR", "message_zh": str(e)[:300]}
