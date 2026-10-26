"""ops_layer/alert_manager.py — 告警管理 + 升级机制 + 生命周期闭环 (OPS-07)

P0-3② 告警去重状态机 (三态, Monit/Netdata 状态迁移语义):
  首触发 = 无同名 open 告警 → 落库 + 推送
  持续   = 同名 open 告警已在 → 静默抑制 (不重复轰炸, 不限时长)
  恢复   = resolve_pipeline_alerts 自动关闭 + 推 recovery (OPS-07 已有)
持续期内唯一再推送通道 = check_escalation 升级 (24h 节流), 不再有时间窗重推。
"""

import os
from dataclasses import fields
from datetime import datetime

from common.database import Database
from common.logging import get_logger
from common.utils import bj_now
from ops_layer.core.models import Alert

logger = get_logger("ops.alert_manager")

_ESCALATION_DELAYS = {
    "critical": 300,    # 5 分钟无人响应 → 升级
    "warning": 1800,    # 30 分钟
    "info": 7200,       # 2 小时
}

_ESCALATION_REPUSH_SECONDS = 86400  # OPS-07: 同条告警 24h 内只重推一次


class AlertManager:
    def __init__(self, db: Database):
        self.db = db
        self._recent: dict[str, str] = {}  # title -> last_sent_time (进程内)
        self._unpersisted: set[str] = set()  # 修3: INSERT 失败进程内兜底 (防同进程重复推)

    def _has_open_alert(self, title: str) -> bool:
        """状态机核心: 是否存在同名 open 告警 (DB 为准, 跨进程一致)。"""
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE title = ? AND status = 'open' LIMIT 1",
                (title,),
            ).fetchone()
            return row is not None
        except Exception:
            return False  # 查询失败宁放勿抑 (告警链路故障不得静默真实告警)

    def should_alert(self, alert: Alert) -> bool:
        """P0-3② 三态去重: 同名 open 告警已在 → 持续期静默抑制 (不重复轰炸,
        不限时长); 无 open → 首触发放行 (恢复后复发是新事件, 不做时间窗抑制)。
        旧 30 分钟重推窗口已废 — 持续期唯一再推送通道 = check_escalation 升级。
        修3 (2026-09-05): INSERT 失败进程内兜底 — DB 无行但本进程已推过的
        title 抑制; 跨进程不抑制 (满盘持续时每采样点重推一条 = 告警本意)。"""
        if alert.title in self._unpersisted:
            return False
        return not self._has_open_alert(alert.title)

    def push_feishu(self, title: str, body: str, severity: str = "warning") -> bool:
        """飞书 App API 私聊推送告警（复用 delivery 层已验证实现，失败不阻塞）。"""
        try:
            app_id = os.environ.get("FEISHU_APP_ID", "")
            app_secret = os.environ.get("FEISHU_APP_SECRET", "")
            dm_id = os.environ.get("FEISHU_DM_ID", "")
            if not all([app_id, app_secret, dm_id]):
                logger.warning("飞书凭据未配置 (FEISHU_APP_ID/SECRET/DM_ID), 告警仅落库")
                return False

            from delivery_layer.channels.feishu_dm import _get_cached_token, _push_via_api
            token = _get_cached_token(app_id, app_secret)
            if not token:
                logger.warning("飞书 token 获取失败, 告警仅落库")
                return False

            emoji = {"critical": "🔴", "warning": "⚠️", "info": "✅"}.get(severity, "ℹ️")
            template = "red" if severity == "critical" else "green" if severity == "info" else "orange"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                    "template": template,
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**时间**: {now}\n{body}"}},
                ],
            }
            ok, err = _push_via_api(dm_id, {"msg_type": "interactive", "card": card}, token)
            if not ok:
                logger.warning(f"飞书告警推送失败: {err}")
            return ok
        except Exception as e:
            logger.warning(f"飞书告警推送异常: {e}")
            return False

    def send(self, alert: Alert) -> bool:
        if not self.should_alert(alert):
            # 抑制重复推送，不抑制持续观测；保留事件起点和升级状态。
            try:
                conn = self.db.get_connection()
                conn.execute(
                    "UPDATE alerts SET detail = ?, last_seen = ? "
                    "WHERE title = ? AND status = 'open'",
                    (alert.detail, bj_now(), alert.title),
                )
                conn.commit()
            except Exception as e:
                logger.warning(f"alerts refresh failed (fail-safe): {e}")
            return False
        alert.created_at = bj_now()
        # 修3 (2026-09-05): 通道反转 — 先推 Feishu (不写盘, 满盘时唯一可用
        # 通道), 再 INSERT alerts (fail-safe: 落库失败仅 warning, 不吞推送
        # 不炸调用方)。09-04 02:43 满盘实证: 旧序先 INSERT 后推送, ENOSPC 时
        # INSERT 抛错 → 推送走不到 → 全链零外发。返回值语义不变 = 首触发放行
        # (非「推送成功」— push_feishu 尽力而为, 既有 50 测契约保持)。
        self.push_feishu(
            f"[{alert.severity}] {alert.title}",
            f"**层级**: {alert.layer}\n{alert.detail}",
            severity=alert.severity,
        )
        try:
            conn = self.db.get_connection()
            conn.execute("""
                INSERT INTO alerts (severity, layer, title, detail, action, status, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """, (alert.severity, alert.layer, alert.title, alert.detail, alert.action,
                  alert.created_at, alert.created_at))
            conn.commit()
            self._recent[alert.title] = alert.created_at
            self._unpersisted.discard(alert.title)
        except Exception as e:
            self._unpersisted.add(alert.title)
            logger.warning(f"alerts INSERT failed (fail-safe, Feishu 推送已发): {e}")
        return True

    def get_open_alerts(self) -> list[Alert]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM alerts WHERE status = 'open' ORDER BY created_at DESC"
        ).fetchall()
        field_names = {f.name for f in fields(Alert)}
        return [Alert(**{k: dict(r)[k] for k in field_names if k in dict(r)}) for r in rows]

    def _has_last_escalated_column(self) -> bool:
        """OPS-07: alerts.last_escalated_at 列探测 (缓存)。

        存量表无此列, ALTER 属生产写操作需用户 GO (GO-3);
        列缺失时升级仍执行但不记重推时间 — 诚实降级, 不静默假装有去重。
        """
        if getattr(self, "_last_esc_col", None) is None:
            try:
                cols = {r["name"] for r in self.db.get_connection().execute(
                    "PRAGMA table_info(alerts)").fetchall()}
                self._last_esc_col = "last_escalated_at" in cols
                if not self._last_esc_col:
                    logger.warning("alerts.last_escalated_at 列不存在 (待 GO-3 ALTER), 升级重推 24h 去重暂不生效")
            except Exception:
                self._last_esc_col = False
        return self._last_esc_col

    def check_escalation(self) -> list[dict]:
        """检查超时未 resolve 的告警并升级。

        OPS-07: 升级不再只改 action 字段 — critical 超时真推飞书二次通知,
        同条告警 24h 内只重推一次 (last_escalated_at 去重)。
        """
        escalated = []
        now = datetime.now()
        conn = self.db.get_connection()
        has_col = self._has_last_escalated_column()
        rows = conn.execute(
            "SELECT * FROM alerts WHERE status = 'open' ORDER BY created_at ASC"
        ).fetchall()
        for r in rows:
            alert = dict(r)
            severity = alert.get("severity", "info")
            delay = _ESCALATION_DELAYS.get(severity, 3600)
            created = alert.get("created_at", "")
            try:
                created_dt = datetime.fromisoformat(created)
            except (ValueError, TypeError):
                continue
            if (now - created_dt).total_seconds() <= delay:
                continue
            # 24h 重推去重
            if has_col and alert.get("last_escalated_at"):
                try:
                    last_esc = datetime.fromisoformat(alert["last_escalated_at"])
                    if (now - last_esc).total_seconds() < _ESCALATION_REPUSH_SECONDS:
                        continue
                except (ValueError, TypeError):
                    pass
            entry = {
                "id": alert["id"],
                "title": alert["title"],
                "severity": severity,
                "created_at": created,
                "elapsed_s": int((now - created_dt).total_seconds()),
                "action": "escalate",
            }
            # critical 真推送二次通知; 其余级别仅标记 (第一期不做多人升级链)
            if severity == "critical":
                pushed = self.push_feishu(
                    f"[升级] {alert['title']}",
                    f"告警超过 {delay // 60} 分钟未处理, 二次提醒\n"
                    f"**层级**: {alert.get('layer', '')}\n{alert.get('detail', '')[:500]}",
                    severity="critical",
                )
                entry["pushed"] = pushed
            escalated.append(entry)
            if has_col:
                conn.execute(
                    "UPDATE alerts SET action = 'escalated', last_escalated_at = ? WHERE id = ?",
                    (bj_now(), alert["id"]))
            else:
                conn.execute("UPDATE alerts SET action = 'escalated' WHERE id = ?", (alert["id"],))
        conn.commit()
        return escalated

    def resolve(self, alert_id: int) -> bool:
        conn = self.db.get_connection()
        cur = conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'open'",
            (bj_now(), alert_id)
        )
        conn.commit()
        return cur.rowcount > 0

    def dismiss(self, alert_id: int) -> bool:
        """OPS-07: 驳回告警 (无需处理/误报), 与 resolved 区分留痕。"""
        conn = self.db.get_connection()
        cur = conn.execute(
            "UPDATE alerts SET status = 'dismissed', resolved_at = ? WHERE id = ? AND status = 'open'",
            (bj_now(), alert_id)
        )
        conn.commit()
        return cur.rowcount > 0

    def resolve_pipeline_alerts(self, pipeline_name: str) -> int:
        """OPS-07 恢复通知: 管道转 success 时自动 resolve 同管道 open 告警。

        匹配 orchestrator 告警标题格式 "管道失败: <name>" (含 CLI 崩溃标题)。
        返回 resolve 条数; 有恢复时推"已恢复" info 通知。
        """
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT id, title FROM alerts WHERE status = 'open' AND title LIKE ?",
            (f"%{pipeline_name}%",),
        ).fetchall()
        if not rows:
            return 0
        now = bj_now()
        for r in rows:
            conn.execute(
                "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ?",
                (now, r["id"]))
        conn.commit()
        self.push_feishu(
            f"已恢复: {pipeline_name}",
            f"管道 {pipeline_name} 恢复正常运行, 自动关闭 {len(rows)} 条相关告警",
            severity="info",
        )
        logger.info(f"pipeline {pipeline_name} recovered, auto-resolved {len(rows)} alert(s)")
        return len(rows)

    def create_alert(self, title: str, detail: str, severity: str = "warning", layer: str = "ops") -> bool:
        alert = Alert(severity=severity, layer=layer, title=title, detail=detail)
        return self.send(alert)
