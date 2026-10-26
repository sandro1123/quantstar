"""昨夜外盘 Section — 美股/原油/黄金/A50

说明: 外盘数据(US spot/NYMEX原油/COMEX黄金/A50期指)不属于 A 股 DataLayer 9 管道职责范围。
当前实现直接通过 akshare 拉取(独立依赖), 不写入 qs.db — 仅飞书晨报渲染使用。
降级: 若 akshare 失败, 显示"外盘数据暂不可用"而非报错。
"""
import concurrent.futures

from common.logging import get_logger
from report_layer.sections._llm_helper import llm_call
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.overnight")

_AKSHARE_TIMEOUT = 10


class OvernightMarketSection(Section):
    name = "overnight_market"

    def _fetch_with_timeout(self, fn, timeout=_AKSHARE_TIMEOUT):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(fn)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                logger.warning(f"Overnight data fetch timed out after {timeout}s")
                return None
            except Exception as e:
                logger.warning(f"Overnight data fetch failed: {e}")
                return None

    def _fetch_overnight(self) -> dict:
        data = {}
        try:
            import akshare as ak
            us_result = self._fetch_with_timeout(ak.stock_us_spot_em)
            if us_result is not None:
                for _, row in us_result.iterrows():
                    name = row.get("名称", "")
                    if "道琼斯" in name:
                        data["dow"] = round(row.get("最新价", 0), 2)
                        data["dow_pct"] = round(row.get("涨跌幅", 0), 2)
                    elif "纳斯达克" in name:
                        data["nasdaq"] = round(row.get("最新价", 0), 2)
                        data["nasdaq_pct"] = round(row.get("涨跌幅", 0), 2)
                    elif "标普" in name:
                        data["sp500"] = round(row.get("最新价", 0), 2)
                        data["sp500_pct"] = round(row.get("涨跌幅", 0), 2)
            comm = self._fetch_with_timeout(lambda: ak.futures_foreign_hist(symbol="NYMEX"))
            if comm is not None and not comm.empty:
                latest = comm.iloc[-1]
                data["oil"] = round(latest.get("close", 0), 2)
                data["oil_pct"] = round((latest.get("close", 0) / latest.get("open", 1) - 1) * 100, 2)
            gold = self._fetch_with_timeout(lambda: ak.futures_foreign_hist(symbol="COMEX"))
            if gold is not None and not gold.empty:
                latest = gold.iloc[-1]
                data["gold"] = round(latest.get("close", 0), 2)
                data["gold_pct"] = round((latest.get("close", 0) / latest.get("open", 1) - 1) * 100, 2)
        except ImportError:
            logger.warning("akshare not available")
        except Exception as e:
            logger.warning(f"Overnight data fetch failed: {e}")
        return data

    def build(self, context) -> SectionResult | None:
        try:
            data = self._fetch_overnight()
            if not data:
                return SectionResult(title="🌙 昨夜外盘", body="外盘数据暂不可用")
            # F5.5: A股惯例涨红跌绿 (🔴=涨, 🟢=跌), 与旧脚本 build_morning_brief 一致
            lines = []
            if data.get("dow"):
                dp = data.get("dow_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} 道琼斯: {data['dow']:.0f} ({dp:+.2f}%)")
            if data.get("nasdaq"):
                dp = data.get("nasdaq_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} 纳斯达克: {data['nasdaq']:.0f} ({dp:+.2f}%)")
            if data.get("sp500"):
                dp = data.get("sp500_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} 标普500: {data['sp500']:.0f} ({dp:+.2f}%)")
            if data.get("oil"):
                dp = data.get("oil_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} 原油: {data['oil']:.1f} ({dp:+.2f}%)")
            if data.get("gold"):
                dp = data.get("gold_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} 黄金: {data['gold']:.0f} ({dp:+.2f}%)")
            if data.get("a50"):
                dp = data.get("a50_pct", 0)
                arrow = "🔴" if dp > 0 else "🟢"
                lines.append(f"{arrow} A50期指: {data['a50']:.0f} ({dp:+.2f}%)")

            # F5.3: LLM 隔夜解读 (失败 → 仅行情列表, 不阻塞)
            summary = "; ".join(
                f"{label} {data.get(k + '_pct', 0):+.2f}%"
                for k, label in (("dow", "道琼斯"), ("nasdaq", "纳斯达克"),
                                 ("sp500", "标普500"), ("oil", "原油"), ("gold", "黄金"))
                if data.get(k))
            if summary:
                prompt = (
                    f"隔夜外盘: {summary}。\n"
                    f"请用1-2句话解读隔夜市场对今日A股的可能影响，不要markdown。"
                )
                comment = llm_call(context, prompt, call_point="overnight_market",
                                   temperature=0.5, max_tokens=2000)
                if comment:
                    lines.append("")
                    lines.append(f"🧠 解读: {comment}")
                    return SectionResult(title="🌙 昨夜外盘", body="\n".join(lines),
                                         sources=["akshare", "LLM"])
            return SectionResult(title="🌙 昨夜外盘", body="\n".join(lines),
                                 sources=["akshare"])
        except Exception as e:
            logger.warning(f"OvernightMarketSection build failed: {e}")
            return SectionResult(title="🌙 昨夜外盘", body="外盘数据暂不可用")
