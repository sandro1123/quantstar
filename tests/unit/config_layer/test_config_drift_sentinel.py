"""P0-7 配置漂移哨兵单元测试 — deep_merge / compute_diff / 漂移检出。

回归锚: deepdiff 9.x 传 verbose_level=0 会吞掉 values_changed (实测漏检
布尔翻转), compute_diff 不得传该参数。
"""
import json
import sys
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from config_drift_sentinel import compute_diff, deep_merge, load_defaults_merged


class TestDeepMerge:
    def test_scalar_overlay_wins(self):
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_dict_deep_merge(self):
        base = {"execution": {"dry_run": True, "max_positions": 10}}
        overlay = {"execution": {"dry_run": False}}
        merged = deep_merge(base, overlay)
        assert merged["execution"]["dry_run"] is False
        assert merged["execution"]["max_positions"] == 10  # 未覆盖键保留

    def test_list_overlay_replaces(self):
        assert deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    def test_new_key_added(self):
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


class TestComputeDiff:
    def test_identical_returns_empty(self):
        assert compute_diff({"a": 1}, {"a": 1}) == {}

    def test_detects_boolean_flip(self):
        """回归: verbose_level=0 曾吞掉此差异"""
        diff = compute_diff({"execution": {"dry_run": True}}, {"execution": {"dry_run": False}})
        assert diff, "布尔翻转必须被检出"
        assert "values_changed" in diff

    def test_detects_value_change(self):
        diff = compute_diff({"a": 1}, {"a": 2})
        assert "values_changed" in diff

    def test_detects_added_key(self):
        diff = compute_diff({"a": 1}, {"a": 1, "b": 2})
        assert "dictionary_item_added" in diff

    def test_detects_removed_key(self):
        diff = compute_diff({"a": 1, "b": 2}, {"a": 1})
        assert "dictionary_item_removed" in diff

    def test_list_reorder_tolerated(self):
        """ignore_order=True: 列表重排不算漂移 (depends_on 顺序无业务意义)"""
        diff = compute_diff({"deps": ["a", "b", "c"]}, {"deps": ["c", "b", "a"]})
        assert diff == {}

    def test_diff_is_json_serializable(self):
        diff = compute_diff({"a": 1}, {"a": 2})
        json.dumps(diff)  # 不抛异常


class TestLoadDefaultsMerged:
    def test_merges_all_default_files(self):
        merged = load_defaults_merged()
        # 至少应含 execution / ops / strategy 三个 section
        assert "execution" in merged
        assert "ops" in merged
        assert "strategy" in merged

    def test_execution_has_dry_run(self):
        merged = load_defaults_merged()
        assert "dry_run" in merged["execution"]
