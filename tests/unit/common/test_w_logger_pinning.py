"""#121 get_logger 钉死微卡 — RED (签名断言型).

病理 (普查 z_logger_census1~4, 2026-09-03, 只读):
    common/logging.py get_logger 对 NOTSET logger 即刻 setLevel(root 当前级)。
    全仓 178 处 get_logger (166 模块级) 在 import 期执行, import 早于首次
    setup_logging → root=WARNING → logger 永久钉死 WARNING, 此后 root→INFO
    不跟随 (#80 ops.orchestrator INFO 149→0 面的机制定案; 群钉实测 158 个)。

案A (设计稿 f77580fb, 统筹 GO sm-33ed791b/sm-ddd77042a):
    删钉死两行, NOTSET 走 stdlib 原生继承 (getEffectiveLevel 沿祖先链实时查 root)。

全局态卫生 (统筹 sm-ddd77042a 注记①, 必做):
    logging 是进程级全局态 — 每测前 logging.root.setLevel(WARNING) 复位
    (上测可能遗留 DEBUG/INFO, 不复位则钉级病征签名被污染); 受测 logger 名用
    z121 专有前缀; 测后从 logging.Logger.manager.loggerDict 前缀清扫 purge
    (钉死状态挂在 logger 实例上, 不 purge 则同名复用吃上测残留)。另对本测
    动过的四处全局 (root 级 / root handlers / common.logging._configured /
    loggerClass) 快照恢复 — 本卡夹具不得成为新的全局泄漏源。
"""
import logging

import pytest

from common.logging import QuantStarLogger, get_logger, setup_logging

_Z121 = "z121"  # 专有名前缀 — 全卡 logger 名空间


@pytest.fixture(autouse=True)
def _log_global_hygiene():
    """注记① 全局态卫生: 测前复位 + 测后 purge/快照恢复。"""
    from common import logging as qs_logging

    root = logging.getLogger()
    snap_level = root.level
    snap_handlers = list(root.handlers)
    snap_configured = qs_logging._configured
    snap_cls = logging.Logger.manager.loggerClass

    root.setLevel(logging.WARNING)  # Python 默认态, 生产 import 期真起点
    yield

    mgr = logging.Logger.manager
    for name in [n for n in list(mgr.loggerDict) if n.startswith(_Z121)]:
        mgr.loggerDict.pop(name, None)
    root.handlers[:] = snap_handlers
    root.setLevel(snap_level)
    qs_logging._configured = snap_configured
    logging.Logger.manager.loggerClass = snap_cls  # None=stdlib 默认态, setLoggerClass(None) 会炸


def test_get_logger_before_setup_not_pinned_follows_root_later():
    """RED-1 生产序治愈 (核心): import 期取 logger → setup 后须 NOTSET 且 eff 跟随.

    生产时序复刻: runner import 链上模块级 get_logger 全部先于 bootstrap 内
    setup_logging → 此刻 root=Python 默认 WARNING。当前代码病征: import 期即
    钉 WARNING(30), setup_logging(level=INFO) 后 eff 冻结 30 不随 root→INFO
    (#80 ops.orchestrator 149→0 INFO 面即此形态)。
    """
    lg = get_logger(f"{_Z121}.seq.ops_orchestrator")  # import 期形态 (root=WARNING)
    setup_logging("test", level=logging.INFO)         # bootstrap main 内形态
    assert lg.level == logging.NOTSET, (
        f"setup 前 get_logger 不得钉级 (须 NOTSET 走原生继承) — 实得 level={lg.level}"
    )
    assert lg.getEffectiveLevel() == logging.INFO, (
        f"setup 后 eff 须跟随 root=INFO — 实得 {lg.getEffectiveLevel()} (冻结即病)"
    )


def test_get_logger_after_setup_also_notset():
    """RED-2 setup 后新建 logger 同语义: 须 NOTSET (现钉 root 当前级 INFO=20)."""
    setup_logging("test", level=logging.INFO)
    lg = get_logger(f"{_Z121}.later.module")
    assert lg.level == logging.NOTSET, (
        f"setup 后新建 logger 亦不得钉级 — 实得 level={lg.level} (==INFO 即钉死)"
    )
    assert lg.getEffectiveLevel() == logging.INFO


def test_get_logger_contract_preserved():
    """RED-3 既有契约回归守卫 (绿守卫): __class__ 强转+layer/trace_id 不被误伤."""
    lg = get_logger(f"{_Z121}.guard.layer", trace_id="t-121")
    assert isinstance(lg, QuantStarLogger)
    assert lg.layer == f"{_Z121}.guard.layer"
    assert lg.trace_id == "t-121"


def test_effective_level_follows_root_changes():
    """RED-4 root 再变实时跟随: 继承是活的非快照 (现钉死后冻结).

    当前代码: setup(INFO) 后 get_logger 钉 INFO(20), root 改 WARNING 后
    eff 仍 20 — 快照语义; 原生继承须实时沿祖先链查 root。
    """
    setup_logging("test", level=logging.INFO)
    lg = get_logger(f"{_Z121}.follow.module")
    assert lg.getEffectiveLevel() == logging.INFO  # 起点对齐
    logging.getLogger().setLevel(logging.WARNING)  # root 再变
    assert lg.getEffectiveLevel() == logging.WARNING, (
        f"NOTSET 继承须实时跟随 root (活的非快照) — 实得 {lg.getEffectiveLevel()}"
    )
