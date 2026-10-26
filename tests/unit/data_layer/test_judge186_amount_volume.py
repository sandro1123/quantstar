# -*- coding: utf-8 -*-
"""判据 #186 + easyquotation 量/额对调 — TDD 测试 (2026-10-11 统筹)

三层守护, 缺一层就是「装了不接线」(判例 #150):
  L1 接线路  : 真调 easyquotation_source._to_kline, 喂 easyquotation 0.7.7 的原始
               dict 形状, 断 volume 必须是股数 / amount 必须是元。
               (若只测 quality.py, 把源码对调回去测试仍全绿 = 测试没在守护接线)
  L2 写入闸  : check_kline_batch 必须把倒置行打上 amount_volume_ratio_close_out_of_tier。
  L3 双向钉  : ①干净行不红 ②把闸判据废掉(改宽到恒真)必须变红 — 只会一律红的钉
               是噪音, 判据必须能被证伪 (机治钉三问第三问)。

数据全部来自 2026-10-09 生产实测 (000001 平安银行), 非构造臆测:
  baostock 权威: volume=107810561.0(股)  amount=1259717108.28(元)  close=11.59
  污染前实写:   volume=1259717108.0(元)  amount=107810561.0(股)   → 两列倒置
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from data_layer.quality import DEFAULTS, check_kline_batch
from data_layer.sources.easyquotation_source import EasyQuotationSourceAdapter as _EQ

# _to_kline 是 EasyQuotationSourceAdapter 的 staticmethod (非模块级函数),
# 这里取静态方法本体, 测试直接驱动它 — 不实例化适配器以免触发网络。(判例 #150)
_to_kline = _EQ._to_kline

RULE = "amount_volume_ratio_close_out_of_tier"

# --- 生产实测锚 (000001 2026-10-09, baostock 逐位对拍) --------------------
AUTH_VOLUME = 107810561.0        # 股
AUTH_AMOUNT = 1259717108.28      # 元
AUTH_CLOSE = 11.59
AUTH_PRE_CLOSE = 11.78
AUTH_HIGH = 11.89
AUTH_LOW = 11.58

# easyquotation 0.7.7 sina.py 真实键语义 (由库源码 int/float 转型定死):
#   turnover=int(stock[9])   -> 成交量(股)
#   volume=float(stock[10])  -> 成交额(元)
#   close  = 昨收 (非收盘价); now = 现价
EQ_RAW = {
    "name": "平安银行",
    "open": AUTH_PRE_CLOSE,
    "close": AUTH_PRE_CLOSE,          # sina 的 close 字段实为昨收
    "now": AUTH_CLOSE,
    "high": AUTH_HIGH,
    "low": AUTH_LOW,
    "buy": AUTH_LOW,
    "sell": AUTH_CLOSE,
    "turnover": int(AUTH_VOLUME),     # 成交量 股
    "volume": AUTH_AMOUNT,            # 成交额 元
    "date": "2026-10-09 15:00:04",
}


def _rec(**kw):
    """一条自洽的 kline dict (干净态: amount/volume ~= close)。"""
    base = dict(
        code="000001", date="2026-10-09",
        open=AUTH_PRE_CLOSE, high=AUTH_HIGH, low=AUTH_LOW, close=AUTH_CLOSE,
        volume=AUTH_VOLUME, amount=AUTH_AMOUNT,
        pre_close=AUTH_PRE_CLOSE,
        change_pct=round((AUTH_CLOSE / AUTH_PRE_CLOSE - 1) * 100, 2),
        turnover_rate=1.0,
    )
    base.update(kw)
    return base


# =========================================================================
# L1 接线路: 守护 easyquotation_source 的映射本身
# =========================================================================
class TestEasyquotationWiring:
    """判例 #150: 测试必须跨越对象边界 — 真调 _to_kline, 不 mock 掉被测对象。"""

    def test_volume_is_shares_and_amount_is_yuan(self):
        """eq 原始 dict 进 -> Kline 出, volume 必须是股数/amount 必须是元。"""
        k = _to_kline("000001", dict(EQ_RAW))
        assert k.volume == AUTH_VOLUME, (
            f"volume 必须是成交量(股)={AUTH_VOLUME}, 实得 {k.volume} "
            "(eq 的 turnover 键才是股数)")
        assert k.amount == AUTH_AMOUNT, (
            f"amount 必须是成交额(元)={AUTH_AMOUNT}, 实得 {k.amount} "
            "(eq 的 volume 键才是元)")

    def test_price_fields_unchanged(self):
        """同卡同时守护价格列不被顺手改坏 (半修风险: 修量额动了价格)。"""
        k = _to_kline("000001", dict(EQ_RAW))
        assert k.close == AUTH_CLOSE, f"close 应取 eq 的 now, 实得 {k.close}"
        assert k.pre_close == AUTH_PRE_CLOSE, (
            f"pre_close 应取 eq 的 close(=昨收), 实得 {k.pre_close}")
        assert (k.open, k.high, k.low) == (AUTH_PRE_CLOSE, AUTH_HIGH, AUTH_LOW)
        assert k.date == "2026-10-09", f"date 应取 eq 的 date, 实得 {k.date}"

    def test_wiring_output_passes_the_gate_clean(self):
        """L1 x L2 闭环: 修好的接线路产出的记录, 写入闸必须判干净 (不误伤)。"""
        k = _to_kline("000001", dict(EQ_RAW))
        rec = dict(code=k.code, date=k.date, open=k.open, high=k.high, low=k.low,
                   close=k.close, volume=k.volume, amount=k.amount,
                   pre_close=k.pre_close, change_pct=k.change_pct)
        flags = check_kline_batch([rec])
        assert RULE not in [f["rule"] for fl in flags.values() for f in fl], (
            f"修好的接线路被判脏 = 闸误伤: {flags}")


# =========================================================================
# L2 写入闸: check_kline_batch 必须抓得住倒置行
# =========================================================================
class TestAmountVolumeInvariant:
    def test_swapped_row_flagged(self):
        """真实污染形态 (量/额两列倒置) 必须被这条判据抓到。"""
        flags = check_kline_batch([_rec(volume=AUTH_AMOUNT, amount=AUTH_VOLUME)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert RULE in rules, (
            f"倒置行未命中 {RULE}: 该比值={(AUTH_VOLUME / AUTH_AMOUNT) / AUTH_CLOSE:.6f}, "
            "远低于下限 0.3; 旧规则集对此全盲正是 09-30~10-09 三天误放行的原因")

    def test_clean_row_not_flagged(self):
        """干净行不红 (判据非恒真 — 否则这条钉毫无信息量)。"""
        flags = check_kline_batch([_rec()])
        assert flags == {}, f"干净行被误报: {flags}"

    def test_reason_names_both_columns(self):
        """告警须可读: 报出算出的均价/收盘价/比值, 便于事后复算。"""
        flags = check_kline_batch([_rec(volume=AUTH_AMOUNT, amount=AUTH_VOLUME)])
        reasons = [f["reason"] for fl in flags.values() for f in fl
                   if f["rule"] == RULE]
        assert reasons, "倒置行应有 reason"
        assert "amount/volume=" in reasons[0] and "close=" in reasons[0], (
            f"reason 应含量与比值供复算: {reasons[0]}")

    def test_flag_targets_amount_column(self):
        """flag 落在 amount 列 — 修 DB 时按列定位。"""
        flags = check_kline_batch([_rec(volume=AUTH_AMOUNT, amount=AUTH_VOLUME)])
        fields = [f["field"] for fl in flags.values() for f in fl if f["rule"] == RULE]
        assert fields == ["amount"], f"应标记 amount 列, 实得 {fields}"

    @pytest.mark.parametrize("ratio,should_flag", [
        (0.10, True),    # 深互换 (1/close 量级)
        (0.29, True),    # 刚跌破下限
        (0.30, False),   # 下限等号侧 — 边界不清则判据可被悄悄改宽
        (1.00, False),   # 理想值
        (5.00, False),   # 上限等号侧
        (5.01, True),    # 刚突破上限
        (50.0, True),    # 极端离群
    ])
    def test_tier_boundary_is_exact(self, ratio, should_flag):
        """边界双咬: [0.3, 5] 闭区间, 等号侧判干净 (判据 #183 族: 硬编码日期测不到等号)。"""
        close = AUTH_CLOSE
        vol = 1000000.0
        rec = _rec(volume=vol, amount=vol * close * ratio)
        flags = check_kline_batch([rec])
        got = RULE in [f["rule"] for fl in flags.values() for f in fl]
        assert got is should_flag, (
            f"ratio={ratio} 应{'命中' if should_flag else '干净'}, 实得 {'命中' if got else '干净'}")

    @pytest.mark.parametrize("kw", [
        dict(volume=0),      # 无量 → 比值不可算, 跳过
        dict(volume=None),   # 缺 volume → 不可算
        dict(amount=None),   # 缺 amount → 不可算
        dict(close=0),       # 无收盘价 → 不可算
    ])
    def test_uncomputable_skips_not_crash(self, kw):
        """比值不可算时跳过判定而非抛异常 (fail-open 不许炸写入路径)。"""
        rec = _rec(**kw)
        flags = check_kline_batch([rec])  # 不得抛
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert RULE not in rules, f"{kw} 触发了不该触发的判据: {flags}"

    def test_zero_amount_with_positive_volume_IS_flagged(self):
        """amount=0 且 volume>0 是真坏行 (量在额没了) — 必须命中。
        自首: 初版测试把 amount=0 也当"应跳过", 是错的。
        同族规则 (pre_close<=0 / volume<0) 都报, 这条不报会是双标。"""
        rec = _rec(amount=0)
        flags = check_kline_batch([rec])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert RULE in rules, f"amount=0 且 volume>0 应命中, 实得 {flags}"

    def test_threshold_configurable(self):
        """阈值走 cfg 不硬编码 (与既有规则集一致, 便于观测期调宽)。"""
        rec = _rec(volume=AUTH_AMOUNT, amount=AUTH_VOLUME)
        tight = check_kline_batch([_rec()], cfg={"vap_ratio_lo": 0.0, "vap_ratio_hi": 1.0})
        rules = [f["rule"] for fl in tight.values() for f in fl]
        assert RULE in rules, "把区间收窄到 [0,1] 后, 干净行(比值~1.008)应被夹中"
        # 越宽越不应红
        wide = check_kline_batch([rec], cfg={"vap_ratio_lo": 0.0, "vap_ratio_hi": 1e9})
        assert RULE not in [f["rule"] for fl in wide.values() for f in fl]


# =========================================================================
# L3 反向钉: 证明这条钉真的在守护 (判例 #150 + 机治钉三问第三问)
# =========================================================================
class TestGuardIsNotVacuous:
    def test_deleting_the_rule_makes_test_red(self, monkeypatch):
        """把判据废成恒真 -> 倒置行必须变干净, 即上面的红确实由这条判据产生。
        若此断言不成立, 说明 test_swapped_row_flagged 是被别的规则顺带染红,
        该钉只在守护一个它并不守护的边界。"""
        monkeypatch.setattr(
            "data_layer.quality.DEFAULTS", {**DEFAULTS, "vap_ratio_lo": 0.0,
                                            "vap_ratio_hi": float("inf")})
        flags = check_kline_batch([_rec(volume=AUTH_AMOUNT, amount=AUTH_VOLUME)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert RULE not in rules, (
            "判据已恒真却仍命中 — 说明命中来自别处, 这条钉没有守护它宣称的边界")

    def test_rule_absent_on_unpatched_branch(self):
        """自证: 本规则名在未打补丁的 main 上不存在 -> 修前该钉必红。
        这是「修前红/修后绿」的可执行证据 (判例 #149 修毕须有 diff 证据)。"""
        assert RULE == "amount_volume_ratio_close_out_of_tier"
        src = Path(__file__).resolve().parents[3] / "src" / "data_layer" / "quality.py"
        assert RULE in src.read_text(encoding="utf-8"), (
            "规则名未出现在 quality.py — 补丁没打上, 测试却能绿 = 假绿")
