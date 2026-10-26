"""生成早报样本 — 保存预览 + 推送飞书 DM"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
from common.database import Database
from common.utils import bj_date
from report_layer import ReportLayer
from report_layer.reports.morning_brief import MorningBriefBuilder
from report_layer.reports.base import ReportContext


def _mock_data_layer():
    dl = MagicMock()
    dl.snapshot.return_value = MagicMock(
        date=bj_date(),
        klines=[
            MagicMock(code="600519", date=bj_date(), close=1520.0, open=1510.0, high=1530.0, low=1505.0,
                      volume=5e6, amount=7.6e9, pre_close=1510.0, change_pct=0.66, turnover_rate=0.5),
            MagicMock(code="000858", date=bj_date(), close=135.0, open=134.0, high=136.0, low=133.5,
                      volume=3e6, amount=4.05e8, pre_close=134.0, change_pct=0.75, turnover_rate=0.8),
            MagicMock(code="300750", date=bj_date(), close=210.0, open=208.0, high=212.0, low=207.0,
                      volume=8e6, amount=1.68e9, pre_close=208.0, change_pct=0.96, turnover_rate=1.2),
            MagicMock(code="601318", date=bj_date(), close=48.5, open=48.0, high=49.0, low=47.8,
                      volume=1e7, amount=4.85e8, pre_close=48.0, change_pct=1.04, turnover_rate=0.6),
            MagicMock(code="000333", date=bj_date(), close=62.0, open=61.5, high=62.5, low=61.2,
                      volume=6e6, amount=3.72e8, pre_close=61.5, change_pct=0.81, turnover_rate=0.9),
        ],
        financials=[],
        valuations=[],
        moneyflow=[],
        news=[],
        limit_up_down=[],
        holdings=[],
        recommendations=[],
        market=MagicMock(state="normal", up_count=2500, down_count=1200, flat_count=300),
        indices=[MagicMock(code="000001.SH", name="上证指数", close=3080.5, change_pct=0.35),
                 MagicMock(code="399001.SZ", name="深证成指", close=9850.0, change_pct=0.48)],
    )
    return dl


def _mock_strategy_layer():
    sl = MagicMock()
    sl.run_daily.return_value = MagicMock(
        signals=[
            MagicMock(code="600519", action="buy", strength=0.82),
            MagicMock(code="300750", action="buy", strength=0.71),
            MagicMock(code="000858", action="buy", strength=0.65),
            MagicMock(code="601318", action="hold", strength=0.55),
            MagicMock(code="000333", action="buy", strength=0.62),
        ],
        regime="震荡偏多",
    )
    def mock_get_scores(code, date):
        scores = {
            "600519": {"momentum_20d": 0.35, "value_discount": 0.22, "roe_quality": 0.18, "moneyflow_net": 0.07},
            "300750": {"momentum_20d": 0.28, "rsi_14": 0.25, "avg_turnover_rate": 0.12, "market_sentiment": 0.06},
            "000858": {"value_discount": 0.30, "momentum_20d": 0.20, "dividend_yield": 0.10, "roe_quality": 0.05},
            "601318": {"value_discount": 0.25, "dividend_yield": 0.15, "momentum_20d": 0.10, "eps_factor": 0.05},
            "000333": {"roe_quality": 0.28, "momentum_20d": 0.18, "earnings_stability": 0.10, "asset_turnover": 0.06},
        }
        s = scores.get(code, {})
        ms = MagicMock()
        ms.factors = s
        ms.composite_score = sum(s.values()) / max(len(s), 1)
        return ms
    sl.get_scores = mock_get_scores
    sl.feedback = {"ic_tuner": MagicMock()}
    sl.feedback["ic_tuner"].get_factor_ic.return_value = {
        "momentum_20d": 0.12, "value_discount": 0.08, "roe_quality": 0.06,
        "ep_ratio": 0.05, "moneyflow_net": 0.04, "rsi_14": 0.03,
        "dividend_yield": 0.02, "avg_turnover_rate": 0.01,
    }
    return sl


def _mock_execution_layer():
    el = MagicMock()
    el.stock_pool = MagicMock()
    el.stock_pool.get_by_status.return_value = [
        {"code": "600519", "name": "贵州茅台", "initial_score": 0.82, "watch_reason": "动量突破+估值折价",
         "days_in_watch": 3, "current_score": 0.85},
        {"code": "300750", "name": "宁德时代", "initial_score": 0.71, "watch_reason": "RSI金叉+放量",
         "days_in_watch": 2, "current_score": 0.73},
        {"code": "000858", "name": "五粮液", "initial_score": 0.65, "watch_reason": "低估值+高股息",
         "days_in_watch": 5, "current_score": 0.68},
    ]
    el.get_portfolio_snapshot.return_value = MagicMock(
        active_count=3, pending_entry_count=2, watch_count=5,
        total_nav=1000000.0, today_pnl=12500.0,
    )
    return el


def _mock_agent_layer():
    al = MagicMock()
    al.analyze.return_value = MagicMock(
        verdict="偏多",
        confidence=0.65,
        reasoning="昨夜外盘偏暖，A50期指小幅上涨。国内政策面支持，市场情绪修复中。"
                   "短期关注量能配合，若放量突破3100点可加仓。",
        risk_flags=[
            MagicMock(flag="美联储降息预期推迟", severity="medium"),
            MagicMock(flag="北向资金连续3日净流出", severity="medium"),
        ],
        output={"position_pct": 0.6},
    )
    return al


def main():
    date = bj_date()
    print(f"=== 生成早报样本: {date} ===")

    rl = ReportLayer(
        data_layer=_mock_data_layer(),
        strategy_layer=_mock_strategy_layer(),
        execution_layer=_mock_execution_layer(),
        agent_layer=_mock_agent_layer(),
    )
    rl.use_text_fallback()

    message = rl.build_morning_brief(date, report_mode="preview")
    if message:
        print("\n" + "=" * 50)
        print("早报内容:")
        print("=" * 50)
        content = message.content
        if isinstance(content, dict):
            sections = content.get("sections", [])
            for s in sections:
                print(f"\n--- {s.get('title', '')} ---")
                print(s.get("body", ""))
        else:
            print(str(content))
        print("\n" + "=" * 50)
        import json, os
        output_dir = os.path.join("output", "preview")
        os.makedirs(output_dir, exist_ok=True)
        save_path = os.path.join(output_dir, f"morning_brief_{date}.txt")
        with open(save_path, "w", encoding="utf-8") as f:
            if isinstance(content, dict):
                sections = content.get("sections", [])
                for s in sections:
                    f.write(f"--- {s.get('title', '')} ---\n")
                    f.write(f"{s.get('body', '')}\n\n")
            else:
                f.write(str(content))
        print(f"\n✅ 预览已保存: {save_path}")
    else:
        print("❌ 早报生成失败")


if __name__ == "__main__":
    main()