"""qscard:// Apprise function-style plugin — the multi-channel unified foundation for QS card delivery (P0-2).

URL scheme:
    qscard://dm       Feishu DM card (FEISHU_APP_ID/FEISHU_APP_SECRET + FEISHU_DM_ID)
    qscard://group    Feishu group card (FEISHU_APP_ID/FEISHU_APP_SECRET + FEISHU_GROUP_ID)

notify_type -> card header color: info=blue / success=green / warning=orange / failure=red.

Self-contained sending core: loads .env on its own (Iron Rule #11). Direct cron invocation does not depend on the caller's environment.
Bypasses feishu_dm/feishu_hitl_dm (P0-2 boundary: does not alter existing channel behavior, zero import dependencies).

Memo on registration pitfalls (empirically verified via six-dimension verification on 2026-08-27 in /tmp venv):
- The APPRISE_PLUGIN_PATH environment variable is CLI-only (only effective in apprise/cli.py).
  The library API relies on importing this module — @notify self-registers to N_MGR at import time.
- If going through the N_MGR.module_detection path-scanning approach, the filename regex only allows pure alphanumerics and forbids underscores.
  This repo uses direct import, which does not go through that filter, hence snake_case naming (which also conforms to the repo's ruff N999).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

try:
    from apprise.decorators import notify
except ImportError:  # apprise not installed: the module can still be imported, direct function calls work, but the scheme is not registered
    notify = None

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT_S = 10

# notify_type -> Feishu card header template color
HEADER_COLOR = {
    "info": "blue",
    "success": "green",
    "warning": "orange",
    "failure": "red",
}

# target (URL host) -> environment variable name for chat_id
TARGET_CHAT_KEY = {
    "dm": "FEISHU_DM_ID",
    "group": "FEISHU_GROUP_ID",
}

_token_cache: dict[str, tuple[str, float]] = {}


def _load_env_once() -> None:
    """Load the .env at the repo root on our own (Iron Rule #11). Deferred until first send, so env isn't polluted at import time."""
    if getattr(_load_env_once, "_done", False):
        return
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parents[3] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass  # Either the caller already loaded it, or dotenv is absent; fall back on env
    _load_env_once._done = True


def _get_token(app_id: str, app_secret: str) -> str | None:
    """tenant_access_token, cached for 100 minutes (Feishu validity is 2 hours). Returns None on failure."""
    now = time.time()
    cached = _token_cache.get(app_id)
    if cached and cached[1] > now:
        return cached[0]
    try:
        resp = requests.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=_TIMEOUT_S,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if data.get("code") != 0:
        return None
    token = data.get("tenant_access_token")
    if token:
        _token_cache[app_id] = (token, now + 6000)
    return token


def build_card(title: str, body: str, notify_type: str = "info") -> dict:
    """Build a Feishu interactive card. notify_type determines the header color."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": HEADER_COLOR.get(notify_type, "blue"),
            "title": {"tag": "plain_text", "content": title or "QuantStar"},
        },
        "elements": [{"tag": "markdown", "content": body or ""}],
    }


def _push_card(chat_id: str, card: dict, token: str) -> bool:
    """Push the card via the Feishu Open API. Returns True only when code==0."""
    try:
        resp = requests.post(
            f"{FEISHU_API_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=_TIMEOUT_S,
        )
        return resp.json().get("code") == 0
    except (requests.RequestException, ValueError):
        return False


def send_qscard(target: str, title: str, body: str, notify_type: str = "info") -> bool:
    """Direct call entry point (does not depend on Apprise): send a card to target=dm/group.

    Returns False on any failure (unknown target / missing env / token failure / push failure). Never raises an exception.
    """
    _load_env_once()
    chat_key = TARGET_CHAT_KEY.get(target)
    if not chat_key:
        return False
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    chat_id = os.environ.get(chat_key, "")
    if not (app_id and app_secret and chat_id):
        return False
    token = _get_token(app_id, app_secret)
    if not token:
        return False
    return _push_card(chat_id, build_card(title, body, notify_type), token)


if notify is not None:
    @notify(on="qscard")
    def qscard_notify(body, title=None, notify_type=None, *args, **kwargs):
        """Apprise handler: the URL host is the target. meta is injected by Apprise."""
        meta = kwargs.get("meta") or {}
        return send_qscard(
            meta.get("host", ""),
            title or "",
            body or "",
            notify_type or "info",
        )
