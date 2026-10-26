"""ConstraintChecker 单元测试 — 入场/出场约束检查"""
from unittest.mock import MagicMock
from execution_layer.core.constraint_checker import ConstraintChecker


def test_entry_cash_balance_check():
    """Should reject entry when cash insufficient"""
    pool = MagicMock()
    pool.get_by_status.return_value = [
        {"entry_price": 100.0, "position_qty": 500}
    ]
    pool.get.return_value = None
    pool.get_available_cash.return_value = 50000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 600.0, 100)
    assert not result.passed
    assert any("余额不足" in v for v in result.violations)


def test_entry_min_shares():
    """Should pass for 100 shares (minimum lot)"""
    pool = MagicMock()
    pool.get_by_status.return_value = []
    pool.get.return_value = None
    pool.get_available_cash.return_value = 300000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 15.0, 100)
    assert result.passed


def test_exit_t1_check():
    """Should reject exit for same-day purchases"""
    from common.utils import bj_date
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active",
        "entry_date": bj_date(),
    }
    checker = ConstraintChecker(pool)
    result = checker.check_exit("000001")
    assert not result.passed
    assert any("T+1" in v for v in result.violations)


def test_entry_t1_duplicate():
    """Should reject re-buy of stock bought today"""
    from common.utils import bj_date
    pool = MagicMock()
    pool.get_by_status.return_value = []
    pool.get.return_value = {
        "code": "000001", "entry_date": bj_date(),
    }
    pool.get_available_cash.return_value = 300000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 15.0, 100)
    assert not result.passed
    assert any("T+1" in v for v in result.violations)


def test_entry_max_position_count():
    """Should reject when already holding 10 positions"""
    pool = MagicMock()
    pool.get_by_status.return_value = [
        {"entry_price": 10.0, "position_qty": 100} for _ in range(10)
    ]
    pool.get.return_value = None
    pool.get_available_cash.return_value = 300000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 15.0, 100)
    assert not result.passed
    assert any("上限10只" in v for v in result.violations)


def test_entry_max_single_position():
    """Should reject when single position exceeds 20%"""
    pool = MagicMock()
    pool.get_by_status.return_value = []
    pool.get.return_value = None
    pool.get_available_cash.return_value = 300000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 30000.0, 100)
    assert not result.passed
    assert any("20%" in v for v in result.violations)


def test_entry_all_checks_pass():
    """Should pass when all constraints met"""
    pool = MagicMock()
    pool.get_by_status.return_value = []
    pool.get.return_value = None
    pool.get_available_cash.return_value = 300000.0
    checker = ConstraintChecker(pool, total_capital=100000.0)
    result = checker.check_entry("000001", 15.0, 100)
    assert result.passed
    assert len(result.violations) == 0


def test_exit_no_position():
    """Should reject exit when not holding stock"""
    pool = MagicMock()
    pool.get.return_value = None
    checker = ConstraintChecker(pool)
    result = checker.check_exit("000001")
    assert not result.passed
    assert any("未持仓" in v for v in result.violations)


def test_exit_wrong_status():
    """Should reject exit when status is not active"""
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "watch", "entry_date": None,
    }
    checker = ConstraintChecker(pool)
    result = checker.check_exit("000001")
    assert not result.passed
    assert any("状态异常" in v for v in result.violations)


def test_exit_all_checks_pass():
    """Should pass when exit constraints met"""
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_date": "2024-01-01",
    }
    checker = ConstraintChecker(pool)
    result = checker.check_exit("000001")
    assert result.passed
    assert len(result.violations) == 0