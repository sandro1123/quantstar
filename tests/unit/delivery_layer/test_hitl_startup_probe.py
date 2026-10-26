"""HITL Phase 2 ③: 启动真写探测 (ledger_selfcheck)

教训#4: 能读证明不了能写 — 91403 事故里新 app 只有查看权限,
token 照拿、读探测全绿, 但台账写入被拒, 静默断链 13 行。
写探测必须"真写一行再立即删除", 权限缺时红牌退出, 不带病上岗。
"""
import pytest

import delivery_layer.hitl_ledger as hitl_ledger


def _env(monkeypatch, env):
    for k in ("FEISHU_BITABLE_LEDGER_APP_TOKEN", "FEISHU_BITABLE_APP_TOKEN",
              "FEISHU_BITABLE_APPROVAL_TABLE",
              "FEISHU_HITL_APP_ID", "FEISHU_HITL_APP_SECRET",
              "FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestLedgerSelfcheck:
    def test_unconfigured_returns_not_ok(self, monkeypatch):
        """未配置台账 = 红牌而非跳过: 把断链从'权限死'变'配置死'同样不可见"""
        _env(monkeypatch, {})
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is False
        assert "unconfigured" in result["detail"]

    def test_token_failure_returns_not_ok(self, monkeypatch):
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s"})
        monkeypatch.setattr(hitl_ledger, "_get_cached_token",
                            lambda a, s: "")
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is False
        assert "tenant_access_token" in result["detail"]

    def test_403_reject_names_collaborator_fix(self, monkeypatch):
        """403/permission 拒绝必须点名'加为台账 base 协作者 (可编辑)'修法"""
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s"})
        monkeypatch.setattr(hitl_ledger, "_get_cached_token",
                            lambda a, s: "tok")
        monkeypatch.setattr(hitl_ledger.requests, "post",
                            lambda *a, **kw: _Resp(
                                {"code": 403, "msg": "permission denied"}))
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is False
        assert "probe POST rejected: code=403" in result["detail"]
        assert "协作者" in result["detail"]

    def test_probe_write_then_delete_ok(self, monkeypatch):
        """真写一行 → 立即删除 → 全通才 ok=True (写探测的完整语义)"""
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s"})
        monkeypatch.setattr(hitl_ledger, "_get_cached_token",
                            lambda a, s: "tok")
        posts, deletes = [], []

        def fake_post(url, headers=None, json=None, timeout=None):
            posts.append((url, json))
            assert json["fields"]["code"] == "PROBE"
            assert json["fields"]["review_id"] == "0"
            return _Resp({"code": 0,
                          "data": {"record": {"record_id": "rec999"}}})

        def fake_delete(url, headers=None, timeout=None):
            deletes.append(url)
            return _Resp({"code": 0})

        monkeypatch.setattr(hitl_ledger.requests, "post", fake_post)
        monkeypatch.setattr(hitl_ledger.requests, "delete", fake_delete)
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is True
        assert "rec999" in result["detail"]
        assert len(posts) == 1 and len(deletes) == 1
        assert deletes[0].endswith("/records/rec999")

    def test_delete_failure_leaves_manual_cleanup_note(self, monkeypatch):
        """探针行删除失败必须留下行号+人工清理指引, 不静默"""
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s"})
        monkeypatch.setattr(hitl_ledger, "_get_cached_token",
                            lambda a, s: "tok")
        monkeypatch.setattr(hitl_ledger.requests, "post",
                            lambda *a, **kw: _Resp(
                                {"code": 0,
                                 "data": {"record": {"record_id": "rec777"}}}))
        monkeypatch.setattr(hitl_ledger.requests, "delete",
                            lambda *a, **kw: _Resp({"code": 1254047, "msg": "x"}))
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is False
        assert "rec777" in result["detail"]
        assert "manual cleanup" in result["detail"]

    def test_post_ok_but_record_id_missing(self, monkeypatch):
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s"})
        monkeypatch.setattr(hitl_ledger, "_get_cached_token",
                            lambda a, s: "tok")
        monkeypatch.setattr(hitl_ledger.requests, "post",
                            lambda *a, **kw: _Resp({"code": 0, "data": {}}))
        result = hitl_ledger.ledger_selfcheck()
        assert result["ok"] is False
        assert "record_id missing" in result["detail"]

    def test_probe_uses_hitl_creds_preferring_new_app(self, monkeypatch):
        """探测凭据与正式写入同语义: FEISHU_HITL_* 优先, 回退共用键"""
        _env(monkeypatch, {
            "FEISHU_BITABLE_LEDGER_APP_TOKEN": "btok",
            "FEISHU_BITABLE_APPROVAL_TABLE": "tbl123",
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s_new",
            "FEISHU_APP_ID": "cli_old", "FEISHU_APP_SECRET": "s_old"})
        seen = {}

        def fake_token(app_id, secret):
            seen["app_id"] = app_id
            return "tok"

        monkeypatch.setattr(hitl_ledger, "_get_cached_token", fake_token)
        monkeypatch.setattr(hitl_ledger.requests, "post",
                            lambda *a, **kw: _Resp(
                                {"code": 0,
                                 "data": {"record": {"record_id": "r1"}}}))
        monkeypatch.setattr(hitl_ledger.requests, "delete",
                            lambda *a, **kw: _Resp({"code": 0}))
        hitl_ledger.ledger_selfcheck()
        assert seen["app_id"] == "cli_new"


class TestDaemonStartupGate:
    """main() 把探测结果写心跳并在失败时退出 — 红牌不静默 (铁律#12)"""

    def test_daemon_imports_ledger_selfcheck(self):
        import importlib
        from delivery_layer import hitl_ws_daemon
        assert hitl_ws_daemon.ledger_selfcheck is hitl_ledger.ledger_selfcheck

    def test_main_returns_1_on_probe_failure(self, monkeypatch):
        import importlib
        from delivery_layer import hitl_ws_daemon as daemon
        monkeypatch.setattr(daemon, "FEISHU_APP_ID", "cli_x", raising=False)
        monkeypatch.setattr(daemon, "FEISHU_APP_SECRET", "s", raising=False)
        monkeypatch.setattr(daemon, "ledger_selfcheck",
                            lambda: {"ok": False, "detail": "code=403"})
        hb = []
        monkeypatch.setattr(daemon, "write_heartbeat",
                            lambda event, **kw: hb.append((event, kw)))
        monkeypatch.setattr(daemon, "_start_heartbeat_thread", lambda: None)
        assert daemon.main() == 1
        assert hb[0][0] == "fatal" or hb[0][0] == "start"
        assert any(e == "startup_probe" and kw.get("ok") is False
                   for e, kw in hb)

    def test_main_proceeds_on_probe_pass(self, monkeypatch):
        import importlib
        from delivery_layer import hitl_ws_daemon as daemon
        monkeypatch.setattr(daemon, "FEISHU_APP_ID", "cli_x", raising=False)
        monkeypatch.setattr(daemon, "FEISHU_APP_SECRET", "s", raising=False)
        monkeypatch.setattr(daemon, "ledger_selfcheck",
                            lambda: {"ok": True, "detail": "probe ok"})
        hb = []
        monkeypatch.setattr(daemon, "write_heartbeat",
                            lambda event, **kw: hb.append((event, kw)))
        monkeypatch.setattr(daemon, "_start_heartbeat_thread", lambda: None)

        class _Boom(RuntimeError):
            pass

        def fake_build():
            raise _Boom("reached ws loop gate")

        monkeypatch.setattr(daemon, "build_handler", fake_build)
        with pytest.raises(_Boom):
            daemon.main()
        # 探测通过 → 心跳记 ok=True, 且走到了 handler 构建 (即未被探测拦下)
        assert any(e == "startup_probe" and kw.get("ok") is True
                   for e, kw in hb)
