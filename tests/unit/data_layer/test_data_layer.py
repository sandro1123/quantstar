"""test_data_layer.py — DataLayer 初始化测试"""

from unittest.mock import Mock

from data_layer import DataLayer

def test_datalayer_has_source_registry():
    """测试 DataLayer 有 SourceRegistry 等新组件"""
    db = Mock()
    dl = DataLayer(db)

    assert hasattr(dl, '_source_registry')
    assert hasattr(dl, '_schema_evolution')

def test_datalayer_baostock_restored():
    """D-39: 2026-08-22 复测证伪 DL-09 黑名单结论 (login error_code=0), baostock 恢复注册"""
    db = Mock()
    dl = DataLayer(db)
    names = [s.name for s in dl._source_registry.get_all_sources()]
    assert 'baostock' in names

def test_datalayer_registration_does_not_break_other_sources():
    """源注册集合: DL-09 其余 9 源在册 + D-39 baostock 恢复 = 10 源"""
    db = Mock()
    dl = DataLayer(db)
    names = [s.name for s in dl._source_registry.get_all_sources()]
    # 卡#20 第一段: adata 注册后 11 源 (意图不变: 注册不破坏其他源)
    # akshare fallback 修法卡 (09-28): +easyquotation = 12 源 (kline 链第三位接力)
    expected = {
        'tushare', 'akshare_sina', 'mootdx', 'tonghuashun',
        'fuyao', 'akshare', 'zzshare', 'easy_tdx', 'gtimg', 'baostock',
        'adata', 'easyquotation',
    }
    assert set(names) == expected
    assert len(names) == 12



def test_suspend_recovery_registered_with_source():
    """D3 注册链覆盖 (09-12 夜班判例): 注册链缺陷 (name property 误调 / 源注入
    先于 _register_sources) 须有经 DataLayer 实例化取管道的测试形态 —
    mock 直构管道打不到注册链 (10P 全绿却漏掉 main 活缺陷的教训)。
    """
    db = Mock()
    dl = DataLayer(db)
    pipe = dl.pipelines.get("suspend_recovery")
    assert pipe is not None, "suspend_recovery 未注册"
    assert pipe.source is not None, (
        "注册期源注入失败 — _register_pipelines 先于 _register_sources, "
        "回注逻辑须在 __init__ 尾 (fcf86292 形态)")
    assert type(pipe.source).__name__ == "BaoStockSourceAdapter"
