"""DL-67: 所有 kline 适配器复权参数一致且为未复权 — 静态扫描硬闸, 防回归

DL-19 定论: 库内 daily_klines.close 为未复权 raw 价 (送转判据 48/48)。
bulk 主路径 (tushare get_daily_market 不传 adj / baostock adjustflag='3') 已是 raw,
但三个 per-symbol 回退适配器曾残留 adj="qfq"/adjust="qfq" — 一旦 bulk 失败回退,
qfq 价会贴 'raw' 标签写库 (写侧 repos/kline.py 硬编码 'raw')。

本测试遍历 sources/ 下全部 *_source.py (不硬编码清单, 新增适配器自动被覆盖),
扫描任何 kline 拉取调用里的前复权参数, 命中即失败。

判据 (任一命中 = 违规):
- adj="qfq" / adj='qfq' / adj = "qfq"      (tushare)
- adjust="qfq" / adjust='qfq'              (akshare 系)
- adjustflag="2" / adjustflag='2'          (baostock 2=前复权)
允许: 不传参 (默认不复权) / adjust="" / adjustflag="3" (不复权)。
"""

import os
import re

SOURCES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "src", "data_layer", "sources",
)

# 前复权参数模式 (违规)。不复权/缺省不匹配。
QFQ_PATTERNS = [
    re.compile(r"""adj\s*=\s*["']qfq["']"""),
    re.compile(r"""adjust\s*=\s*["']qfq["']"""),
    re.compile(r"""adjustflag\s*=\s*["']2["']"""),
]


def _source_files():
    return sorted(
        f for f in os.listdir(SOURCES_DIR)
        if f.endswith("_source.py")
    )


def test_sources_dir_found():
    assert os.path.isdir(SOURCES_DIR), f"sources dir missing: {SOURCES_DIR}"
    assert _source_files(), "no *_source.py found — 扫描范围异常"


def test_no_qfq_adjust_in_any_kline_adapter():
    """遍历全部适配器, 任何前复权参数都不得出现 (口径必须统一为未复权)。"""
    violations = []
    for fname in _source_files():
        path = os.path.join(SOURCES_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            in_docstring = False
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                # 跳注释行与 docstring 内容: 历史说明不应触发违规 (Q8④b)
                if stripped.startswith("#"):
                    continue
                # 跟踪三引号 docstring 状态 (奇数个三引号=进出切换)
                for q in ('"""', "'''"):
                    if stripped.count(q) % 2 == 1:
                        in_docstring = not in_docstring
                if in_docstring:
                    continue
                for pat in QFQ_PATTERNS:
                    if pat.search(line):
                        violations.append(f"{fname}:{lineno}: {line.strip()}")
    assert not violations, (
        "发现前复权参数 (违反 DL-67 口径统一, 库内为 raw):\n" + "\n".join(violations)
    )


def test_known_adapters_present_and_clean():
    """点名三个曾残留 qfq 的适配器, 确保它们仍在被扫描范围内 (防文件改名漏扫)。"""
    files = set(_source_files())
    for expected in ("tushare_source.py", "akshare_source.py", "akshare_sina_source.py"):
        assert expected in files, f"{expected} 不在 sources/ — 可能改名导致漏扫"
