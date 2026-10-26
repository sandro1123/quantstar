"""F5.2 Delivery 加固测试 — group App API + health 真探测 + delivery_log 落表"""
import pytest

from common.database import Database
from delivery_layer import DeliveryLayer
from delivery_layer.channels import feishu_dm, feishu_group
from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.channels.feishu_group import FeishuGroupChannel
from delivery_layer.channels.feishu_dm import FeishuDMChannel
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.log_store import DeliveryLogStore
from delivery_layer.message import Message
from delivery_layer.router import Router


class FakeResp:
    def __init__(self, body=None):
        self._body = body if body is not None else {"code": 0}
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_GROUP_ID",
              "FEISHU_GROUP_WEBHOOK", "FEISHU_WEBHOOK_URL", "FEISHU_DM_ID",
              "FEISHU_DM_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)


def _msg(force_channel=None, content_type="test_delivery"):
    return Message(content={"msg_type": "text", "content": {"text": "hi"}},
                   content_type=content_type, title="t",
                   force_channel=force_channel)


class TestGroupAppAPI:
    """F5.2a: feishu_group App API 模式 (复用 dm token+push_via_api)"""

    def test_api_mode_success(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_GROUP_ID", "oc_group")
        monkeypatch.setattr(feishu_group, "_get_cached_token", lambda a, s: "tok")
        calls = []
        monkeypatch.setattr(feishu_group, "_push_via_api",
                            lambda cid, content, token: calls.append((cid, token)) or (True, ""))
        result = FeishuGroupChannel().send(_msg())
        assert result.status == "success"
        assert calls == [("oc_group", "tok")]

    def test_api_fail_falls_back_to_webhook(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_GROUP_ID", "oc_group")
        monkeypatch.setenv("FEISHU_GROUP_WEBHOOK", "https://hook")
        monkeypatch.setattr(feishu_group, "_get_cached_token", lambda a, s: "tok")
        monkeypatch.setattr(feishu_group, "_push_via_api", lambda *a: (False, "api down"))
        monkeypatch.setattr(feishu_group.requests, "post", lambda *a, **kw: FakeResp())
        result = FeishuGroupChannel().send(_msg())
        assert result.status == "success"

    def test_no_creds_fails(self):
        result = FeishuGroupChannel().send(_msg())
        assert result.status == "failed"
        assert "无可用凭据" in result.error_msg

    def test_webhook_only_still_works(self, monkeypatch):
        monkeypatch.setenv("FEISHU_GROUP_WEBHOOK", "https://hook")
        monkeypatch.setattr(feishu_group.requests, "post", lambda *a, **kw: FakeResp())
        result = FeishuGroupChannel().send(_msg())
        assert result.status == "success"


class TestHealthRealProbe:
    """F5.2b: health() 真探测, 不再只判 env 存在"""

    def test_group_health_probes_token_and_chat(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_GROUP_ID", "oc_group")
        monkeypatch.setattr(feishu_group, "_get_cached_token", lambda a, s: "tok")
        seen = []

        def fake_get(url, headers=None, timeout=None):
            seen.append(url)
            return FakeResp({"code": 0})
        monkeypatch.setattr(feishu_group.requests, "get", fake_get)
        assert FeishuGroupChannel().health() is True
        assert "oc_group" in seen[0]

    def test_group_health_token_fail_unhealthy(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_GROUP_ID", "oc_group")
        monkeypatch.setattr(feishu_group, "_get_cached_token", lambda a, s: "")
        assert FeishuGroupChannel().health() is False

    def test_group_health_chat_unreachable_unhealthy(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_GROUP_ID", "oc_group")
        monkeypatch.setattr(feishu_group, "_get_cached_token", lambda a, s: "tok")
        monkeypatch.setattr(feishu_group.requests, "get",
                            lambda *a, **kw: FakeResp({"code": 230002, "msg": "chat not found"}))
        assert FeishuGroupChannel().health() is False

    def test_dm_health_token_ok(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_DM_ID", "oc_dm")
        monkeypatch.setattr(feishu_dm, "_get_cached_token", lambda a, s: "tok")
        assert FeishuDMChannel().health() is True

    def test_dm_health_token_fail_webhook_fallback(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
        monkeypatch.setenv("FEISHU_DM_ID", "oc_dm")
        monkeypatch.setenv("FEISHU_DM_WEBHOOK", "https://hook")
        monkeypatch.setattr(feishu_dm, "_get_cached_token", lambda a, s: "")
        assert FeishuDMChannel().health() is True

    def test_dm_health_no_creds_unhealthy(self):
        assert FeishuDMChannel().health() is False


class TestDeliveryLog:
    """F5.2c: delivery_log 持久化 + DeliveryExecutor 写入"""

    def test_log_store_record_and_recent(self):
        db = Database(":memory:")
        store = DeliveryLogStore(db)
        store.record(DeliveryResult(message_id="m1", channel="feishu_dm",
                                    status="success", duration_ms=42),
                     content_type="morning_brief")
        store.record(DeliveryResult(message_id="m2", channel="feishu_group",
                                    status="failed", error_msg="boom"),
                     content_type="evening_report")
        rows = store.recent()
        assert len(rows) == 2
        assert rows[0]["message_id"] == "m2"  # 最新在前
        assert rows[0]["error"] == "boom"
        assert rows[1]["content_type"] == "morning_brief"

    def test_executor_writes_log_on_success(self):
        class OKChannel(Channel):
            name = "ok_ch"

            def send(self, message):
                return DeliveryResult(message_id=message.message_id,
                                      channel=self.name, status="success", duration_ms=5)

            def health(self):
                return True

        db = Database(":memory:")
        reg = ChannelRegistry()
        reg.register(OKChannel())
        ex = DeliveryExecutor(reg, Router(), db=db)
        results = ex.send(_msg(force_channel="ok_ch", content_type="t_ok"),
                          report_mode="debug")
        assert results and results[0].status == "success"
        rows = db.get_connection().execute(
            "SELECT channel, status, content_type FROM delivery_log").fetchall()
        assert len(rows) == 1
        assert (rows[0]["channel"], rows[0]["status"], rows[0]["content_type"]) == \
            ("ok_ch", "success", "t_ok")

    def test_executor_writes_log_on_failure(self):
        class BadChannel(Channel):
            name = "bad_ch"

            def send(self, message):
                return DeliveryResult(message_id=message.message_id,
                                      channel=self.name, status="failed", error_msg="down")

            def health(self):
                return True

        db = Database(":memory:")
        reg = ChannelRegistry()
        reg.register(BadChannel())
        ex = DeliveryExecutor(reg, Router(), db=db)
        ex.send(_msg(force_channel="bad_ch", content_type="t_bad"), report_mode="debug")
        rows = db.get_connection().execute(
            "SELECT status, error FROM delivery_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "down"

    def test_executor_without_db_no_crash(self):
        class OKChannel(Channel):
            name = "ok_ch"

            def send(self, message):
                return DeliveryResult(message_id=message.message_id,
                                      channel=self.name, status="success")

            def health(self):
                return True

        reg = ChannelRegistry()
        reg.register(OKChannel())
        ex = DeliveryExecutor(reg, Router())  # db=None
        assert ex.log_store is None
        results = ex.send(_msg(force_channel="ok_ch", content_type="t_nodb"),
                          report_mode="debug")
        assert results[0].status == "success"

    def test_delivery_layer_accepts_db(self):
        db = Database(":memory:")
        layer = DeliveryLayer(config=None, db=db)
        assert layer.executor.log_store is not None
        assert db.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_log'"
        ).fetchone()


class TestMigrationV3:
    """delivery_log 走 schema_version 版本管理。

    B席 09-16: 断言改用 TARGET_VERSION 常量 —— 版本号随新迁移递增 (v7 起),
    写死数字会在每次加迁移时假红 (测的是"迁移生效"非"版本号恰为 N")。
    """

    def test_fresh_db_creates_delivery_log(self):
        from execution_layer.migrations import TARGET_VERSION, ensure_schema
        db = Database(":memory:")
        db.init_db()
        result = ensure_schema(db)
        assert result["version_after"] == TARGET_VERSION
        conn = db.get_connection()
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_log'"
        ).fetchone()

    def test_idempotent_second_run(self):
        from execution_layer.migrations import ensure_schema
        db = Database(":memory:")
        db.init_db()
        from execution_layer.migrations import TARGET_VERSION
        ensure_schema(db)
        result = ensure_schema(db)
        assert result["version_before"] == TARGET_VERSION
        assert result["applied"] == []

    def test_existing_stock_pool_path_also_creates_log(self):
        from execution_layer.migrations import ensure_schema
        db = Database(":memory:")
        db.init_db()
        # 模拟旧库: stock_pool 存在 (缺列 → v2 ALTER 会补)
        db.get_connection().execute(
            "CREATE TABLE stock_pool (code TEXT PRIMARY KEY, name TEXT)")
        db.commit()
        from execution_layer.migrations import TARGET_VERSION
        result = ensure_schema(db)
        assert result["version_after"] == TARGET_VERSION
        assert any("delivery_log" in s for s in result["applied"])
        assert any("atr_stop" in s for s in result["applied"])  # v2 补列仍生效

    # ── B席 09-16: 正面验证断言面对「迁移未生效」敏感 ──────────────
    # 统筹加码: 把写死的 version==6 换成 TARGET_VERSION 后, 必须证明
    # 「迁移没生效」这类真问题仍报得出 —— 否则只是假红换永绿
    # (与 cron_watchdog 假绿 / health 永久 red 同族病)。

    def test_migration_not_applied_is_detectable(self, monkeypatch):
        """故障注入: 清空迁移清单 (模拟"迁移没生效") → 断言面须报得出。"""
        import execution_layer.migrations as M
        real_target = M.TARGET_VERSION          # 注入前的真值 (不写死数字)
        monkeypatch.setattr(M, "MIGRATIONS", [])
        monkeypatch.setattr(M, "TARGET_VERSION", 1)
        db = Database(":memory:")
        db.init_db()
        result = M.ensure_schema(db)
        # version 面: 迁移没生效 → 到不了真 TARGET
        assert result["version_after"] != real_target
        # 表面: 迁移没跑 → 表不存在 (证明原断言的"表存在"确实敏感)
        assert not db.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='report_section_trace'"
        ).fetchone()

    def test_v7_removal_is_detectable(self, monkeypatch):
        """故障注入: 只剔除 v7 → report_section_trace 不建 (证明该表由 v7 建)。

        v8 入场后 version_after 不再是单版剔除的探针 (剔 v7 仍达 TARGET=8);
        判据改锚表存在性, 版本面断言退役 (改断言须同步改所有副本——该副本
        在 v8 加入时被漏掉, 与 test_migrations 写死版本号同族)。
        """
        import execution_layer.migrations as M
        monkeypatch.setattr(
            M, "MIGRATIONS", [(v, st) for v, st in M.MIGRATIONS if v != 7])
        monkeypatch.setattr(
            M, "TARGET_VERSION", max(v for v, _ in M.MIGRATIONS))
        db = Database(":memory:")
        db.init_db()
        M.ensure_schema(db)
        assert not db.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='report_section_trace'"
        ).fetchone()
        # 对照: delivery_log (v3) 仍在 → 剔除面精确到 v7, 非一刀切
        assert db.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_log'"
        ).fetchone()

    def test_v7_presence_assertion_is_mutation_verified(self, monkeypatch):
        """变异验证手法实战: 用 _mutation 工具证明「v7 表存在」断言敏感。

        与 test_v7_removal_is_detectable 的区别: 那条手写注入; 这条用
        可复用手法 (统筹 sm-069d97397b2244d4 归 B 席), 证明手法本身可用。
        """
        from tests.unit._mutation import assert_mutation_caught
        import execution_layer.migrations as M

        def _table_exists():
            db = Database(":memory:")
            db.init_db()
            M.ensure_schema(db)
            return db.get_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='report_section_trace'").fetchone() is not None

        assert _table_exists(), "前提: 正常态下 v7 表存在"

        real = M.MIGRATIONS
        def _drop_v7():
            monkeypatch.setattr(
                M, "MIGRATIONS", [(v, st) for v, st in real if v != 7])
            monkeypatch.setattr(M, "TARGET_VERSION", max(v for v, _ in M.MIGRATIONS))

        assert_mutation_caught(
            mutate=_drop_v7,
            check=lambda: (_ for _ in ()).throw(AssertionError("表不存在"))
            if not _table_exists() else None,
            what="v7 表存在断言",
        )
