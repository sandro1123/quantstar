"""data_layer/sources/coverage.py — 数据源能力矩阵与缺口判据 (2026-11)

立此模块的由头 (对标 ZhuLinsen/daily_stock_analysis):
DSA 用 `docs/market-support.md` 讲清「哪个市场、哪个源、能拿什么数据」。那份表有用,
但它是文档 —— 人会忘了改, 改了代码不会报错。本项目把它变成**代码判据**:
每个已声明数据类型有几条腿、哪条腿此刻不可用、哪些类型是无对冲单点,
一条命令全出 (`qs data sources`)。

判据不造第二套清单 (与 DL-30 catalog 同一原则):
- 全部数据类型        = protocol.DATA_TYPES (协议唯一事实源)
- 源侧能力            = DataSource.capabilities
- 可用性              = DataSource.is_available() (经 registry 过滤)

四态与 docs/ARCHITECTURE.md 1.1 宪法#5「单点必有对冲」对齐:
- ok        : >= 2 个可用源 —— 单源故障不影响全盘
- single    : 恰 1 个可用源 —— 能跑, 但宪法#5 未被满足 (应视为技术债)
- degraded  : 有源声明该能力, 当前 0 个可用 (凭证失效/熔断/网络)
- gap       : 没有任何已注册源声明该能力 (能力缺口, 配了也用不了)

只读模块: 不发网络请求以外的副作用, 不写库, 不改动现有方法体。
`is_available()` 是各源自实现的连通性判断, 可能是廉价的 key 检查,
也可能发一次 HTTP —— 调用频率由调用方控制 (CLI 手动 / health 巡检)。
"""

from __future__ import annotations

from common.logging import get_logger
from common.utils import bj_now
from data_layer.sources.protocol import DATA_TYPES

logger = get_logger("data.source_coverage")

STATUS_OK = "ok"
STATUS_SINGLE = "single"
STATUS_DEGRADED = "degraded"
STATUS_GAP = "gap"

# 严重度排序: 缺口最重, ok 最轻 — 报告按此升序排 (先看坏消息)
_SEVERITY = {STATUS_GAP: 0, STATUS_DEGRADED: 1, STATUS_SINGLE: 2, STATUS_OK: 3}


def _is_available(source) -> bool:
    """源的可用性判断; 抛异常一律按不可用处理 (fail-visible, 不静默当可用)。"""
    try:
        return bool(source.is_available())
    except Exception as e:  # noqa: BLE001 — 单源探测失败不得中断整张矩阵
        logger.warning(f"source {getattr(source, 'name', '?')} is_available() 抛异常: {e}")
        return False


def coverage_report(registry) -> dict:
    """生成能力矩阵。

    Args:
        registry: SourceRegistry (或任何实现 get_all_sources() 的对象)

    Returns:
        {
          "generated_at": ..., "total_types": int,
          "summary": {"ok": n, "single": n, "degraded": n, "gap": n},
          "types": [{data_type, label, status, registered, available, unavailable}],
          "gaps": [...], "singles": [...], "degraded": [...],
          "unhealthy_sources": [...],
        }
    """
    sources = list(registry.get_all_sources())
    # 一次探测, 全部类型复用 — 避免同一源被 is_available() 打 N 次
    availability = {s.name: _is_available(s) for s in sources}

    entries: list[dict] = []
    summary = {STATUS_OK: 0, STATUS_SINGLE: 0, STATUS_DEGRADED: 0, STATUS_GAP: 0}

    for data_type, label in sorted(DATA_TYPES.items()):
        registered = [s.name for s in sources if data_type in getattr(s, "capabilities", set())]
        available = [n for n in registered if availability.get(n)]
        unavailable = [n for n in registered if not availability.get(n)]

        if not registered:
            status = STATUS_GAP
        elif not available:
            status = STATUS_DEGRADED
        elif len(available) == 1:
            status = STATUS_SINGLE
        else:
            status = STATUS_OK

        summary[status] += 1
        entries.append({
            "data_type": data_type,
            "label": label,
            "status": status,
            "registered": len(registered),
            "available": available,
            "unavailable": unavailable,
        })

    entries.sort(key=lambda e: (_SEVERITY[e["status"]], e["data_type"]))

    def _names(status: str) -> list[str]:
        return [e["data_type"] for e in entries if e["status"] == status]

    return {
        "generated_at": bj_now(),
        "total_types": len(entries),
        "summary": summary,
        "types": entries,
        "gaps": _names(STATUS_GAP),
        "degraded": _names(STATUS_DEGRADED),
        "singles": _names(STATUS_SINGLE),
        "unhealthy_sources": sorted(n for n, ok in availability.items() if not ok),
    }


def gaps(registry, required: list[str] | None = None) -> list[str]:
    """返回不可用 (gap + degraded) 的数据类型清单。

    required: 只关心这些类型 (如某次部署实际要用的 kline/valuation/financial)。
              传了未登记的类型名也算缺口 —— 拼错的名字不该静默通过
              (与 protocol.DATA_TYPES 对照, 不另造词表)。
    """
    report = coverage_report(registry)
    bad = set(report["gaps"]) | set(report["degraded"])
    if required is None:
        return sorted(bad)
    unknown = {t for t in required if t not in DATA_TYPES}
    return sorted((set(required) & bad) | unknown)


def assert_coverage(registry, required: list[str]) -> list[str]:
    """供启动自检 / 部署门禁使用: 返回缺口清单, 空列表 = 通过。

    只出判据不抛异常 —— 是否致命由调用方决定
    (启动可 warn, 部署门禁可 fail; 判据与处置分离)。
    """
    return gaps(registry, required)
