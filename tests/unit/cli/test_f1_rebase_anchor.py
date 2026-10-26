"""F1 改基小修卡测试 — 哨兵文档/正文锚日勘正 (08-29→08-26)。

改基裁定 (统筹 sm-5ab96e9e15aa4123): F2 锚日=2026-08-26 (fwd 回补批 apply 完成日,
jsonl run_finished 08-26 00:31:17 物证), stable_window_end="2026-09-09" (锚+14天)。
GO-9 卷宗「08-29 回补批完成日」=窗口包日期误标为批完成日 — ops.yaml 注释与
issues.py detail_zh 的 08-29 措辞即该旧锚产物, 本卡勘正为 08-26。
T2 比较器逻辑锚日无关 (零改动), 只勘正文档面。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPS_YAML = _REPO_ROOT / "config" / "defaults" / "ops.yaml"
_ISSUES = _REPO_ROOT / "src" / "cli" / "issues.py"


class TestF1RebaseAnchor:
    """ops.yaml purged_cv_upgrade 段注释: 锚=08-26 回补批完成日, 窗末=09-09."""

    def test_ops_yaml_anchor_comment_uses_0826_batch_completion(self):
        body = _OPS_YAML.read_text(encoding="utf-8")
        seg = _segment(body)
        assert "回补批完成日 08-26" in seg, (
            "ops.yaml purged_cv_upgrade 段注释须写明锚=08-26 回补批完成日 "
            "(改基裁定 sm-5ab96e9e15aa4123), 段现状:\n" + seg
        )

    def test_ops_yaml_window_end_value_not_old_anchor(self):
        seg = _segment(_OPS_YAML.read_text(encoding="utf-8"))
        # 值行不得写旧锚; 记档行 (勘正说明引旧锚) 允许 — 记档必须能提旧锚才叫记档
        value_lines = [l for l in seg.splitlines()
                       if l.strip() and not l.lstrip().startswith("#")]
        assert not any("08-29" in l for l in value_lines), (
            f"段值行不得含旧锚 08-29 (窗口包日期误标), 值行: {value_lines}"
        )

    def test_ops_yaml_window_end_is_anchor_plus_14(self):
        seg = _segment(_OPS_YAML.read_text(encoding="utf-8"))
        m = re.search(r'stable_window_end:\s*"2026-09-09"', seg)
        assert m, (
            "值=锚日 08-26 + 14 天的推导值, 引号串形态 (统筹 8bc5aac config "
            "commit 亲落; 引号免 yaml 解析成 date 型, _purgedcv_cfg str 直通); "
            f"段现状:\n{seg}"
        )
        assert "锚日 08-26 + 14 天 = 2026-09-09" in seg, (
            f"窗末须在段注释内明示推导 (锚+14=09-09):\n{seg}"
        )


class TestF1RebaseAnchorDetail:
    """issues.py 哨兵正文 detail_zh: T2 触发时携带改基勘正说明."""

    def test_issues_detail_zh_mentions_rebase_anchor_0826(self):
        body = _ISSUES.read_text(encoding="utf-8")
        assert "回补批完成日 08-26" in body, (
            "issues.py 哨兵正文须含改基说明 (旧跃升锚 08-29 已证伪: GO-9 销案 + "
            "回补批吃尽可填史, 实填 7,870,684 cells vs 残NULL估算≈15,551,557), 现: "
            f"{[l for l in body.splitlines() if 'stable_window_end' in l][:3]}"
        )


def _segment(body: str) -> str:
    """ops.yaml 里 purged_cv_upgrade 顶层段, 含段首上方的连续注释块。

    段注释约定在键上方 (yaml 惯例), 只从段首键行起切会把锚日推导整块切掉。
    """
    start = body.find("purged_cv_upgrade:")
    assert start != -1, "ops.yaml 缺 purged_cv_upgrade 顶层段"
    head = body.rfind("\n\n", 0, start)
    seg_start = head + 1 if head != -1 else 0
    nxt = body.find("\n\n", start)
    return body[seg_start:nxt if nxt != -1 else len(body)]
