"""创建仪表盘 4 张 Bitable 表（QuantMaster 身份，幂等）。

只动 Bitable，不碰 qs.db。已存在同名表则跳过并回显其 table_id。
用法：~/qs/.venv/bin/python scripts/create_dashboard_tables.py
"""
import os
import sys

import requests
from dotenv import load_dotenv

ENV_PATH = os.path.expanduser("~/qs/.env")
BASE = "https://open.feishu.cn/open-apis"

# 字段类型：Text=1 Number=2 SingleSelect=3（沿审批表三条类型教训：ID/日期全 Text）
TABLES = [
    {
        "name": "📊 管道运行矩阵",
        "fields": [
            {"field_name": "date", "type": 1},
            {"field_name": "job", "type": 1},
            {"field_name": "status", "type": 3, "property": {"options": [
                {"name": "success"}, {"name": "failed"}, {"name": "skipped"}]}},
            {"field_name": "duration_min", "type": 2},
            {"field_name": "error_brief", "type": 1},
            {"field_name": "version", "type": 1},
            {"field_name": "generated_at", "type": 1},
        ],
    },
    {
        "name": "📋 运维日报摘要",
        "fields": [
            {"field_name": "date", "type": 1},
            {"field_name": "total_runs", "type": 2},
            {"field_name": "success", "type": 2},
            {"field_name": "failed", "type": 2},
            {"field_name": "skipped", "type": 2},
            {"field_name": "failed_jobs", "type": 1},
            {"field_name": "freshness_red_count", "type": 2},
            {"field_name": "open_alerts", "type": 2},
            {"field_name": "overall_health", "type": 1},
            {"field_name": "e2e_status", "type": 1},
            {"field_name": "disk_free_gb", "type": 2},
            {"field_name": "generated_at", "type": 1},
        ],
    },
    {
        "name": "🔔 告警台账",
        "fields": [
            {"field_name": "title", "type": 1},
            {"field_name": "alert_id", "type": 1},
            {"field_name": "date", "type": 1},
            {"field_name": "severity", "type": 3, "property": {"options": [
                {"name": "info"}, {"name": "warning"}, {"name": "critical"}]}},
            {"field_name": "layer", "type": 1},
            {"field_name": "detail", "type": 1},
            {"field_name": "action", "type": 1},
            {"field_name": "status", "type": 3, "property": {"options": [
                {"name": "open"}, {"name": "resolved"}]}},
            {"field_name": "resolved_at", "type": 1},
            {"field_name": "created_at", "type": 1},
        ],
    },
    {
        "name": "📈 信号总览",
        "fields": [
            {"field_name": "date", "type": 1},
            {"field_name": "code", "type": 1},
            {"field_name": "name", "type": 1},
            {"field_name": "rank", "type": 2},
            {"field_name": "composite_score", "type": 2},
            {"field_name": "verdict", "type": 1},
            {"field_name": "buy_range", "type": 1},
            {"field_name": "stop_loss", "type": 1},
            {"field_name": "one_line_reason", "type": 1},
            {"field_name": "review_status", "type": 1},
            {"field_name": "generated_at", "type": 1},
        ],
    },
]


def main() -> int:
    load_dotenv(ENV_PATH)
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN")
    if not (app_id and app_secret and app_token):
        print("FAIL: FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_APP_TOKEN 缺配置")
        return 1

    resp = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
    token = resp.json().get("tenant_access_token")
    if not token:
        print(f"FAIL: tenant token 获取失败: {resp.text[:200]}")
        return 1
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 既有表清单（幂等跳过）
    existing: dict[str, str] = {}
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{BASE}/bitable/v1/apps/{app_token}/tables",
                         headers=headers, params=params, timeout=10)
        data = r.json().get("data", {})
        for item in data.get("items", []):
            existing[item["name"]] = item["table_id"]
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")

    print(f"base={app_token} 既有表: {existing}")
    results = {}
    for spec in TABLES:
        name = spec["name"]
        if name in existing:
            results[name] = existing[name]
            print(f"SKIP 已存在: {name} -> {existing[name]}")
            continue
        r = requests.post(f"{BASE}/bitable/v1/apps/{app_token}/tables",
                          headers=headers,
                          json={"table": {"name": name, "fields": spec["fields"]}},
                          timeout=15)
        body = r.json()
        if body.get("code") != 0:
            print(f"FAIL 建表 {name}: code={body.get('code')} msg={body.get('msg')}")
            return 1
        table_id = body["data"]["table_id"]
        results[name] = table_id
        print(f"CREATED: {name} -> {table_id}")

    print("\n=== .env 追加行（table_id 回填用）===")
    env_keys = {
        "📊 管道运行矩阵": "FEISHU_BITABLE_DASHBOARD_JOB_TABLE",
        "📋 运维日报摘要": "FEISHU_BITABLE_DASHBOARD_DIGEST_TABLE",
        "🔔 告警台账": "FEISHU_BITABLE_DASHBOARD_ALERT_TABLE",
        "📈 信号总览": "FEISHU_BITABLE_SIGNAL_TABLE",
    }
    for name, key in env_keys.items():
        print(f'{key}="{results[name]}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
