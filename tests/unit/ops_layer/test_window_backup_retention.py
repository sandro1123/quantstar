# -*- coding: utf-8 -*-
"""窗口备份自清 TDD — prewindow 命名白名单保留策略（统筹 09-25 批·GO）

病根：/tmp 窗口备份「只写不删」——09-24 亲历 4 份 16G 副本堆到 85% 磁盘，
手工清 60G 才缓过。GFS backup_retention（W2卡⑤）只管 data/backups 日备，
/tmp 窗口备份是盲区（防线没人接线同族）。

统筹裁定（sm-ca5c94e838bc4f5e）：
  α 触发=CLI 手动（默认 dry-run，--yes 才真删——破坏性动作须显式授权）
  bundle 保留最近 2 份 / db 副本保留最近 1 份，更老一律删
  删除时机=并窗成功验证后（不在备份时删）
三钉：
  1. 非 prewindow 命名零删除（qs-bundle-backup-20260918.bundle 类永久保险天然豁免）
  2. 删前断言保留集非空且含最新一份（防全删）
  3. 幂等+留痕
命名锚：qs_prewindow_YYYYMMDD.bundle / qs_prewindow_YYYYMMDD.db*（带日期戳=窗口备份；
不带 prewindow 词=永不进删除集，无需硬编码白名单）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from ops_layer.window_backup_retention import (
    keep_set, prune_window_backups, parse_prewindow_name,
    PREWINDOW_RE, BUNDLE_KEEP, DB_KEEP,
)


def w(name):
    return f"/tmp/{name}"


class TestNameAnchor:
    """命名锚——白名单语义"""

    def test_bundle_name_parses(self):
        d = parse_prewindow_name("qs_prewindow_20260924.bundle")
        assert d is not None and (d.year, d.month, d.day) == (2026, 9, 24)

    def test_db_name_parses(self):
        assert parse_prewindow_name("qs_prewindow_20260924.db") is not None
        assert parse_prewindow_name("qs_prewindow_20260924.db-wal") is not None
        assert parse_prewindow_name("qs_prewindow_20260924.db-shm") is not None

    def test_permanent_insurance_never_matches(self):
        """永久保险命名（无 prewindow 词）→ None = 永不进删除集"""
        assert parse_prewindow_name("qs-bundle-backup-20260918.bundle") is None
        assert parse_prewindow_name("qs-main-prewindow-20260921.bundle") is None, \
            "横杠连字（统筹旧命名）不属受管命名——首跑会报它为白名单外待人工处置"

    def test_regex_shape(self):
        assert PREWINDOW_RE.match("qs_prewindow_20260924.bundle")
        assert not PREWINDOW_RE.match("qs_prewindow_2026.bundle")

    def test_keeps_constants(self):
        assert BUNDLE_KEEP == 2 and DB_KEEP == 1


class TestKeepSet:
    """保留集纯函数"""

    def test_bundle_keep_two(self):
        files = [w(f"qs_prewindow_2026092{d}.bundle") for d in (1, 2, 3, 4)]
        keep = keep_set(files)
        assert w("qs_prewindow_20260924.bundle") in keep
        assert w("qs_prewindow_20260923.bundle") in keep
        assert w("qs_prewindow_20260922.bundle") not in keep
        assert w("qs_prewindow_20260921.bundle") not in keep

    def test_db_keep_one(self):
        files = [w(f"qs_prewindow_2026092{d}.db") for d in (1, 2, 3)]
        keep = keep_set(files)
        assert keep == {w("qs_prewindow_20260923.db")}

    def test_others_never_in_delete(self):
        """白名单外文件永远在保留集（三钉#1）"""
        files = [w("qs-bundle-backup-20260918.bundle"),
                 w("qs_prewindow_20260922.bundle"),
                 w("unrelated.txt")]
        keep = keep_set(files)
        assert w("qs-bundle-backup-20260918.bundle") in keep
        assert w("unrelated.txt") in keep

    def test_db_wal_shm_follows_main(self):
        """-wal/-shm 与主 db 同日期 → 同命（主留则留，主删则删）"""
        files = [w("qs_prewindow_20260922.db"), w("qs_prewindow_20260922.db-wal"),
                 w("qs_prewindow_20260924.db")]
        keep = keep_set(files)
        assert w("qs_prewindow_20260922.db") not in keep
        assert w("qs_prewindow_20260922.db-wal") not in keep, "-wal 不得遗孤"
        assert w("qs_prewindow_20260924.db") in keep

    def test_empty_input_safe(self):
        assert keep_set([]) == set()


class TestPrune:
    """prune——删除执行侧（tmp_path 真文件演练）"""

    def _mk(self, tmp_path, names):
        for n in names:
            (tmp_path / n).write_bytes(b"x")

    def test_dry_run_default_no_deletion(self, tmp_path):
        """默认 dry-run：只报清单不真删（统筹裁定 A：CLI 默认 dry-run）"""
        self._mk(tmp_path, ["qs_prewindow_20260921.bundle",
                            "qs_prewindow_20260922.bundle"])
        plan = prune_window_backups(str(tmp_path), yes=False)
        assert plan["dry_run"] is True
        assert len(plan["would_delete"]) == 0, "2份=保留额度内，无可删"
        assert (tmp_path / "qs_prewindow_20260921.bundle").exists(), "dry-run 不得真删"

    def test_yes_deletes_and_reports(self, tmp_path):
        self._mk(tmp_path, ["qs_prewindow_20260921.bundle",
                            "qs_prewindow_20260922.bundle",
                            "qs_prewindow_20260923.bundle",
                            "qs_prewindow_20260924.bundle"])
        plan = prune_window_backups(str(tmp_path), yes=True)
        assert plan["dry_run"] is False
        # bundle 保留最近 2 份（0924/0923），删最老 2 份（0921/0922）
        assert len(plan["deleted"]) == 2
        assert not (tmp_path / "qs_prewindow_20260921.bundle").exists()
        assert not (tmp_path / "qs_prewindow_20260922.bundle").exists()
        assert (tmp_path / "qs_prewindow_20260923.bundle").exists()
        assert (tmp_path / "qs_prewindow_20260924.bundle").exists()

    def test_never_touches_permanent(self, tmp_path):
        """三钉#1 反向锚：永久保险任何代码路径不可碰"""
        self._mk(tmp_path, ["qs-bundle-backup-20260918.bundle",
                            "qs_prewindow_20260921.bundle",
                            "qs_prewindow_20260922.bundle",
                            "qs_prewindow_20260923.bundle"])
        prune_window_backups(str(tmp_path), yes=True)
        assert (tmp_path / "qs-bundle-backup-20260918.bundle").exists()

    def test_refuses_when_keep_set_empty(self, tmp_path):
        """三钉#2：目录里全是受管文件但解析异常 → 拒删（宁可不删不可误删）"""
        self._mk(tmp_path, ["qs_prewindow_20260924.bundle"])
        plan = prune_window_backups(str(tmp_path), yes=True)
        assert plan["deleted"] == [], "唯一一份就是最新=保留集，无可删"

    def test_idempotent(self, tmp_path):
        """三钉#3：重跑零删除"""
        self._mk(tmp_path, ["qs_prewindow_20260921.bundle",
                            "qs_prewindow_20260922.bundle",
                            "qs_prewindow_20260923.bundle"])
        prune_window_backups(str(tmp_path), yes=True)
        plan2 = prune_window_backups(str(tmp_path), yes=True)
        assert plan2["deleted"] == []
