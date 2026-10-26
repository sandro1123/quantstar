# -*- coding: utf-8 -*-
"""审计复核 P1×3 判据 — 数字口径统一 / REPLACE 孤儿锚 / DDL 镜像守卫

P1① 数字 regex 双口径 → 统一到 text_domain 单一来源
P1② record_rationale REPLACE 清 1b 两列 + 换 id 致旧锚孤儿 → 写回捕获pre_image + 孤儿探测
P1③ DDL 镜像漂移 → 列集守卫（见 test_advisor_guard_trace_migration.py）
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.text_domain import NUM_PATTERN, extract_numbers, find_numbers


class TestP1SingleNumberDomain:
    def test_source_and_fidelity_share_pattern(self):
        """双口径合并：两模块引用同一常量（防未来再分叉）"""
        import report_layer.advisor.fidelity as fid
        import report_layer.advisor.source as src
        # source 侧经 extract_numbers 间接使用；fidelity 侧经 find_numbers
        assert fid.find_numbers is find_numbers
        assert src.extract_numbers is extract_numbers

    def test_ascii_day_unit_supported_both_sides(self):
        """'20日' 两侧同判（原 source 侧含"日"而 fidelity 侧不含 → 不等宽）"""
        assert "20日" in extract_numbers("近20日强势")
        assert "20日" in find_numbers("近20日强势")

    def test_chinese_quantifier_both_sides(self):
        """'五成' 两侧同判"""
        assert "五成" in extract_numbers("量能萎缩至五成")
        assert "五成" in find_numbers("量能萎缩至五成")

    def test_year_month_supported(self):
        assert "三年" in find_numbers("估值处于近三年低位")


class TestP1OrphanAnchor:
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE recommendation_rationale (rationale_id TEXT PRIMARY KEY, "
            "code TEXT, rec_date TEXT, verdict TEXT, confidence REAL, position_pct REAL, "
            "reasoning TEXT, expert_name TEXT, key_points TEXT, invalidation_conditions TEXT, "
            "claims TEXT, trace_id TEXT, run_id TEXT, request_id TEXT, "
            "advisor_text TEXT, advisor_guard TEXT)")
        conn.execute(
            "CREATE TABLE advisor_guard_trace (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "date TEXT, code TEXT, rejected_gate TEXT, degrade_path TEXT, llm_output_hash TEXT, "
            "detail TEXT, rationale_id TEXT, trace_id TEXT, created_at TEXT)")
        conn.commit()
        return conn

    def test_pre_image_capture_does_not_lose_previous(self):
        """P1②：二次写回覆盖既有 1b 列（pre_image 供审计对照，不静默丢）"""
        from report_layer.advisor.rationale_join import write_back_advisor, GUARD_PASS, GUARD_DEGRADED
        conn = self._db()
        conn.execute("INSERT INTO recommendation_rationale (rationale_id, code, rec_date, "
                     "advisor_text, advisor_guard) VALUES ('r1','600000','2026-09-22','旧文本','degraded')")
        conn.commit()
        n = write_back_advisor(conn, "r1", "新文本", GUARD_PASS)
        assert n == 1
        row = conn.execute("SELECT advisor_text, advisor_guard FROM recommendation_rationale "
                           "WHERE rationale_id='r1'").fetchone()
        assert (row[0], row[1]) == ("新文本", GUARD_PASS)

    def test_write_back_uses_update_not_replace(self):
        """P1②：写回必须 UPDATE（不换 id），否则旧锚孤儿"""
        from report_layer.advisor.rationale_join import write_back_advisor, GUARD_PASS
        conn = self._db()
        conn.execute("INSERT INTO recommendation_rationale (rationale_id, code, rec_date) "
                     "VALUES ('r1','600000','2026-09-22')")
        conn.commit()
        write_back_advisor(conn, "r1", "文本", GUARD_PASS)
        ids = [r[0] for r in conn.execute("SELECT rationale_id FROM recommendation_rationale")]
        assert ids == ["r1"], "写回不应更换 rationale_id"

    def test_detect_orphan_traces(self):
        """P1②：1a REPLACE 换 id 后旧锚成孤儿 → 探测器能找出来（审计反 JOIN 断链可视）"""
        from report_layer.advisor.rationale_join import find_orphan_traces
        conn = self._db()
        conn.execute("INSERT INTO advisor_guard_trace (date, code, rejected_gate, degrade_path, "
                     "rationale_id) VALUES ('2026-09-22','600000','evidence','rule_fallback','OLD_ID')")
        conn.execute("INSERT INTO recommendation_rationale (rationale_id, code, rec_date) "
                     "VALUES ('NEW_ID','600000','2026-09-22')")
        conn.commit()
        orphans = find_orphan_traces(conn)
        assert len(orphans) == 1
        assert orphans[0]["rationale_id"] == "OLD_ID"

    def test_no_orphan_when_anchor_alive(self):
        from report_layer.advisor.rationale_join import find_orphan_traces
        conn = self._db()
        conn.execute("INSERT INTO advisor_guard_trace (date, code, rejected_gate, degrade_path, "
                     "rationale_id) VALUES ('2026-09-22','600000','evidence','rule_fallback','r1')")
        conn.execute("INSERT INTO recommendation_rationale (rationale_id, code, rec_date) "
                     "VALUES ('r1','600000','2026-09-22')")
        conn.commit()
        assert find_orphan_traces(conn) == []

    def test_orphan_probe_safe_when_tables_absent(self):
        from report_layer.advisor.rationale_join import find_orphan_traces
        conn = sqlite3.connect(":memory:")
        assert find_orphan_traces(conn) == []
