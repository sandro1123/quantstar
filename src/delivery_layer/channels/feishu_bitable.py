"""Feishu Bitable sync — sync stock_pool/action_records/backtest_runs to Bitable"""
import json
import os

from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.message import Message

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class FeishuBitableChannel(Channel):
    @property
    def name(self): return "feishu_bitable"

    def _get_tenant_token(self) -> str | None:
        app_id = os.getenv("FEISHU_APP_ID")
        app_secret = os.getenv("FEISHU_APP_SECRET")
        if not app_id or not app_secret or not HAS_REQUESTS:
            return None
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            data = resp.json()
            return data.get("tenant_access_token")
        except Exception:
            return None

    def _append_record(self, table_id: str, fields: dict, token: str) -> bool:
        app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN")
        if not app_token or not HAS_REQUESTS:
            return False
        try:
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def send(self, message: Message) -> DeliveryResult:
        app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN")
        if not app_token:
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="FEISHU_BITABLE_APP_TOKEN not set")
        if not HAS_REQUESTS:
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="requests library not installed")
        token = self._get_tenant_token()
        if not token:
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="Failed to get tenant token")
        content = message.content
        content_type = message.content_type
        table_map = {
            "evening_report": os.getenv("FEISHU_BITABLE_REPORT_TABLE", ""),
            "signal": os.getenv("FEISHU_BITABLE_SIGNAL_TABLE", ""),
            "backtest_result": os.getenv("FEISHU_BITABLE_BACKTEST_TABLE", ""),
            # 仪表盘三表（2026-08-27 建，c 轨）
            "ops_job_status": os.getenv("FEISHU_BITABLE_DASHBOARD_JOB_TABLE", ""),
            "ops_digest": os.getenv("FEISHU_BITABLE_DASHBOARD_DIGEST_TABLE", ""),
            "ops_alert": os.getenv("FEISHU_BITABLE_DASHBOARD_ALERT_TABLE", ""),
        }
        table_id = table_map.get(content_type, "")
        if not table_id:
            return DeliveryResult(message_id="bitable_skip", channel="feishu_bitable", status="success",
                                  error_msg=f"No table mapping for {content_type}")
        if isinstance(content, dict):
            fields = {}
            for key, value in content.items():
                if isinstance(value, (str, int, float, bool)):
                    fields[key] = value
                elif isinstance(value, (list, dict)):
                    fields[key] = json.dumps(value, ensure_ascii=False)
            success = self._append_record(table_id, fields, token)
            if success:
                return DeliveryResult(message_id="bitable_record", channel="feishu_bitable", status="success")
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="Failed to append record")
        return DeliveryResult(message_id="bitable_skip", channel="feishu_bitable", status="success",
                              error_msg="Non-dict content, skipped")

    def sync_stock_pool(self, stock_pool_data: list[dict]) -> DeliveryResult:
        app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN")
        table_id = os.getenv("FEISHU_BITABLE_POOL_TABLE", "")
        if not app_token or not table_id:
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="Bitable not configured for stock pool")
        token = self._get_tenant_token()
        if not token:
            return DeliveryResult(message_id="", channel="feishu_bitable", status="failed",
                                  error_msg="Failed to get tenant token")
        success_count = 0
        for item in stock_pool_data[:50]:
            fields = {k: v for k, v in item.items() if isinstance(v, (str, int, float, bool))}
            if self._append_record(table_id, fields, token):
                success_count += 1
        return DeliveryResult(message_id=f"bitable_pool_{success_count}", channel="feishu_bitable",
                              status="success" if success_count > 0 else "failed",
                              error_msg=f"Synced {success_count}/{len(stock_pool_data)} records")

    def health(self) -> bool:
        app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN")
        app_id = os.getenv("FEISHU_APP_ID")
        return bool(app_token and app_id)
