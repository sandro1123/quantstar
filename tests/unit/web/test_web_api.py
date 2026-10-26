"""Web API 单元测试 — 鉴权/输入校验/路由"""
import pytest


@pytest.fixture
def client(monkeypatch):
    from web.api import app
    monkeypatch.delenv("QS_API_TOKEN", raising=False)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestAuth:
    def test_no_token_local_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code in (200, 500)

    def test_no_token_remote_forbidden(self, client, monkeypatch):
        monkeypatch.setattr(client, "environ_base", {**client.environ_base, "REMOTE_ADDR": "203.0.113.5"})
        resp = client.get("/health")
        assert resp.status_code == 403

    def test_with_token_required(self, monkeypatch):
        monkeypatch.setenv("QS_API_TOKEN", "a-long-test-token-0123456789")
        from web.api import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.get("/health")
            assert resp.status_code == 401
            resp = c.get("/health", headers={"Authorization": "Bearer a-long-test-token-0123456789"})
            assert resp.status_code in (200, 500)
            resp = c.get("/health", headers={"Authorization": "Bearer wrong"})
            assert resp.status_code == 401


    def test_short_token_refused(self, monkeypatch):
        monkeypatch.setenv("QS_API_TOKEN", "a")
        from web.api import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.get("/health", headers={"Authorization": "Bearer a"})
            assert resp.status_code == 503


class TestValidation:
    def test_invalid_date(self, client):
        resp = client.get("/api/snapshot/2026-13-99")
        assert resp.status_code == 400

    def test_valid_date_shape(self, client):
        resp = client.get("/api/snapshot/2026-08-14")
        assert resp.status_code in (200, 500)


class TestRoutes:
    def test_health_route(self, client):
        resp = client.get("/health")
        assert resp.status_code in (200, 500)

    def test_metrics_route(self, client):
        resp = client.get("/metrics")
        assert resp.status_code in (200, 500)

    def test_cors_denies_untrusted_origin_by_default(self, client):
        resp = client.get("/api/snapshot/not-a-date", headers={"Origin": "https://attacker.example"})
        assert resp.status_code == 400
        assert resp.headers.get("Access-Control-Allow-Origin") is None

    def test_cors_allows_only_configured_exact_origin(self, client, monkeypatch):
        monkeypatch.setenv("QS_CORS_ORIGINS", "http://localhost:3000, https://dashboard.example")
        allowed = client.get("/api/snapshot/not-a-date", headers={"Origin": "http://localhost:3000"})
        assert allowed.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
        assert "Origin" in allowed.headers.get("Vary", "")
        denied = client.get("/api/snapshot/not-a-date", headers={"Origin": "https://attacker.example"})
        assert denied.headers.get("Access-Control-Allow-Origin") is None


class TestProxyHardening:
    @pytest.mark.parametrize("hdr", ["X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP", "Forwarded"])
    def test_no_token_proxied_request_forbidden(self, client, hdr):
        """未设 token 时, 经反向代理 (remote_addr=127.0.0.1) 转发进来的请求必须拒绝。"""
        resp = client.get("/health", headers={hdr: "198.51.100.7"})
        assert resp.status_code == 403

    def test_errors_do_not_leak_exception_text(self, client, monkeypatch):
        import web.api as api

        def boom():
            raise RuntimeError("/home/someone/secret/path.db")
        monkeypatch.setattr(api, "_get_app", boom)
        resp = client.get("/health")
        assert resp.status_code == 500
        assert b"/home/someone" not in resp.data
