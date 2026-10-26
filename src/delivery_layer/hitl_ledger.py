"""HITL Bitable 台账 — 审批/否决留痕写多维表格

Phase 1.6 从 hitl_ws_daemon.py 抽出, 供三个调用方共用:
  * hitl_ws_daemon  — ws 回调 (approve/reject/veto)
  * plan_manager    — 模拟盘自动批准 (auto-paper)
  * scripts/hitl_backlog_liquidation.py — 积压清算

缺配置 (FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_APPROVAL_TABLE) 时跳过,
不阻断审批 — 台账是审计件, 不是主流程闸门。
"""

import os

import requests

from common.logging import get_logger
from common.utils import bj_now
from delivery_layer.channels.feishu_dm import FEISHU_API_BASE, _get_cached_token

logger = get_logger("delivery.hitl_ledger")


def write_bitable_ledger(review_id: int, code: str, decision_type: str, action: str,
                         reviewer: str, note: str, channel: str,
                         signal_summary: str, reviewed_at: str = "") -> bool:
    """A案台账: 审批留痕写多维表格 (缺配置则跳过, 不阻断审批)"""
    # 台账专用 base 优先 (HITL app 自建自持, 绕开 quantmaster 旧 base 权限死结);
    # 缺则回退共享键, 兼容旧配置。
    app_token = (os.environ.get("FEISHU_BITABLE_LEDGER_APP_TOKEN")
                 or os.environ.get("FEISHU_BITABLE_APP_TOKEN", ""))
    table_id = os.environ.get("FEISHU_BITABLE_APPROVAL_TABLE", "")
    if not (app_token and table_id):
        logger.info("Bitable 台账未配置 (FEISHU_BITABLE_APPROVAL_TABLE 缺失), 跳过")
        return False
    # Phase 2: 台账是 HITL 流量, 用新 app 凭据 (新 app 已加为 base 协作者);
    # 缺则回退共用键, 与 daemon 切换点 A 同语义。
    token = _get_cached_token(
        os.environ.get("FEISHU_HITL_APP_ID") or os.environ.get("FEISHU_APP_ID", ""),
        os.environ.get("FEISHU_HITL_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET", ""))
    if not token:
        return False
    try:
        resp = requests.post(
            FEISHU_API_BASE + "/bitable/v1/apps/" + app_token
            + "/tables/" + table_id + "/records",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"},
            json={"fields": {
                # 台账表 review_id 列建成了 Text (type=1), 传 int 会被飞书拒:
                # code=1254060 TextFieldConvFail (08-27 03:08 实战翻车)。
                # 设计稿里写的是"数字", 实际建表用了文本 —— 以实际 schema 为准。
                "review_id": str(review_id),
                "code": code,
                "decision_type": decision_type,
                "action": action,
                "reviewer": reviewer,
                "reviewed_at": reviewed_at or bj_now(),
                "note": note,
                "channel": channel,
                "signal_summary": signal_summary,
            }},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True
        logger.warning(f"Bitable 台账写入失败 review_id={review_id}: "
                       f"code={data.get('code')} msg={data.get('msg')}")
    except Exception as e:
        logger.warning(f"Bitable 台账写入异常 review_id={review_id}: {e}")
    return False


def ledger_selfcheck() -> dict:
    """启动真写探测 (教训#4: 能读证明不了能写, 写探测须真写一行再删)。

    在台账表真实写入一行探针记录, 立即删除, 返回探测结果 dict。
    403/1254060 等权限类失败会明确给出可读指引 — 91403 断链事故的对策:
    新 app 未加为台账 base 协作者 (可编辑) 时, 这里直接红牌, 而不是
    等第一张审批卡回调后静默断链 13 行。

    返回: {"ok": bool, "detail": str}
      ok=True   探针行写入+删除全通 (或未配置=跳过, 由 caller 决定语义)
      ok=False  写入或删除失败, detail 含飞书 code/msg
    """
    app_token = (os.environ.get("FEISHU_BITABLE_LEDGER_APP_TOKEN")
                 or os.environ.get("FEISHU_BITABLE_APP_TOKEN", ""))
    table_id = os.environ.get("FEISHU_BITABLE_APPROVAL_TABLE", "")
    if not (app_token and table_id):
        return {"ok": False, "detail": "ledger unconfigured "
                "(FEISHU_BITABLE_APPROVAL_TABLE missing) - probe skipped"}

    creds_app_id = (os.environ.get("FEISHU_HITL_APP_ID")
                    or os.environ.get("FEISHU_APP_ID", ""))
    creds_secret = (os.environ.get("FEISHU_HITL_APP_SECRET")
                    or os.environ.get("FEISHU_APP_SECRET", ""))
    token = _get_cached_token(creds_app_id, creds_secret)
    if not token:
        return {"ok": False, "detail": "tenant_access_token acquire failed - "
                "check FEISHU_HITL_APP_ID/SECRET"}

    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    base = FEISHU_API_BASE + "/bitable/v1/apps/" + app_token         + "/tables/" + table_id + "/records"

    # 1) 真写一行探针 (非空字段全按台账列名, 与正式写入同 schema)
    probe_fields = {
        "review_id": "0",
        "code": "PROBE",
        "decision_type": "probe",
        "action": "selfcheck",
        "reviewer": "startup_probe",
        "reviewed_at": bj_now(),
        "note": "daemon 启动写探测, 即将自删",
        "channel": "startup_probe",
        "signal_summary": "write-probe row, deleted immediately",
    }
    try:
        resp = requests.post(base, headers=headers,
                             json={"fields": probe_fields}, timeout=15)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "detail": f"probe POST exception: {e}"}
    if data.get("code") != 0:
        code, msg = data.get("code"), data.get("msg")
        hint = ""
        if code == 403 or "permission" in str(msg).lower():
            hint = (" | 新 app 须加为台账 base 协作者 (可编辑), "
                    "否则 token 能拿但写入被拒 (91403 断链同因)")
        return {"ok": False, "detail": f"probe POST rejected: code={code} "
                f"msg={msg}{hint}"}
    record_id = ((data.get("data") or {}).get("record") or {}).get("record_id") or ""
    if not record_id:
        return {"ok": False, "detail": "probe POST ok but record_id missing"}

    # 2) 立即删除探针行 (不留垃圾在台账)
    try:
        resp = requests.delete(base + "/" + record_id, headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "detail": f"probe DELETE exception (record {record_id} "
                f"left in table, manual cleanup needed): {e}"}
    if data.get("code") != 0:
        return {"ok": False, "detail": f"probe DELETE rejected: code={data.get('code')} "
                f"msg={data.get('msg')} (record {record_id} left, manual cleanup)"}
    return {"ok": True, "detail": f"probe ok: wrote+deleted {record_id}"}
