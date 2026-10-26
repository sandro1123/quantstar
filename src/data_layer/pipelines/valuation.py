"""估值采集管道：批量主源、只补缺口、覆盖率门禁、单次写入。"""

from __future__ import annotations

import math
from typing import Any

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos import get_repo

logger = get_logger("data.valuation")

# 结构性无覆盖代码段 — 估值分母豁免清单 (估值断供卡定谳, 2026-09-25)。
#
# 43/83/87 = 老三板/北交所旧代码段 (stock_info 里 250 只 is_active=1)。
# 生产实证: 2026-09 整月 daily_klines 零行、stock_valuation 零行; 全历史仅
# 2026-07-17 单日出现过 248 行 (当天是一次性全量回补日, klines 同日也短暂
# 出现老段), 此后两月两表皆零 —— 即这批代码既不交易也无估值源覆盖。
#
# ★豁免锚必须锚到「源覆盖事实」, 不是锚到「交易所归属」:
#   **禁用 market='BJ' 作豁免锚** —— BJ 同时含 920 活段 (348 只, 健康日
#   343/343、344/344、345/345 = 100% 覆盖) 与本清单的 250 只死段; 按 market
#   豁免会把 348 只真缺口一起藏掉 (过度豁免 = 另一种假绿)。
UNCOVERED_CODE_PREFIXES = ("43", "83", "87")


class ValuationPipeline(BasePipeline):
    name = "valuation"
    requires = ["stock_info"]
    produces = ["stock_valuation"]
    capability = "historical_valuation"

    def __init__(self, source_registry=None):
        self.source_registry = source_registry

    @staticmethod
    def _normalize_date(value: Any, fallback: str) -> str:
        raw = str(value or fallback)
        compact = raw.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        return raw

    @staticmethod
    def _optional_number(
        record: dict, field: str, alias: str | None = None
    ) -> float | None:
        """Normalize one optional numeric field without fabricating 0.

        ``None`` and empty/blank strings stay ``None``. Valid finite numbers
        become ``float``. ``pb`` is honored as an alias for ``pb_mrq`` only
        when the primary field is absent or blank, and a missing alias never
        produces 0. Non-finite or unparseable values raise ``ValueError`` so
        the record fails validation.
        """
        raw = record.get(field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if alias is None:
                return None
            raw = record.get(alias)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"invalid valuation number for {field}: {raw!r}")
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite valuation number for {field}: {raw!r}")
        return parsed

    @classmethod
    def _normalize_record(
        cls,
        record: Any,
        fallback_date: str,
        expected_symbols: set[str],
    ) -> dict | None:
        if not isinstance(record, dict):
            return None
        code = str(
            record.get("code") or record.get("ts_code") or record.get("symbol") or ""
        ).split(".", 1)[0]
        if not code or (expected_symbols and code not in expected_symbols):
            return None
        date = cls._normalize_date(
            record.get("date") or record.get("trade_date"), fallback_date
        )
        if date != cls._normalize_date(fallback_date, fallback_date):
            return None
        try:
            pe_ttm = cls._optional_number(record, "pe_ttm")
            pb_mrq = cls._optional_number(record, "pb_mrq", alias="pb")
            ps_ttm = cls._optional_number(record, "ps_ttm")
            market_cap = cls._optional_number(record, "market_cap")
            total_shares = cls._optional_number(record, "total_shares")
            dividend_yield = cls._optional_number(record, "dividend_yield")
        except ValueError:
            return None
        core_fields = [pe_ttm, pb_mrq, ps_ttm, market_cap]
        if not any(
            value is not None and value != 0.0 for value in core_fields
        ):
            return None
        return {
            "code": code,
            "date": date,
            "pe_ttm": pe_ttm,
            "pb_mrq": pb_mrq,
            "ps_ttm": ps_ttm,
            "market_cap": market_cap,
            "total_shares": total_shares,
            "dividend_yield": dividend_yield,
        }

    @staticmethod
    def _classify_segment(code: str) -> str:
        if not code:
            return "UNKNOWN"
        first = code[0]
        if first == "6":
            return "SH"
        if first in ("0", "3"):
            return "SZ"
        if first in ("4", "8", "9"):
            return "BJ"
        return "UNKNOWN"

    @staticmethod
    def _compute_segment_coverage(
        expected: set[str], covered: set[str]
    ) -> dict[str, dict]:
        expected_per_seg: dict[str, set[str]] = {}
        for code in expected:
            expected_per_seg.setdefault(
                ValuationPipeline._classify_segment(code), set()
            ).add(code)
        covered_per_seg: dict[str, set[str]] = {}
        for code in covered:
            covered_per_seg.setdefault(
                ValuationPipeline._classify_segment(code), set()
            ).add(code)
        result: dict[str, dict] = {}
        for seg in sorted(expected_per_seg):
            exp = expected_per_seg[seg]
            cov = covered_per_seg.get(seg, set())
            missing_list = sorted(exp - cov)
            result[seg] = {
                "expected": len(exp),
                "covered": len(cov),
                "missing": len(missing_list),
                "coverage_ratio": round(len(cov) / len(exp), 6) if exp else 1.0,
                "missing_sample": missing_list[:20],
            }
        return result

    def _fetch_batch(
        self,
        source,
        date: str,
        symbols: list[str],
        max_individual_fallback: int,
        timeout_s: float | None = None,
    ) -> list[dict]:
        import concurrent.futures as _futures

        if timeout_s is None:
            # 逐股/并行批量源（akshare stock_value_em）需要覆盖全市场时长；
            # 单调用源（tushare daily_basic）用较短超时快速失败让位兜底源。
            if callable(getattr(source, "get_batch", None)):
                timeout_s = 600.0
            else:
                timeout_s = 60.0

        params = {
            "date": date,
            "symbols": symbols,
            "max_individual_fallback": max_individual_fallback,
        }
        fetch = getattr(source, "get_batch", None)
        if callable(fetch):
            fn = lambda: fetch(self.capability, params) or []
        else:
            legacy_fetch = getattr(source, "get_daily_valuation", None)
            if callable(legacy_fetch):
                fn = lambda: legacy_fetch(date) or []
            else:
                return []
        # 逐源超时：禁止一个慢/挂源（如 tushare daily_basic 配额耗尽）
        # 阻塞整个估值管道，阻断后续可用源（akshare stock_value_em）兜底。
        with _futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                return pool.submit(fn).result(timeout=timeout_s)
            except _futures.TimeoutError:
                logger.warning(
                    f"{source.name} batch valuation timed out after {timeout_s}s, skipped"
                )
                return []

    def _fetch_individual(self, source, code: str, date: str) -> dict | None:
        value = source.get_data(self.capability, {"code": code, "date": date})
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def _record_failure(self, source_name: str, exc: Exception) -> None:
        recorder = getattr(self.source_registry, "record_failure", None)
        if callable(recorder):
            recorder(source_name, self.capability, exc)

    def run(self, input: PipelineInput) -> PipelineResult:
        repo = get_repo(input.db)
        symbols = list(dict.fromkeys(
            input.symbols if input.symbols is not None else repo.stock.get_all_active()
        ))
        expected = set(symbols)
        if not expected:
            return PipelineResult(
                name=self.name,
                date=input.date,
                status="failed",
                errors=["no active symbols for valuation collection"],
            )

        # 分母口径锚 (2026-09-25 估值断供卡定谳, 禁改回)：
        # expected 取 stock_info.is_active=1 减去结构性无覆盖代码段
        # UNCOVERED_CODE_PREFIXES，**绝不复用当日 daily_klines 表**。
        #
        # 事故 (生产实证 2026-09-16~09-24): kline 腿 tushare 限流残缺时,
        # 原实现 `expected &= set(traded)` 把分母收窄到当日 klines 表现有的
        # ~1940 只, 于是估值采到 1908 只算出 99.9% 覆盖率判绿 —— 真实覆盖率
        # 34.3%。**门禁分母被上游竞态产物污染 = 假绿**。判例「用有病的产物
        # 当基线」。
        #
        # 原实现的两个同谋一并删除, 不留旁路:
        #   ① `if traded:` 空集保护 = fail-open —— 「kline 腿彻底失败(0行)」
        #      与「真非交易日」不可区分, 且部分失败反被静默接受, 形成
        #      「全败得红、半败得绿」这个最坏搭配;
        #   ② `except: pass` —— 抛错走「不收窄」分支, 与成功路径两张脸且无留痕。
        #
        # 当日停牌/新上市 (约 16 只 ≈ 0.3pt) 不单独豁免, 由门禁余量吸收:
        # 引入动态停牌源 = 新跨源耦合, 收益 0.3pt 不值。宁可少豁免一点,
        # 也不多引一条耦合。
        #
        # 显式传入 symbols 时保持调用方口径, 不做豁免过滤。
        if input.symbols is None:
            expected = {
                code for code in expected
                if not str(code).startswith(UNCOVERED_CODE_PREFIXES)
            }
        if not self.source_registry:
            return PipelineResult(
                name=self.name,
                date=input.date,
                status="failed",
                errors=["no source_registry"],
            )

        # 门禁阈值 (乙件, 统筹 2026-09-25 裁 0.95):
        # 旧值 0.80 是「源限流时 95% 触发频繁」的妥协, 但在甲件把分母从
        # 竞态 klines 换成独立口径之后, 健康日实测覆盖率 99.6~99.7%
        # (09-14/18/22), 断供日 9.3~34.8% —— 九日回测零误判, 余量 4.6pt、
        # 差距 60pt。0.80 会放过「半残日」(如 85% 覆盖), 故收紧到 0.95。
        default_ratio = 1.0 if input.symbols is not None else 0.95
        min_coverage_ratio = float(
            input.config.get("min_coverage_ratio", default_ratio)
        )
        max_individual = int(
            input.config.get(
                "max_individual_fallback",
                len(expected) if input.symbols is not None else 200,
            )
        )
        merged: dict[str, dict] = {}
        source_counts: dict[str, int] = {}
        warnings: list[str] = []
        failures: list[str] = []
        sources = self.source_registry.get_sources_for(self.capability)

        for source in sources:
            missing = sorted(expected - set(merged))
            if not missing:
                break
            try:
                raw_records = self._fetch_batch(source, input.date, missing, max_individual)
                accepted = 0
                for raw in raw_records:
                    record = self._normalize_record(raw, input.date, expected)
                    if record is None or record["code"] in merged:
                        continue
                    merged[record["code"]] = record
                    accepted += 1
                if accepted:
                    source_counts[source.name] = source_counts.get(source.name, 0) + accepted
                elif raw_records:
                    warnings.append(
                        f"{source.name}: batch records failed schema/date/value validation"
                    )
            except Exception as exc:
                failures.append(f"{source.name} batch valuation failed: {exc}")
                self._record_failure(source.name, exc)

        missing = sorted(expected - set(merged))
        if missing and len(missing) <= max_individual:
            for code in list(missing):
                for source in self.source_registry.get_sources_for(self.capability):
                    try:
                        raw = self._fetch_individual(source, code, input.date)
                        record = self._normalize_record(raw, input.date, expected)
                        if record is None:
                            continue
                        merged[code] = record
                        source_counts[source.name] = source_counts.get(source.name, 0) + 1
                        break
                    except Exception as exc:
                        failures.append(f"{source.name}/{code}: {exc}")
                        self._record_failure(source.name, exc)
        elif missing:
            warnings.append(
                f"individual fallback skipped: {len(missing)} gaps exceed limit {max_individual}"
            )

        coverage_ratio = len(merged) / len(expected)
        missing = sorted(expected - set(merged))
        merged_set = set(merged)

        # --- Segment coverage gate ---
        segment_coverage = self._compute_segment_coverage(expected, merged_set)

        seg_min_raw = input.config.get("segment_min_coverage_ratio")
        if seg_min_raw is None:
            segment_min = {}
            for seg in segment_coverage:
                if seg == "UNKNOWN":
                    segment_min[seg] = 1.0
                elif seg == "BJ":
                    # 北交所部分证券可能缺估值源覆盖(N+2 上市公司)
                    # 试运行期间降低到 50% 以避免系统性 fail 阻塞主流程
                    segment_min[seg] = 0.5
                else:
                    segment_min[seg] = min_coverage_ratio
        elif isinstance(seg_min_raw, (int, float)):
            segment_min = {
                seg: float(seg_min_raw)
                for seg in segment_coverage
            }
        else:
            segment_min = {
                seg: float(
                    seg_min_raw.get(
                        seg, 1.0 if seg == "UNKNOWN" else min_coverage_ratio
                    )
                )
                for seg in segment_coverage
            }

        failed_segments: list[str] = []
        warn_failed = []
        # BJ 段默认严格门禁；expected 已在全市场模式下收敛到当日交易代码，
        # 结构性缺失由调用方通过 best_effort_segments 显式声明（如遇源端永久缺口）。
        best_effort_segments = set(
            input.config.get("best_effort_segments", [])
        )
        for seg, info in segment_coverage.items():
            threshold = segment_min.get(
                seg, 1.0 if seg == "UNKNOWN" else min_coverage_ratio
            )
            info["min_coverage_ratio"] = threshold
            if info["expected"] and info["coverage_ratio"] < threshold:
                if seg in best_effort_segments:
                    warn_failed.append(seg)
                    warnings.append(
                        f"segment {seg} coverage {info['coverage_ratio']:.2%} below "
                        f"threshold {threshold:.2%} (best-effort, not gating)"
                    )
                else:
                    failed_segments.append(seg)

        # 总体门禁覆盖 best-effort 段的外口径：BJ 覆盖缺口不拖垮总体 gate，
        # 但 SH/SZ 缺口照常计算。
        total_expected = len(expected)
        best_effort_missing = sum(
            info["missing"] for seg, info in segment_coverage.items()
            if seg in best_effort_segments
        )
        best_effort_expected = sum(
            info["expected"] for seg, info in segment_coverage.items()
            if seg in best_effort_segments
        )
        gated_expected = total_expected - best_effort_expected
        merged_in_gated = len(merged) - (best_effort_expected - best_effort_missing)
        gated_coverage_ratio = (
            merged_in_gated / gated_expected if gated_expected else 1.0
        )

        metadata = {
            "mode": "batch_merge_then_bounded_gap_fill",
            "capability": self.capability,
            "expected_records": len(expected),
            "coverage_ratio": round(coverage_ratio, 6),
            "gated_coverage_ratio": round(gated_coverage_ratio, 6),
            "min_coverage_ratio": min_coverage_ratio,
            "missing_count": len(missing),
            "missing_sample": missing[:20],
            "source_counts": source_counts,
            "segment_coverage": segment_coverage,
            "failed_segments": failed_segments,
            "best_effort_segments_warned": warn_failed,
            "quality_status": (
                "passed"
                if coverage_ratio >= min_coverage_ratio
                    and not failed_segments
                else "passed_best_effort_only"
                if gated_coverage_ratio >= min_coverage_ratio and not failed_segments
                else "failed"
            ),
            "dry_run": input.dry_run,
        }

        if gated_coverage_ratio < min_coverage_ratio or failed_segments:
            errors: list[str] = []
            if gated_coverage_ratio < min_coverage_ratio:
                errors.append(
                    f"valuation coverage {coverage_ratio:.2%} "
                    f"(gated {gated_coverage_ratio:.2%}) below gate {min_coverage_ratio:.2%}"
                )
            for seg in failed_segments:
                info = segment_coverage[seg]
                errors.append(
                    f"segment {seg} coverage {info['coverage_ratio']:.2%} below "
                    f"threshold {info['min_coverage_ratio']:.2%} "
                    f"({info['covered']}/{info['expected']})"
                )
            errors.extend(failures[:20])
            return PipelineResult(
                name=self.name,
                date=input.date,
                status="failed",
                records_read=len(merged),
                records_written=0,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        records = [merged[code] for code in sorted(merged)]
        if not input.dry_run:
            repo.valuation.batch_upsert(records)
        return PipelineResult(
            name=self.name,
            date=input.date,
            status="success",
            records_read=len(records),
            records_written=0 if input.dry_run else len(records),
            errors=[],
            warnings=warnings + failures[:20],
            metadata=metadata,
        )
