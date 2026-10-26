"""OPS-08 (D-30): AutoHealer switch_source 冻结 — 不许伪造切换成功

测行为边界:
- data_source_down / source_stale 两类异常都走诚实 not_implemented
- action_log 落库 status 与返回值一致 (不是 success)
- 返回与落库文案中不得出现"已切换"假事实
"""
import pytest

from common.database import Database
from ops_layer.auto_healer import AutoHealer
from ops_layer.core.tables import OPS_TABLES_DDL


@pytest.fixture
def healer():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return AutoHealer(db), conn


class TestSwitchSourceFrozen:
    def test_data_source_down_honest_not_implemented(self, healer):
        ah, conn = healer
        result = ah.heal({"anomaly_type": "data_source_down", "source": "akshare"})
        assert result["status"] == "not_implemented"
        assert "已切换" not in result.get("detail", "")

    def test_source_stale_also_frozen(self, healer):
        ah, conn = healer
        result = ah.heal({"anomaly_type": "source_stale", "pipeline": "daily_data_pipeline"})
        assert result["status"] == "not_implemented"
        assert "已切换" not in result.get("detail", "")

    def test_action_log_records_not_implemented(self, healer):
        ah, conn = healer
        ah.heal({"anomaly_type": "data_source_down", "source": "akshare"})
        row = conn.execute(
            "SELECT status, detail FROM action_log WHERE action = 'switch_source'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "not_implemented"
        assert "已切换" not in (row["detail"] or "")

    def test_module_source_has_no_fake_switch_text(self, healer):
        """D-30 裁决: 假文案必须从代码中移除, 防止回归"""
        import inspect
        from ops_layer import auto_healer
        source = inspect.getsource(auto_healer)
        assert "已切换至备用数据源" not in source
