"""QuantStar API 定时健康检查 — 服务不可用时自动飞书告警。

设计:
  - 每 5 分钟由 crontab 触发
  - 探测 http://127.0.0.1:8000/health (带 token)
  - 连续失败 >= ALERT_THRESHOLD 次才告警 (避免偶发抖动)
  - 恢复时推送恢复通知
  - 状态文件记录连续失败次数,避免重启丢失

用法:
  PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/api_health_check.py

告警逻辑:
  - 连续失败 3 次 → 飞书 DM 告警 (critical)
  - 恢复 → 飞书 DM 恢复通知 (info)
  - 同一告警 30 分钟内不重复 (AlertManager.should_alert 去重)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

# 刀4顺手件: load_dotenv 必须在模块级 env 读取之前 (cron 裸跑时 .env 未加载,
# 否则 QS_API_TOKEN 恒空 → API 请求无 Authorization 头)。
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

API_URL = os.environ.get("QS_API_URL", "http://127.0.0.1:8000")
API_TOKEN = os.environ.get("QS_API_TOKEN", "")
HEALTH_TIMEOUT = 10
ALERT_THRESHOLD = 3
STATE_FILE = os.path.join(PROJECT_ROOT, "logs", "api_health_state.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "api_health_check.jsonl")


def _log(event: str, **fields):
    entry = {"timestamp": datetime.now().astimezone().isoformat(), "event": event, **fields}
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False))


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_alert_time": None, "was_down": False}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _check_api() -> tuple[bool, str, dict | None]:
    """探测 API /health 端点。返回 (ok, detail, health_json)。"""
    headers = {}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    try:
        resp = requests.get(f"{API_URL}/health", headers=headers, timeout=HEALTH_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            return True, f"HTTP 200, status={data.get('status', '?')}", data
        return False, f"HTTP {resp.status_code}", None
    except requests.exceptions.ConnectionError:
        return False, "连接被拒 (服务未运行或崩溃)", None
    except requests.exceptions.Timeout:
        return False, f"超时 ({HEALTH_TIMEOUT}s 无响应)", None
    except Exception as e:
        return False, f"异常: {e}", None


def _push_feishu(title: str, body: str, severity: str = "critical"):
    """飞书 App API 私聊推送告警通知 (轻量, 直接 HTTP 调用)。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        dm_id = os.environ.get("FEISHU_DM_ID", "")
        if not all([app_id, app_secret, dm_id]):
            print("⚠️ 飞书凭据未配置 (FEISHU_APP_ID/SECRET/DM_ID), 跳过通知")
            return

        token_resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10,
        )
        token = token_resp.json().get("tenant_access_token", "")
        if not token:
            print(f"⚠️ 获取飞书 token失败: {token_resp.json()}")
            return

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

        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": dm_id, "msg_type": "interactive", "content": json.dumps(card)},
            timeout=10,
        )
        result = resp.json()
        if result.get("code", -1) == 0:
            print(f"飞书推送成功: {title}")
        else:
            print(f"飞书推送失败: {result}")
    except Exception as exc:
        print(f"⚠️ 飞书通知推送失败: {exc}")


def run() -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    global API_TOKEN
    API_TOKEN = os.environ.get("QS_API_TOKEN", "")

    state = _load_state()
    ok, detail, health_data = _check_api()

    if ok:
        prev_failures = state["consecutive_failures"]
        was_down = state.get("was_down", False)
        state["consecutive_failures"] = 0
        state["was_down"] = False
        _save_state(state)

        _log("api_healthy", detail=detail, prev_failures=prev_failures,
             layers=health_data.get("layers", []) if health_data else [])

        if was_down and prev_failures >= ALERT_THRESHOLD:
            body = f"API 已恢复\n地址: {API_URL}\n此前连续失败 {prev_failures} 次\n当前: {detail}"
            _push_feishu("API 恢复通知", body, severity="info")
        return 0

    else:
        state["consecutive_failures"] += 1
        failures = state["consecutive_failures"]
        state["was_down"] = True
        _save_state(state)

        _log("api_unhealthy", detail=detail, consecutive_failures=failures)

        if failures >= ALERT_THRESHOLD:
            body = (f"API 连续 {failures} 次健康检查失败\n"
                    f"地址: {API_URL}\n"
                    f"错误: {detail}\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"排查建议:\n"
                    f"  systemctl status quantstar-api\n"
                    f"  sudo journalctl -u quantstar-api -n 50\n"
                    f"  sudo systemctl restart quantstar-api")
            _push_feishu("API 服务不可用", body, severity="critical")
        else:
            print(f"失败 {failures}/{ALERT_THRESHOLD} 次, 未达告警阈值")
        return 1


if __name__ == "__main__":
    # DL-13 噪音分流: 不再经 run_guarded 写 pipeline_runs (每 5 分钟一条, 高频噪音)。
    # 可观测性由本脚本 logs/api_health_check.jsonl 独立日志 + 飞书告警承担;
    # 崩溃时落 jsonl + 飞书, 不依赖 pipeline_runs。
    try:
        sys.exit(run())
    except Exception:
        import traceback

        tb = traceback.format_exc()
        _log("api_health_check_crash", traceback=tb)
        _push_feishu("API 健康检查脚本崩溃", tb, severity="critical")
        sys.exit(1)