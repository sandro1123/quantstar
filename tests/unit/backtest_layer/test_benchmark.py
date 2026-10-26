"""Benchmark 单元测试"""
import pytest
from backtest_layer.core.benchmark import Benchmark


def test_benchmark_excess_return():
    b = Benchmark()
    assert abs(b.calc_excess_return(0.15, 0.10) - 0.05) < 1e-10


def test_benchmark_information_ratio():
    b = Benchmark()
    ir = b.calc_information_ratio([0.05, 0.03, -0.01, 0.04])
    assert ir is not None
    assert ir > -1.0


def test_information_ratio_uses_sample_std_dev():
    """Information Ratio 应使用样本标准差 (Bessel 修正 ddof=1), 而非总体标准差。"""
    b = Benchmark()
    excess = [0.05, 0.03, -0.01, 0.04]
    mean = sum(excess) / len(excess)
    sample_var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    expected = mean / (sample_var ** 0.5)
    assert abs(b.calc_information_ratio(excess) - expected) < 1e-10


def test_information_ratio_single_point_returns_zero():
    """len<2 无法计算 IR, 应返回 0.0 而非除零。"""
    b = Benchmark()
    assert b.calc_information_ratio([0.01]) == 0.0
    assert b.calc_information_ratio([]) == 0.0
