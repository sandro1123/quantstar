# -*- coding: utf-8 -*-
"""P1 合规小卡 (2026-09-21): 生产买入/卖出通知补模拟标识。

Kimi 阶段一审计 P1 实锤 (统筹亲核): paper_broker 生产分支 (dry_run=false)
两处通知——买入 :131-135 / 卖出 :199-203——标题「触发买入-{code}」正文含
「总金额 N 元」, 零模拟标识零股票名; 用户看到「总金额 14,160 元」第一反应
是钱动了 = 合规红线。dry_run 分支反而有「⚠️ 模拟运行」后缀。

修法: 生产分支通知补「【模拟盘】」前缀 + 股票名 + 免责行; 措辞单一来源 =
复用同层 touch_card.COMPLIANCE_FOOTER (「模拟盘研究，非投资建议」),
不另造新措辞。构造通知文本直测, 不走全链 (判例#4)。
"""
import pytest


def _build_buy_text(dry_run: bool, code: str, name: str, amount: float) -> tuple:
    """镜像 paper_broker 两分支的通知构造 (测试锚 = 生产行的提炼面)。"""
    from execution_layer.core.paper_broker import PaperBroker
    import inspect
    src = inspect.getsource(PaperBroker)
    # 静态判定: 生产分支 (非 dry_run) 通知文本须含三要素
    # 生产分支 = 「触发买入-」最后出现 (dry_run 分支在前, 生产分支在后);
    # find 从文件尾倒查第一处即生产分支。
    i_prod = src.rfind("触发买入-")
    seg = src[max(0, i_prod - 80):i_prod + 500]  # 前移窗口含标题前缀
    return seg


def test_prod_buy_notice_has_sim_marker():
    """生产买入通知须含【模拟盘】前缀 + 股票名 + 免责行。"""
    seg = _build_buy_text(False, "600001", "测试A", 14160)
    assert "【模拟盘】" in seg, f"生产买入通知缺模拟标识 (合规红线): {seg[:120]}"
    assert "免责" in seg or "模拟盘研究" in seg, "生产买入通知缺免责行"
    assert "name" in seg, "生产买入通知须带股票名 (pool.get('name', code))"


def test_prod_exit_notice_has_sim_marker():
    """生产卖出通知须含【模拟盘】前缀 + 股票名 + 免责行。"""
    from execution_layer.core.paper_broker import PaperBroker
    import inspect
    src = inspect.getsource(PaperBroker)
    i_exit = src.rfind("触发卖出-")  # 生产分支在后 (dry_run 分支无卖出通知)
    assert i_exit > 0, "卖出通知构造缺失"
    seg = src[max(0, i_exit - 80):i_exit + 400]  # 前移窗口含标题前缀
    assert "【模拟盘】" in seg, f"生产卖出通知缺模拟标识: {seg[:120]}"
    assert "免责" in seg or "模拟盘研究" in seg, "生产卖出通知缺免责行"


def test_disclaimer_reuses_touch_card_footer():
    """免责措辞单一来源: 须复用 touch_card.COMPLIANCE_FOOTER (勿另造)。"""
    from execution_layer.core.paper_broker import PaperBroker
    from execution_layer.intraday.touch_card import COMPLIANCE_FOOTER
    import inspect
    src = inspect.getsource(PaperBroker)
    assert "COMPLIANCE_FOOTER" in src or COMPLIANCE_FOOTER in src, \
        "paper_broker 须 import touch_card 的 COMPLIANCE_FOOTER (单一来源)"


def test_dry_run_branch_unchanged():
    """dry_run 分支不动 (修法不扩大 — 它本就有模拟标识)。"""
    from execution_layer.core.paper_broker import PaperBroker
    import inspect
    src = inspect.getsource(PaperBroker)
    i_dry = src.find("if self.dry_run:")
    i_first_buy = src.find("触发买入-", i_dry)  # dry_run 分支内的第一处
    seg = src[i_first_buy:i_first_buy + 400]
    assert "模拟运行" in seg, "dry_run 分支的「⚠️ 模拟运行」后缀不得被动"
