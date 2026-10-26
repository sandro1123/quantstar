"""P-DATA-A3 测试 — ST 过滤修复(按名称判断而非 code 前缀)"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from common.database import Database
from data_layer._loader import DataLoader


class TestIsStStock:
    def _make_loader(self, rows):
        tmp = tempfile.TemporaryDirectory()
        db = Database(str(Path(tmp.name) / "test.db"))
        conn = db.get_connection()
        conn.execute("""
            CREATE TABLE stock_info (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT DEFAULT 'SZ',
                is_active INTEGER DEFAULT 1
            )
        """)
        for code, name in rows:
            conn.execute(
                "INSERT INTO stock_info (code, name) VALUES (?, ?)",
                (code, name),
            )
        conn.commit()
        return DataLoader(db)

    def test_real_st_stock_by_name(self):
        # 真实 A 股 ST: code 数字开头, 名称含 ST
        loader = self._make_loader([
            ("000430", "ST张家界"),
            ("000488", "ST晨鸣"),
            ("600519", "贵州茅台"),
        ])
        assert loader.is_st_stock("000430") is True
        assert loader.is_st_stock("000488") is True
        assert loader.is_st_stock("600519") is False

    def test_star_st_stock(self):
        loader = self._make_loader([
            ("000001", "*ST示例"),
            ("600000", "正常股票"),
        ])
        assert loader.is_st_stock("000001") is True
        assert loader.is_st_stock("600000") is False

    def test_unknown_code_not_st(self):
        loader = self._make_loader([("600000", "正常")])
        assert loader.is_st_stock("999999") is False

    def test_empty_name_not_st(self):
        loader = self._make_loader([("600000", "")])
        assert loader.is_st_stock("600000") is False

    def test_st_in_middle_but_not_prefix(self):
        # "ST" 在名称中间(非前缀)不算 ST? 用前3字符判断策略
        loader = self._make_loader([("600000", "SST股份")])
        # "SST" 前3字符含 ST -> 视为风险警示
        assert loader.is_st_stock("600000") is True
