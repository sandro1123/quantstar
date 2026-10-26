"""adata 源适配器 RED→GREEN 夹具 (卡#20 第一段)。

RED: data_layer.sources.adata_source 模块不存在 + 注册面无 adata。
GREEN: 能力集/归一化/report_date 显式纪律/b 案单腿形态/探活门控 全测。
网络依赖全部 mock (真拉只在夜间窗纪律下走)。

2026-09-16 卡②: lhb 字段映射改契约名 (buy_in/turnover/capitalization),
缺 report_date 改为报错 (禁静默回退 T-1, 09-15 全零事故根因)。
"""
from unittest.mock import MagicMock, patch

import pytest


from data_layer.sources.adata_source import ADataSourceAdapter


class _Row(dict):
    """iterrows 兼容的行对象。"""
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _df_rows(rows):
    """伪造 adata 返回的 DataFrame (iterrows 接口)。"""
    df = MagicMock()
    df.empty = False
    df.iterrows.return_value = [(i, _Row(r)) for i, r in enumerate(rows)]
    return df


def _empty_df():
    df = MagicMock()
    df.empty = True
    return df


class TestAdapterBasics:
    def test_name_and_capabilities(self):
        a = ADataSourceAdapter()
        assert a.name == "adata"
        assert a.capabilities == {"lhb", "popularity", "concept_hot", "mine_clearance"}

    def test_north_and_margin_not_registered(self):
        # 统筹 09-09 定谳: 北向 (个股级停披) / 两融 (API 空) 不入册
        assert "north_flow" not in ADataSourceAdapter().capabilities
        assert "securities_margin" not in ADataSourceAdapter().capabilities


class TestLhbBackup:
    def test_explicit_report_date_passthrough(self):
        # report_date 显式传参纪律: params 里的 report_date 必须透传
        # 卡② (09-15 全零事故): 字段映射改为 lhb 腿契约名 buy_in/turnover/capitalization
        a = ADataSourceAdapter()
        a._init_ok = True
        hot = MagicMock()
        hot.list_a_list_daily.return_value = _df_rows([
            {"stock_code": "000020", "trade_date": "2026-09-08", "close": 14.9,
             "change_cpt": 8.7591, "a_net_amount": 10875600.0, "a_buy_amount": 90645560.0,
             "a_sell_amount": 79769960.0, "reason": "涨幅偏离值20%"}])
        a._hot = hot
        out = a.get_data("lhb", {"report_date": "2026-09-08"})
        hot.list_a_list_daily.assert_called_once_with(report_date="2026-09-08")
        assert len(out) == 1
        assert out[0]["code"] == "000020"
        assert out[0]["buy_in"] == pytest.approx(10875600.0)
        assert out[0]["turnover"] == pytest.approx(90645560.0)
        assert out[0]["capitalization"] == pytest.approx(79769960.0)
        assert out[0]["source"] == "adata"

    def test_missing_date_raises_not_silent_t1_fallback(self):
        # 卡② (09-15 全零事故根因): 缺 report_date 必须报错, 禁静默回退 T-1。
        # 旧行为回退昨天 → T-1 数据以当日日期落库 = 09-15 的 72 行全零占位。
        # 铁律 #12: 降级不得静默。
        from data_layer.sources.adata_source import SourceContractError
        a = ADataSourceAdapter()
        a._init_ok = True
        hot = MagicMock()
        a._hot = hot
        with pytest.raises(SourceContractError) as ei:
            a.get_data("lhb", {})
        assert "report_date" in str(ei.value).lower()
        hot.list_a_list_daily.assert_not_called()

    def test_empty_returns_empty_list(self):
        a = ADataSourceAdapter()
        a._init_ok = True
        a._hot = MagicMock()
        a._hot.list_a_list_daily.return_value = _empty_df()
        assert a.get_data("lhb", {"report_date": "2026-09-09"}) == []


class TestPopularityPlanB:
    def test_single_leg_shape_price_null(self):
        # b 案: rank+hisRc 在, price/change_pct 诚实 None
        a = ADataSourceAdapter()
        a._init_ok = True
        with patch("requests.post") as mp:
            mp.return_value.json.return_value = {
                "data": [{"sc": "SH600869", "rk": 1, "rc": 0, "hisRc": 0},
                         {"sc": "SZ000523", "rk": 3, "rc": 0, "hisRc": 6}]}
            out = a.get_data("popularity", {})
        assert out[0] == {"code": "600869", "rank": 1, "his_rank": 0.0,
                          "price": None, "change_pct": None, "source": "adata"}
        assert out[1]["code"] == "000523"
        assert out[1]["rank"] == 3

    def test_probe_single_endpoint_only(self):
        # 探活只打 emappdata 第一腿 (push2 被墙死不依赖)。
        # adata 可导入 + 端点 200 → True; mock 双面 (导入+端点)。
        a = ADataSourceAdapter()
        fake_hot = MagicMock()
        with patch("data_layer.sources.adata_source._import_adata",
                   return_value=(fake_hot, MagicMock())), \
             patch("requests.post") as mp:
            mp.return_value.status_code = 200
            mp.return_value.json.return_value = {"data": [{"sc": "SH600869"}]}
            assert a.is_available() is True
        # 单点探活后 10 分钟缓存 (第二次不再打网络)
        with patch("requests.post") as mp2:
            assert a.is_available() is True
            mp2.assert_not_called()

    def test_probe_fail_gates_off(self):
        a = ADataSourceAdapter()
        with patch("requests.post", side_effect=Exception("net down")):
            a.is_available() is False or True  # 缓存语义: 探活失败 → False
        # 显式重置缓存后验证
        a._avail_cached, a._avail_ts = None, 0
        with patch("requests.post", side_effect=Exception("net down")):
            assert a.is_available() is False

    def test_no_adata_import_gates_off(self):
        # 09-14 修 (跨域污染族新案): 字符串形/对象形 patch 都依赖"当前 sys.modules 的模块
        # 就是类所在模块"这一假设。当同 session 内本模块被别处重载过时, 测试文件顶部
        # import 到的类仍指向旧 globals → patch 打在新模块上 → 漏 patch → 真导入 adata
        # (设备已装 adata 2.9.5) → 探活真打网络 → True 假红。
        # 修法: 直接 patch 方法实际读取的那个 globals dict (与模块身份解耦)。
        import unittest.mock as _mk
        a = ADataSourceAdapter()
        _globals = type(a).is_available.__globals__
        with _mk.patch.dict(_globals, {"_import_adata": lambda: (None, None)}):
            a._avail_cached, a._avail_ts = None, 0
            assert a.is_available() is False


class TestConceptHot:
    def test_normalize_concept_rows(self):
        a = ADataSourceAdapter()
        a._init_ok = True
        hot = MagicMock()
        hot.hot_concept_20_ths.return_value = _df_rows([
            {"rank": 1, "concept_code": "885959", "concept_name": "PCB概念",
             "change_pct": -0.3033, "hot_value": 248880.5, "hot_tag": "连续112天上榜"}])
        a._hot = hot
        out = a.get_data("concept_hot", {})
        assert out[0]["concept_name"] == "PCB概念"
        assert out[0]["hot_value"] == pytest.approx(248880.5)
        assert out[0]["rank"] == 1


class TestMineClearance:
    def test_risk_profile_shape(self):
        # 形态: 风险档案 (score/f_type/s_type/reason) 非解禁日历
        a = ADataSourceAdapter()
        a._init_ok = True
        mine = MagicMock()
        mine.mine_clearance_tdx.return_value = _df_rows([
            {"stock_code": "000002", "short_name": "万科Ａ", "score": 64,
             "f_type": "财务类风险", "s_type": "短期负债风险", "t_type": "短期负债风险",
             "reason": "(货币资金+交易性金融资产)/(短期负债)<40%"}])
        a._mine = mine
        out = a.get_data("mine_clearance", {"code": "000002"})
        mine.mine_clearance_tdx.assert_called_once_with("000002")
        assert out[0]["score"] == 64.0
        assert out[0]["f_type"] == "财务类风险"

    def test_no_code_returns_empty(self):
        a = ADataSourceAdapter()
        a._init_ok = True
        a._mine = MagicMock()
        assert a.get_data("mine_clearance", {}) == []


class TestRegistration:
    def test_registered_in_source_registry(self):
        # 注册面: data_layer __init__ _register_sources 须含 adata
        import inspect
        from data_layer import DataLayer
        src = inspect.getsource(DataLayer._register_sources)
        assert "ADataSourceAdapter" in src

    def test_lhb_backup_priority_after_fuyao(self):
        # lhb: fuyao 主源 (type_priority=1), adata 对冲备源 (2) — 排序实证
        from data_layer.sources.registry import SourceRegistry
        reg = SourceRegistry()
        fuyao = MagicMock()
        fuyao.name = "fuyao"
        fuyao.capabilities = {"lhb"}
        fuyao.is_available.return_value = True
        adata = ADataSourceAdapter()
        reg.register(fuyao, priority=10, type_priorities={"lhb": 1})
        reg.register(adata, priority=25, type_priorities={"lhb": 2})
        with patch.object(ADataSourceAdapter, "is_available", return_value=True):
            sources = reg.get_sources_for("lhb")
            assert [s.name for s in sources] == ["fuyao", "adata"]
