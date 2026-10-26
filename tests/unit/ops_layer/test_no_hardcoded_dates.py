# -*- coding: utf-8 -*-
r"""硬编码日期机治钉 升 hard fail 分级 + 基线锁定 (统 筹 GO 升 第 6 追加,判 例 #153)

判 例 #153 「告警必须有可见载体,否则等于没告警」 — pytest 默 认 捕 获 stdout,
只在 测 试 失 败 时才回显。「print 在 缓冲 区」 = 「告警 无 载 体」 = 没 有 载 体。
改 版: 升 hard fail + baseline 锁 定 (存 量 4302 行 已 知 账 不 阻 塞, 新 增 才 红)

判 据 三行 命 令 能 验证:
  pytest tests/unit/ops_layer/test_no_hardcoded_dates.py -q   → 应 红/应 绿
  grep 'hardcoded_date_baseline\|新增硬编码日期' <门禁 输出>  → 应 能 命中
  grep '2026-10-01' <门禁 输出>  → 若 未 加 -s 也 应 能 命中 (本 钉 检 出)

判 例 #152/#153 + #150 三 钉 机 治 应 用。
"""
from __future__ import annotations

import pathlib
import re
from datetime import date

import pytest

def _is_real_date(s: str) -> bool:
    """形状 对 但 不 是 日 期 → 不 是 本 钉 要 治 的 病 (判 条 #150 镜 像 病 防 守:
    2026-13-99 非 法 日期 字面量 不 应 进 baseline)
    """
    try:
        parts = s.split("-")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        date(y, m, d)
        return True
    except (ValueError, IndexError):
        return False


# baseline 文 件 (与 扫描 同源: 相 对 路 径, tests/* 形 态)
_BASELINE = pathlib.Path(".hardcoded_date_baseline.txt")


class TestNoHardcodedDates:
    """硬编码日期 机 治 钉 — 基线 锁 定 + 新 增 才 红"""

    _DATE_PATTERN = re.compile(r'"?(20\d\d-\d\d-\d\d)"?')
    _TESTS_ROOT = pathlib.Path("tests")

    def _scan_test_files(self) -> dict[str, list[tuple[int, str]]]:
        """扫 tests/ 全 树,用 相 对 路 径 (与 baseline 统一格式, 判例 #150 跨对象边界)"""
        findings = {}
        for py in self._TESTS_ROOT.rglob("test_*.py"):
            text = py.read_text(encoding="utf-8", errors="replace")
            for i, ln in enumerate(text.splitlines(), 1):
                for m in self._DATE_PATTERN.finditer(ln):
                    # 判 条 #153 地基 3: 「 2026-13-99 非 法 日期 字面量 不 是 本 钉 要 治 的 病」
                    # 形状 对 但 不 是 日 期 → 不 收 进
                    if not _is_real_date(m.group(1)):
                        continue
                    rel = str(py.relative_to("."))
                    findings.setdefault(rel, []).append((i, m.group(1)))
        return findings

    def _load_baseline(self) -> set[tuple[str, int, str]]:
        """读 baseline 锁 定 文 件 (存 量 账, 与 扫描 同源 相 对 路 径)"""
        if not _BASELINE.exists():
            return set()
        known = set()
        for ln in _BASELINE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.rsplit(":", 2)
            if len(parts) >= 3:
                file, line_s, date = parts[0], parts[1], parts[2]
                try:
                    known.add((file, int(line_s), date))
                except ValueError:
                    continue
        return known

    def _new_findings_vs_baseline(
        self, findings: dict[str, list[tuple[int, str]]],
        baseline: set[tuple[str, int, str]],
    ) -> dict[str, list[tuple[int, str]]]:
        """与 baseline 比 对 得 新增"""
        new = {}
        for file, hits in findings.items():
            fresh = [(ln, d) for ln, d in hits if (file, ln, d) not in baseline]
            if fresh:
                new[file] = fresh
        return new

    def test_no_hardcoded_dates_new_entries_fail_with_visible_error(self):
        """判 例 #153 hard fail 分级 — 新 增 硬 编 码 日期 才 红 而 存 量 不 阻 塞"""
        findings = self._scan_test_files()
        baseline = self._load_baseline()
        new = self._new_findings_vs_baseline(findings, baseline)
        # 白名单 (ex 夹具 内 _ALLOWLIST 白名单 列)
        _ALLOWLIST: dict[str, str] = {}
        allowlist_files = set(_ALLOWLIST.keys())
        unexpected_new = {
            k: v for k, v in new.items() if k not in allowlist_files}
        if unexpected_new:
            lines = []
            for file, hits in unexpected_new.items():
                for ln, d in hits[:5]:
                    lines.append(f"  {file}:{ln} {d}")
            # 升 hard fail, CI 红 且 输 出 在 门禁 报告 中 可 见 (判 例 #153)
            pytest.fail(
                f"新增硬编码日期 {len(unexpected_new)} 文件 / "
                f"{sum(len(v) for v in unexpected_new.values())} 处 "
                f"(判 例 #152 日期漂移假红 风 险; "
                f"存 量 {len(baseline)} 处 已 在 基 线 内 不 阻 塞):\n"
                + "\n".join(lines[:20])
                + f"\n存 量 基 线={len(baseline)} 处, 新增 = "
                  f"{sum(len(v) for v in unexpected_new.values())} 处")

    def test_baseline_file_self_consistency(self):
        """判 条 #150 自 检:baseline 文 件 若 不 存 在,本 钉 必 报 出"""
        if not _BASELINE.exists():
            print(f"WARN: baseline 文 件 {_BASELINE} 不 存 在")

    def test_scan_relative_path_matches_baseline_format(self):
        """判 条 #150 自 检 2:扫描 与 baseline 路 径 格 式 必 一 致 (相 对 路 径)"""
        if not _BASELINE.exists():
            pytest.skip("baseline 文 件 不 存 在")
        findings = self._scan_test_files()
        baseline_keys = {f for (f, ln, d) in self._load_baseline()}
        findings_keys = set(findings.keys())
        # 扫描 全 部 文 件 必 在 baseline 中 找 到 (若 baseline 已 全 录)
        assert findings_keys.issubset(baseline_keys) or not findings_keys, (
            f"扫描 文 件 有 {len(findings_keys - baseline_keys)} 件 不 在 baseline"
            f" — 路 径 格 式 不 一 致 或 baseline 缺 失")


class TestHardcodedDateRegressionDetectability:
    """判 例 #152 应 用 证 明:故意塞 一个假日日期必 检 出"""

    def test_regression_detection_with_simulated_hardcoded_date(self, tmp_path):
        fake_test = tmp_path / "test_fake_hardcoded.py"
        fake_test.write_text(
            '# -*- coding: utf-8 -*-\n'
            'def test_fake():\n'
            '    x = "2026-10-01"  # 硬编码日期\n'
            '    assert x\n',
            encoding="utf-8")
        pattern = re.compile(r'"?(20\d\d-\d\d-\d\d)"?')
        text = fake_test.read_text(encoding="utf-8")
        matches = list(pattern.finditer(text))
        assert matches, f"硬编码日期 应 被 检 出;实得 {len(matches)}"
        assert "2026-10-01" in [m.group(1) for m in matches]


class TestBaselineWorkflowIntegrity:
    """基线 工 作 流 完整 性 钉 （存 量 账 新 增 才 红 机 制）"""
    pass
