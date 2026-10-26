# -*- coding: utf-8 -*-
"""双层登录修法 TDD (6 钉, 09-29 04:00 窗, 用户视角席方案)。"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))


def _mod():
    import qs_dash_auth_proxy as m
    return m


def _handler(path="/", cookie=None):
    h = MagicMock()
    h.path = path
    h.command = "GET"
    h.client_address = ("127.0.0.1", 0)
    h.rfile.read.return_value = b""
    hdrs = MagicMock()
    hdrs.get = lambda k, d=None: (f"qs_dash={cookie}" if (k == "Cookie" and cookie) else d)
    hdrs.get_all = lambda k: ([f"qs_dash={cookie}"] if (k == "Cookie" and cookie) else [])
    h.headers = hdrs
    return h


def test_proxy_injects_marimo_cookie():
    m = _mod()
    m._marimo_cookie = "session_8501=abc123"
    with patch("qs_dash_auth_proxy.urlopen") as mo_url:
        mo_url.return_value = MagicMock(status=200, headers={"Content-Type": "text/html"},
                                        read=lambda: b"ok")
        m._Handler._proxy(_handler())
        req = mo_url.call_args[0][0]
        assert req.get_header("Cookie") == "session_8501=abc123"
        assert "access_token" not in req.full_url


def test_user_browser_only_auth_cookie():
    m = _mod()
    ck = m._issue_cookie()
    assert "session_8501" not in ck
    assert len(ck) > 30


def test_fetch_marimo_cookie_once():
    m = _mod()
    with patch("http.client.HTTPConnection") as mc:
        resp = MagicMock()
        resp.getheaders.return_value = [("set-cookie", "session_8501=xyz; path=/")]
        resp.read.return_value = b""
        mc.return_value.getresponse.return_value = resp
        with patch("builtins.open", create=True) as mo_open:
            mo_open.return_value.__enter__.return_value.read.return_value = "tok123"
            m._marimo_cookie = None
            got = m._fetch_marimo_cookie()
            assert got == "session_8501=xyz"
            req_path = mc.return_value.request.call_args[0][1]
            assert "access_token=tok123" in req_path


def test_expired_cookie_auto_refresh():
    m = _mod()
    m._marimo_cookie = "session_8501=old"
    calls = {"n": 0}

    def fake_urlopen(req, data=None, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            from urllib.error import HTTPError
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        return MagicMock(status=200, headers={"Content-Type": "text/html"},
                         read=lambda: b"ok")

    handler = _handler()
    handler._proxy = lambda _retried=False: m._Handler._proxy(handler, _retried)
    with patch("qs_dash_auth_proxy.urlopen", side_effect=fake_urlopen),          patch.object(m, "_refresh_marimo_cookie", return_value="session_8501=new") as mo_r:
        m._Handler._proxy(handler)
        assert mo_r.called, "401 须强续"
        assert calls["n"] == 2, "重试一次走 urlopen 第二次"


def test_unauthenticated_still_302():
    m = _mod()
    handler = _handler(cookie=None)
    m._Handler.do_GET(handler)
    handler._redirect.assert_called_with("/login")


def test_no_token_in_logs(caplog):
    m = _mod()
    import logging
    with caplog.at_level(logging.INFO):
        with patch("http.client.HTTPConnection") as mc:
            resp = MagicMock()
            resp.getheaders.return_value = [("set-cookie", "session_8501=xyz")]
            mc.return_value.getresponse.return_value = resp
            with patch("builtins.open", create=True) as mo_open:
                mo_open.return_value.__enter__.return_value.read.return_value = "tok123"
                m._marimo_cookie = None
                m._fetch_marimo_cookie()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "tok123" not in logged and "access_token" not in logged
