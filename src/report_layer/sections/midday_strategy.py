"""下午策略 Section — LLM 基于上午盘面生成下午策略

S5回归修复: 原实现从 snapshot.indices 取指数文本, 但 DataSnapshot 无 indices 字段,
恒为空 → 直接返回 "数据暂不可用"。改为走 gtimg 实时指数构建 prompt。

R3 (设计稿-C §2.4 第 3 条): LLM 输入必须注入候选股清单+实时价,
修 midday_strategy 的连续性断裂 — 晨报推的候选, 下午策略必须接着谈。
候选口径复用 midday_candidate_tracking.load_candidates (单一来源)。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.midday_strategy")

class MiddayStrategySection(Section):
    name = "midday_strategy"

    def _candidates_text(self, context) -> str:
        """R3: 候选股清单文本 (code 名称 现价 vs 区间), 注入 LLM 输入。"""
        try:
            from report_layer.sections.midday_candidate_tracking import load_candidates
            candidates = load_candidates(context)
            if not candidates:
                return ""
            codes = [ll.code for ll in candidates]
            prices = {}
            try:
                prices = context.data_layer.get_realtime_prices(codes) or {}
            except Exception as _deg_exc:
                note_degraded("midday_strategy", _deg_exc)
                pass
            parts = []
            for ll in candidates:
                price = float(prices.get(ll.code, 0) or 0)
                low = ll.entry_low or 0
                high = ll.entry_high or 0
                price_str = f"{price:.2f}" if price > 0 else "?"
                range_str = f"[{low:.2f}, {high:.2f}]" if low > 0 and high > 0 else "?"
                parts.append(f"{ll.name}({ll.code}) 现价{price_str} 买入区间{range_str}")
            return "；".join(parts)
        except Exception as e:
            logger.warning(f"candidates text failed: {e}")
            return ""

    def build(self, context) -> SectionResult | None:
        try:
            indices_text = ""
            try:
                from report_layer.sections.indices import fetch_realtime_indices
                idx = fetch_realtime_indices(context.data_layer)[:3]
                if idx:
                    parts = [f"{i.get('name','')}: {i.get('price',0)} ({(i.get('change_pct',0) or 0):+.2f}%)"
                             for i in idx]
                    indices_text = "；".join(parts)
            except Exception as _deg_exc:
                note_degraded("midday_strategy", _deg_exc)
                pass

            if not indices_text:
                return SectionResult(title="🧠 下午策略", body="数据暂不可用")

            # AG-10: 只用 bootstrap 注入的门内叙述柜台; 无则规则降级 (不伸手 _pm)。
            llm = getattr(context, "llm_service", None)

            if llm is None or not llm.is_available():
                up = sum(1 for i in indices_text.split("；") if "+" in i)
                total = len(indices_text.split("；"))
                direction = "偏多" if up > total / 2 else "偏空" if up < total / 2 else "震荡"
                return SectionResult(title="🧠 下午策略",
                                     body=f"方向判断: {direction}" + chr(10) + "建议仓位: 5-7成",
                                     sources=["rule-based"])

            # R3: 候选股清单注入 prompt (修连续性断裂)
            candidates_text = self._candidates_text(context)
            prompt = (
                "今日上午A股指数数据:" + chr(10) + indices_text + chr(10) + chr(10)
            )
            if candidates_text:
                prompt += (
                    "今日候选股(晨报已推, 下午策略须围绕其延续性):" + chr(10)
                    + candidates_text + chr(10) + chr(10)
                )
            prompt += (
                "请分析上午盘面，给出下午策略建议，不要markdown:" + chr(10) +
                "方向判断: " + chr(10) + "板块关注: " + chr(10) + "操作建议: "
            )
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(lambda: llm.call(prompt, temperature=0.5, max_tokens=2000,
                                                 call_point="midday_strategy"))
                result = fut.result(timeout=20)
            if result:
                import re
                return SectionResult(title="🧠 下午策略",
                                     body=re.sub(r"#{1,6}\s*\*?", "", result.strip()),
                                     sources=["LLM"])
            return SectionResult(title="🧠 下午策略", body="研判暂不可用")
        except Exception as e:
            logger.warning(f"Strategy failed: {e}")
            return SectionResult(title="🧠 下午策略", body="研判暂不可用")
