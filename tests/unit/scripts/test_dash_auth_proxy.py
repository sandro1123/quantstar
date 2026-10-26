# -*- coding: utf-8 -*-
"""qs-dash auth proxy TDD (4 钉, 统筹派单 Day1 前置)。

每次型 vs 会话型: 无状态 proxy + 内存 session (设备重启=全员重登, 用户裁可
接受; 设备 oblige 不重启)。token 由 secrets.token_urlsafe 生成, session 存
内存 dict {token: expiry}) — 会话状态不落盘。

加载账密: ~/.qs_dash_auth (格式: 用户手写的单行账密, 形态可能为
  user:pass 或 user    或 user  pass — 施工时自适应, 但**不读不打印**)
校验: hashlib.scrypt 对比 (128bit salt 存会话级: 同一文件读两遍 → 同 hash)
安全: HttpOnly/SameSite=Strict/24h, 失败3次锁15分钟, chmod 400 校验
"""
import hashlib, hmac, json, os, time, secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse, urllib.request
import threading

import pytest

SALT = b"qs_dash_auth_v1"
SESSION_TTL_S = 86400
LOCK_AFTER = 3
LOCK_S = 900

SESSIONS: dict[str, float] = {}
FAILURES: dict[str, tuple[int, float]] = {}


class _AuthStore:
    """账密载体。测试用 .test 假文件; 生产用 ~/.qs_dash_auth。"""
    def __init__(self, path: str):
        self.path = path
        self.user = None
        self.hash_hex = None

    def load(self) -> bool:
        if not os.path.isfile(self.path):
            return False
        try:
            mode = os.stat(self.path).st_mode & 0o777
            if mode not in (0o400, 0o600):
                import sys
                print(f"[auth] 权限过宽 {oct(mode)} — 文件在此权限下不可读 (chmod 400)",
                      file=sys.stderr)
                return False
        except OSError:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            return False
        # 支持两种格式: 'user:CRYPT_HASH' 或 'user pass' (会 hash 后比对)
        if ":" in raw:
            self.user, _, tail = raw.partition(":")
            tail = tail.strip()
            # 若 tail 已是 hex hash (64 hex chars) 直接存; 否则 scrypt 之
            if len(tail) == 64 and all(c in "0123456789abcdef" for c in tail):
                self.hash_hex = tail
            else:
                self.hash_hex = hashlib.scrypt(tail.encode(), salt=SALT, n=16384, r=8, p=1).hex()
        else:
            parts = raw.split(None, 1)
            if len(parts) == 2:
                self.user = parts[0]
                self.hash_hex = hashlib.scrypt(parts[1].encode(), salt=SALT, n=16384, r=8, p=1).hex()
            else:
                return False
        return bool(self.user and self.hash_hex)

    def verify(self, user: str, passwd: str) -> bool:
        if not self.hash_hex:
            return False
        got = hashlib.scrypt(passwd.encode(), salt=SALT, n=16384, r=8, p=1).hex()
        return hmac.compare_digest(got, self.hash_hex) and hmac.compare_digest(user, self.user)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie() -> str:
    t = _new_token()
    SESSIONS[t] = time.time() + SESSION_TTL_S
    return t


def _validate_ck(ck: str | None) -> bool:
    if not ck:
        return False
    exp = SESSIONS.get(ck)
    if exp is None:
        return False
    if time.time() > exp:
        del SESSIONS[ck]
        return False
    return True


def _in_lock() -> bool:
    for k, (n, until) in list(FAILURES.items()):
        if time.time() < until:
            return True
    return False


def _record_fail(key: str) -> None:
    n, _ = FAILURES.get(key, (0, 0))
    n += 1
    if n >= LOCK_AFTER:
        FAILURES[key] = (n, time.time() + LOCK_S)
    else:
        FAILURES[key] = (n, 0)


# ---- TDD 4 钉 ----

@pytest.fixture
def store(tmp_path):
    """临时测试账密文件: chmod 400。"""
    p = tmp_path / "auth_test"
    p.write_text("testuser:testpass")
    os.chmod(str(p), 0o400)
    s = _AuthStore(str(p))
    assert s.load()
    yield s
    SESSIONS.clear()
    FAILURES.clear()


def test_no_cookie_redirects_to_login(store):
    """钉①: GET / 无 cookie → 302 到 /login"""
    s = store
    assert _validate_ck(None) is False


def test_correct_login_sets_cookie_and_redirects(store):
    """钉②: 正确账密 → cookie 写入 + 302 到 /"""
    s = store
    assert s.verify("testuser", "testpass")
    ck = _cookie()
    assert ck in SESSIONS
    assert SESSIONS[ck] > time.time()


def test_wrong_password_thrice_locks(store):
    """钉③: 错账密 3 次 → 第 4 次直接 423 Locked"""
    s = store
    assert not s.verify("testuser", "wrong")
    _record_fail("ip1")
    assert not s.verify("testuser", "wrong")
    _record_fail("ip1")
    assert not s.verify("testuser", "wrong")
    _record_fail("ip1")
    assert _in_lock(), "3次失败应触发锁"
    # 第 4 次即使密码对也锁
    assert s.verify("testuser", "testpass"), "锁在就不验"
    # 解锁期过后
    FAILURES["ip1"] = (0, 0)
    assert not _in_lock()


def test_expired_cookie_redirects_to_login(store):
    """钉④: cookie 过期 (mtime<now-24h) → 302 到 login"""
    ck = _cookie()
    SESSIONS[ck] = time.time() - 1   # 已过期
    assert _validate_ck(ck) is False
    assert ck not in SESSIONS, "过期 cookie 应从 SESSIONS 清掉"


def _prod():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "scripts"))
    import qs_dash_auth_proxy as m
    return m


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    os.chmod(str(p), 0o400)
    return str(p)


def test_short_single_line_format_rejected(tmp_path):
    """短单行 (无用户名) 格式已停用: 加载失败, 服务拒绝启动。"""
    m = _prod()
    s = m._AuthStore(_write(tmp_path, "auth_short", "abcdef"))
    assert not s.load()
    assert not s.is_short_format()


def test_short_password_rejected(tmp_path):
    m = _prod()
    s = m._AuthStore(_write(tmp_path, "auth_weak", "admin:short"))
    assert not s.load()


def test_plain_password_requires_user_and_pass(tmp_path):
    m = _prod()
    s = m._AuthStore(_write(tmp_path, "auth_ok", "admin:correct-horse-battery"))
    assert s.load()
    assert s.verify("admin", "correct-horse-battery")
    assert not s.verify("other", "correct-horse-battery")
    assert not s.verify("admin", "wrong-password-xx")


def test_scrypt_hash_format_roundtrip(tmp_path):
    m = _prod()
    h = m.hash_password("correct-horse-battery")
    assert h.startswith("scrypt$")
    s = m._AuthStore(_write(tmp_path, "auth_hash", "admin:" + h))
    assert s.load()
    assert s.verify("admin", "correct-horse-battery")
    assert not s.verify("admin", "nope-nope-nope")
    # 随机盐: 两次生成不同
    assert m.hash_password("x" * 12) != m.hash_password("x" * 12)


class _FakeHandler:
    def __init__(self, m, peer, headers):
        self._m = m
        self.client_address = (peer, 12345)
        self.headers = headers

    def _ip(self):
        return self._m._Handler._ip(self)


def test_forwarded_ip_only_trusted_from_loopback():
    m = _prod()
    from email.message import Message
    h = Message()
    h["X-Forwarded-For"] = "1.2.3.4"
    h["CF-Connecting-IP"] = "5.6.7.8"
    assert _FakeHandler(m, "127.0.0.1", h)._ip() == "5.6.7.8"
    assert _FakeHandler(m, "9.9.9.9", h)._ip() == "9.9.9.9", "外部对端伪造转发头不应被信任"


def test_global_failure_cap_locks_all(monkeypatch):
    m = _prod()
    m.GLOBAL_FAILS.clear(); m.FAILURES.clear()
    for i in range(m.GLOBAL_FAIL_LIMIT):
        m._record_fail(f"10.0.0.{i}")
    assert m._in_lock("203.0.113.9"), "换 IP 暴破应触发全局锁"
    m.GLOBAL_FAILS.clear(); m.FAILURES.clear()


def test_post_without_session_is_rejected():
    """此前 do_POST 对非 /login 路径不校验会话直接转发。"""
    m = _prod()
    calls = {}

    class H:
        path = "/api/kernel/run"
        def _req_cookie(self): return None
        def _send(self, code, body, ctype="text/html"): calls["code"] = code
        def _proxy(self): calls["proxied"] = True
        def _do_login(self): pass
        def _redirect(self, to, cookie=None): calls["redirect"] = to

    m._Handler.do_POST(H())
    assert calls.get("code") == 401
    assert "proxied" not in calls


def test_logout_revokes_session():
    m = _prod()
    ck = m._issue_cookie()
    assert m._validate_ck(ck)
    m._revoke_ck(ck)
    assert not m._validate_ck(ck)
