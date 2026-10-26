#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qs_dash_auth_proxy.py — dashboard 鉴权代理 (stdlib 只, 约 150 行)。

架构定位 (用户 09-27 裁): 应用层自锁, 弃用 Cloudflare Access。

拓扑:
  外网 → Cloudflare Tunnel (qs.example.com)
       → 127.0.0.1:8500 (本代理, 唯一入口)
       → 127.0.0.1:8501 (marimo --token, 只绑 loopback)

安全面:
  - marimo 依然开 --token 且只绑 127.0.0.1 (外网永远先撞本代理)
  - session token 内存 dict, 重启即全员重登 (用户裁可接受)
  - HttpOnly / SameSite=Strict / Secure / 24h, /logout 吊销
  - 所有非 /login 请求 (GET/POST) 都必须持有效会话
  - 只在对端为 127.0.0.1 时信任 CF-Connecting-IP; 另有全局失败上限
  - 账密文件: user:pass (>=12 位) 或 user:scrypt$salt$hash
    生成哈希: python3 -c "from qs_dash_auth_proxy import hash_password; print(hash_password('...'))"
  - 失败 3 次锁 15 分钟 (内存计数器, 无数据库)
  - 账密读 ~/.qs_dash_auth, 权限必须 400/600 否则拒服务
  - 不 print / 不 log 密码
"""
from __future__ import annotations

import os

import hashlib
import hmac
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

QS_ROOT = os.path.expanduser("~/qs")
AUTH_FILE = os.environ.get("QS_DASH_AUTH") or os.path.expanduser("~/.qs_dash_auth")
SALT = b"qs_dash_auth_v1"  # legacy only; new hashes use a random per-account salt
MIN_PASSWORD_LEN = int(os.environ.get("QS_DASH_MIN_PASSWORD_LEN", "12"))
GLOBAL_FAIL_LIMIT = 30          # 全局 15 分钟内失败上限 (防换 IP 暴破)
MAX_BODY = 1 << 20              # 1 MiB
COOKIE_SECURE = os.environ.get("QS_DASH_COOKIE_SECURE", "1") != "0"
TRUSTED_PEERS = {"127.0.0.1", "::1"}
SESSION_TTL_S = 86400
LOCK_AFTER = 3
LOCK_S = 900

SESSIONS: dict[str, float] = {}
FAILURES: dict[str, tuple[int, float]] = {}
GLOBAL_FAILS: list[float] = []
LOCK = threading.Lock()


class _AuthStore:
    """账密载体。支持格式: 'user:pass' 或 'user pass' 或 'user:HASH64'."""

    def __init__(self, path: str):
        self.path = path
        self.user = None
        self.hash_hex = None
        self.salt = SALT

    def load(self) -> bool:
        try:
            mode = os.stat(self.path).st_mode & 0o777
            if mode not in (0o400, 0o600):
                _warn(f"权限过宽 {oct(mode)} — 须 chmod 400/600")
                return False
        except OSError:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            return False
        if not raw:
            return False

        # 格式: 'user:pass' / 'user pass' / 'user:scrypt$<salt_hex>$<hash_hex>'
        # 安全加固: 不再接受无用户名的短单行格式, 明文密码须 >= MIN_PASSWORD_LEN。
        if ":" in raw:
            self.user, _, tail = raw.partition(":")
            tail = tail.strip()
        else:
            parts = raw.split(None, 1)
            if len(parts) != 2:
                _warn("账密文件须为 'user:pass' 或 'user:scrypt$salt$hash' 格式 (短单行已停用)")
                return False
            self.user, tail = parts[0], parts[1].strip()
        self.user = (self.user or "").strip()
        if not self.user or not tail:
            return False
        if tail.startswith("scrypt$"):
            try:
                _, salt_hex, hash_hex = tail.split("$", 2)
                self.salt = bytes.fromhex(salt_hex)
                bytes.fromhex(hash_hex)
            except ValueError:
                _warn("scrypt 哈希格式错误, 期望 scrypt$<salt_hex>$<hash_hex>")
                return False
            self.hash_hex = hash_hex.lower()
        else:
            if len(tail) < MIN_PASSWORD_LEN:
                _warn(f"密码过短 (< {MIN_PASSWORD_LEN} 位), 拒绝启动")
                return False
            self.salt = secrets.token_bytes(16)
            self.hash_hex = _scrypt(tail, self.salt)
        return True

    def verify(self, user: str, passwd: str) -> bool:
        if self.user is None or self.hash_hex is None:
            return False
        got = _scrypt(passwd, self.salt)
        ok_pass = hmac.compare_digest(got, self.hash_hex)
        ok_user = hmac.compare_digest(user.encode(), self.user.encode())
        return ok_pass and ok_user

    def is_short_format(self) -> bool:
        """短单行格式已停用, 恒为 False (保留接口兼容)。"""
        return False


def hash_password(password: str) -> str:
    """生成写入账密文件的哈希串: scrypt$<salt_hex>$<hash_hex>。"""
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt)}"


def _scrypt(s: str, salt: bytes = SALT) -> str:
    return hashlib.scrypt(s.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def _warn(msg: str) -> None:
    import sys
    print(f"[qs-dash-auth] {msg}", file=sys.stderr)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _issue_cookie() -> str:
    t = _new_token()
    now = time.time()
    with LOCK:
        for k in [k for k, exp in SESSIONS.items() if exp < now]:
            del SESSIONS[k]
        SESSIONS[t] = now + SESSION_TTL_S
    return t


def _revoke_ck(ck: str | None) -> None:
    if ck:
        with LOCK:
            SESSIONS.pop(ck, None)


def _validate_ck(ck: str | None) -> bool:
    if not ck:
        return False
    with LOCK:
        exp = SESSIONS.get(ck)
        if exp is None:
            return False
        if time.time() > exp:
            del SESSIONS[ck]
            return False
    return True


def _in_lock(ip: str) -> bool:
    now = time.time()
    with LOCK:
        GLOBAL_FAILS[:] = [t for t in GLOBAL_FAILS if now - t < LOCK_S]
        if len(GLOBAL_FAILS) >= GLOBAL_FAIL_LIMIT:
            return True
        n, until = FAILURES.get(ip, (0, 0))
        return now < until


def _record_fail(ip: str) -> None:
    with LOCK:
        GLOBAL_FAILS.append(time.time())
        n, _ = FAILURES.get(ip, (0, 0))
        n += 1
        if n >= LOCK_AFTER:
            FAILURES[ip] = (n, time.time() + LOCK_S)
        else:
            FAILURES[ip] = (n, 0)


class _Handler(BaseHTTPRequestHandler):
    # 简洁日志: 只记 IP 与结果, 不记 token/cookie/账密
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes | str, ctype: str = "text/html"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, cookie: str | None = None):
        self.send_response(302)
        self.send_header("Location", to)
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"qs_dash={cookie}; HttpOnly; SameSite=Strict; "
                f"Max-Age={SESSION_TTL_S}; Path=/" + ("; Secure" if COOKIE_SECURE else ""))
        self.end_headers()

    def _req_cookie(self) -> str | None:
        for part in self.headers.get_all("Cookie") or []:
            for kv in part.split(";"):
                k, _, v = kv.strip().partition("=")
                if k.strip() == "qs_dash":
                    return v.strip()
        return None

    def _ip(self) -> str:
        """客户端 IP: 只有对端是本机 (cloudflared) 时才信任转发头。"""
        peer = self.client_address[0]
        if peer in TRUSTED_PEERS:
            fwd = self.headers.get("CF-Connecting-IP") or (
                self.headers.get("X-Forwarded-For", "").split(",")[0])
            fwd = fwd.strip()
            if fwd:
                return fwd[:64]
        return peer

    def _body(self) -> bytes | None:
        """读取请求体; Content-Length 非法或超限返回 None。"""
        raw = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        ck = self._req_cookie()
        if _validate_ck(ck):
            self._proxy()
            return
        if self.path in ("/login", "/favicon.ico"):
            return self._login_page()
        return self._redirect("/login")

    def do_POST(self):
        if self.path == "/login":
            return self._do_login()
        ck = self._req_cookie()
        if self.path == "/logout":
            _revoke_ck(ck)
            return self._redirect("/login")
        # 其余 POST 必须持有效会话才转发 (此前未校验 → 未登录即可打到 marimo)
        if not _validate_ck(ck):
            return self._send(401, "401 Unauthorized")
        return self._proxy()

    def _login_page(self):
        self._send(200, LOGIN_HTML)

    def _do_login(self):
        ip = self._ip()
        if _in_lock(ip):
            self._send(423, "423 Locked - 连续失败, 请等 15 分钟后再试")
            return
        raw = self._body()
        if raw is None:
            return self._send(413, "413 Payload Too Large")
        try:
            params = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            user = params.get("u", [""])[0].strip()[:64]
            passwd = params.get("p", [""])[0].strip()[:128]
        except Exception:
            user = passwd = ""

        if not AUTH.verify(user, passwd):
            _record_fail(ip)
            self._redirect("/login?err=1")
            return
        # 成功: 清失败计数, 发 cookie
        with LOCK:
            FAILURES.pop(ip, None)
        ck = _issue_cookie()
        self._redirect("/", cookie=ck)

    def _proxy(self, _retried: bool = False):
        """反代到 marimo 127.0.0.1:8501 — 注入代持 cookie (双层登录修法)。"""
        try:
            url = "http://127.0.0.1:8501" + self.path
            # 尽量保真: 原 host 一并走 (marimo 有 --token 可能需要)
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("cookie", "x-forwarded-for", "cf-connecting-ip",
                                                "content-length", "host")}
            req = Request(url, method=self.command, headers=fwd_headers)
            # 双层登录修法: 用户浏览器的 qs_dash cookie 不转给 marimo,
            # 改注入 proxy 代持的 marimo session cookie。
            mc = _ensure_marimo_cookie()
            if mc:
                req.add_header("Cookie", mc)
            req.add_header("X-Forwarded-For", self._ip())
            body = self._body()
            if body is None:
                return self._send(413, "413 Payload Too Large")
            body = body or None
            try:
                resp = urlopen(req, data=body, timeout=60)
            except URLError as ue:
                # marimo cookie 过期 (401) → 强续重试一次
                if (not _retried and hasattr(ue, "code") and ue.code in (401, 403)):
                    _refresh_marimo_cookie()
                    return self._proxy(_retried=True)
                raise
            data = resp.read()
            # 303 回 login (token 认证页) 也判过期 → 强续重试一次
            if (not _retried and resp.status in (301, 302, 303)
                    and "access_token" in (resp.headers.get("Location") or "")):
                _refresh_marimo_cookie()
                return self._proxy(_retried=True)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() in ("transfer-encoding", "connection", "set-cookie"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except URLError as e:
            _warn(f"upstream error: {e.reason}")
            self._send(502, "502 Bad Gateway")
        except Exception as e:
            _warn(f"proxy error: {type(e).__name__}")
            self._send(502, "502 Bad Gateway")


AUTH = _AuthStore(AUTH_FILE)

# ------------------------------------------------------------------
# 双层登录修法 (09-29, 用户视角席方案): proxy 代持 marimo session cookie。
# token 只出现一次 (设备内 loopback 启动时换 cookie) 永不出设备不进 URL;
# 用户浏览器只持 auth proxy cookie (qs_dash)。
# ------------------------------------------------------------------
MARIMO_TOKEN_FILE = os.environ.get("QS_DASH_MARIMO_TOKEN") or os.path.expanduser(
    "~/.qs_dash_marimo_token")
MARIMO_BASE = "http://127.0.0.1:8501"
_marimo_cookie: str | None = None  # 代持的 marimo session cookie (session_8501=...)


def _fetch_marimo_cookie() -> str | None:
    """启动时/过期后续: GET /?access_token=<token> → 303 + Set-Cookie 取回。"""
    try:
        with open(MARIMO_TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    if not token:
        return None
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8501, timeout=10)
        conn.request("GET", f"/?access_token={token}")
        resp = conn.getresponse()
        resp.read()  # drain
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie":
                # session_8501=...; path=/; ... → 只取 cookie 名值对
                return v.split(";")[0].strip()
    except OSError:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return None


def _ensure_marimo_cookie() -> str | None:
    """代持 cookie 惰性初始化 + 过期自动续 (调用方判 401/303 后重调)。"""
    global _marimo_cookie
    if _marimo_cookie is None:
        _marimo_cookie = _fetch_marimo_cookie()
    return _marimo_cookie


def _refresh_marimo_cookie() -> str | None:
    """强制续期 (marimo cookie 过期 401/303 回 login 时调)。"""
    global _marimo_cookie
    _marimo_cookie = _fetch_marimo_cookie()
    return _marimo_cookie


AUTH = _AuthStore(AUTH_FILE)  # noqa: E305

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>QS Dashboard 登录</title>
<style>
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:380px;margin:80px auto;padding:0 16px}
 h1{font-size:1.2em} form label{display:block;margin:12px 0 4px}
 input{width:100%;padding:8px;box-sizing:border-box}
 button{width:100%;padding:10px;margin-top:16px;background:#0550ae;color:#fff;border:0;border-radius:4px}
 .err{color:#b00;margin-top:8px}
</style></head>
<body><h1>QuantStar Dashboard</h1>
<form method="post" action="/login">
 <label>账号</label><input name="u" autocomplete="username" required>
 <label>密码</label><input name="p" type="password" autocomplete="current-password" required>
 <button>登录</button>
</form>
<script>if(location.search.includes("err=1"))document.write('<p class="err">账号或密码错误。</p>')</script>
</body></html>"""

def main():
    if not AUTH.load():
        # 给具体岔口, 但不读不打印内容 (权限判例)
        if not os.path.isfile(AUTH_FILE):
            print(f"[qs-dash-auth] 文件不存在: {AUTH_FILE}")
        else:
            try:
                mode = os.stat(AUTH_FILE).st_mode & 0o777
                print(f"[qs-dash-auth] 权限过宽 {oct(mode)} — 须 chmod 400 或 600: {AUTH_FILE}")
            except OSError as e:
                print(f"[qs-dash-auth] 无法读出权限: {e}")
        raise SystemExit("auth file unreadable")
    port = int(os.environ.get("QS_DASH_AGENT_PORT", "8500"))
    host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.daemon_threads = True
    print(f"[qs-dash-auth] listening http://{host}:{port} → marimo 127.0.0.1:8501 "
          f"(session {SESSION_TTL_S//3600}h, lock {LOCK_AFTER}次/{LOCK_S//60}min)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
