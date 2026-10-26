"""P-DATA-A2/A3 测试 — market 规范化 + ST 过滤修复"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from common.database import Database
from data_layer.repos.stock import StockRepo, normalize_market


class TestNormalizeMarket:
    def test_sh_6_prefix(self):
        assert normalize_market("600000", "") == "SH"
        assert normalize_market("688981", "") == "SH"  # 科创板 688

    def test_sz_0_3_prefix(self):
        assert normalize_market("000001", "") == "SZ"
        assert normalize_market("300750", "") == "SZ"  # 创业板 300
        assert normalize_market("002594", "") == "SZ"

    def test_bj_prefix(self):
        assert normalize_market("430047", "") == "BJ"
        assert normalize_market("830799", "") == "BJ"
        assert normalize_market("920808", "") == "BJ"  # 北交所新号段

    def test_chinese_market_normalized_by_code(self):
        # 中文口径也应被 code 前缀修正
        assert normalize_market("300750", "创业板") == "SZ"
        assert normalize_market("688981", "科创板") == "SH"
        assert normalize_market("920808", "北交所") == "BJ"

    def test_unknown_prefix_uses_passed_value(self):
        assert normalize_market("XXX", "SZ") == "SZ"

    def test_no_key_unknown_uses_default(self):
        assert normalize_market("XXX") == "SZ"


class TestStockRepoMarketNormalization:
    def test_batch_upsert_normalizes_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            repo = StockRepo(db)
            repo.batch_upsert([
                {"code": "600000", "name": "浦发银行", "market": "SH"},
                {"code": "300750", "name": "宁德时代", "market": "创业板"},
                {"code": "920808", "name": "曙光数创", "market": "SZ"},
                {"code": "000001", "name": "平安银行"},  # 无 market -> 应默认 SZ
            ])
            rows = {r["code"]: r["market"]
                    for r in db.get_connection().execute(
                        "SELECT code, market FROM stock_info").fetchall()}
            assert rows["600000"] == "SH"
            assert rows["300750"] == "SZ"
            assert rows["920808"] == "BJ"
            assert rows["000001"] == "SZ"
