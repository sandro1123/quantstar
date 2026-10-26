"""DL-09 幽灵源处置 — 注册表与 sources/ 适配器文件一致性"""

from pathlib import Path
from unittest.mock import Mock

from data_layer import DataLayer

SOURCES_DIR = Path(__file__).resolve().parents[3] / "src" / "data_layer" / "sources"

def test_every_adapter_file_is_registered():
    """不允许幽灵源: 注册源数量 == 适配器文件数 (*_source.py + fuyao_client.py)"""
    db = Mock()
    dl = DataLayer(db)
    registered = {s.name for s in dl._source_registry.get_all_sources()}
    adapter_files = list(SOURCES_DIR.glob("*_source.py")) + [SOURCES_DIR / "fuyao_client.py"]
    assert len(registered) == len(adapter_files), (
        f"注册源 {len(registered)} 个 != 适配器文件 {len(adapter_files)} 个: "
        f"registered={sorted(registered)}"
    )

def test_baostock_adapter_restored():
    """D-39: baostock 2026-08-22 复测证伪 DL-09 黑名单结论, 恢复接入。
    DL-35 先开放 adjust_factor/dividend 复权元数据能力; DL-25 再扩 kline_bulk。"""
    assert (SOURCES_DIR / "baostock_source.py").exists()
    db = Mock()
    dl = DataLayer(db)
    assert dl._source_registry.is_registered("baostock")
    adapter = next(s for s in dl._source_registry.get_all_sources() if s.name == "baostock")
    assert "adjust_factor" in adapter.capabilities
    assert "dividend" in adapter.capabilities

def test_easy_tdx_registered_env_gated():
    """easy_tdx 保留注册 (唯一 tick/分钟本地源); 本机不可用时 is_available() 门控自动排除"""
    db = Mock()
    dl = DataLayer(db)
    assert dl._source_registry.is_registered("easy_tdx")
    adapter = next(s for s in dl._source_registry.get_all_sources() if s.name == "easy_tdx")
    if not adapter.is_available():
        routed = [s.name for s in dl._source_registry.get_sources_for("minute_data")]
        assert "easy_tdx" not in routed

def test_gtimg_registered_with_type_priority():
    """gtimg 已注册, 且为 realtime_quote/index 主源 (DL-00 实测免费快速)"""
    db = Mock()
    dl = DataLayer(db)
    assert dl._source_registry.is_registered("gtimg")
    best_rt = dl._source_registry.get_best_source("realtime_quote")
    assert best_rt is not None and best_rt.name == "gtimg"
    best_idx = dl._source_registry.get_best_source("index")
    assert best_idx is not None and best_idx.name == "gtimg"
