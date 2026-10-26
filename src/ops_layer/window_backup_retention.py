# -*- coding: utf-8 -*-
"""窗口备份保留策略 — qs_prewindow_* 命名白名单（统筹 09-25 批·GO）

GFS backup_retention（W2卡⑤）的姊妹模块：GFS 管 data/backups 日备，
本模块管 /tmp 窗口备份（统筹并窗前的 prewindow 安全网）。

病根（09-24 亲历）：只写不删，4 份 16G 副本堆到 85% 磁盘。
触发（统筹裁定 α）：CLI 手动，**默认 dry-run**，--yes 才真删
（删除是破坏性动作须显式授权——铁律「备份的意义是事故时可回退」）。

三钉（沿用 backup_retention 08-21 血案判例模式）：
  1. 白名单式删除：只删「qs_prewindow_YYYYMMDD(.bundle|.db*)」命名且不在保留集。
     **非 prewindow 命名零删除**——qs-bundle-backup-20260918.bundle 类永久保险
     天然豁免（锚是命名，无需硬编码白名单）。
  2. 删前断言保留集非空且含最新一份（防全删）。
  3. 幂等 + 留痕（每删一份 logger.info；CLI 输出结构化清单）。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("ops.window_backup_retention")

# 受管命名锚：qs_prewindow_YYYYMMDD + 可选后缀（.bundle / .db / .db-wal / .db-shm）
PREWINDOW_RE = re.compile(r"^qs_prewindow_(\d{4})(\d{2})(\d{2})(\.bundle|\.db|\.db-wal|\.db-shm)$")

# 保留策略（统筹锚点）：bundle 跨窗回退保险 2 份；db 副本窗口成功后前一份即废 1 份
BUNDLE_KEEP = 2
DB_KEEP = 1


def parse_prewindow_name(path: str):
    """从文件名解析 prewindow 日期；非受管命名返回 None（白名单外，永不删）。"""
    m = PREWINDOW_RE.match(os.path.basename(path or ""))
    if not m:
        return None
    from datetime import date
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None  # 语义非法日期 = 白名单外，不判不删（backup_retention 同款）


def keep_set(files: list[str]) -> set[str]:
    """保留集（纯函数）：bundle 最近 2 份 / db 族最近 1 份（-wal/-shm 随主 db 同命）；
    白名单外文件无条件保留。"""
    bundles: list[tuple[object, str]] = []
    dbs: list[tuple[object, str]] = []
    others: set[str] = set()
    # db 附属件（-wal/-shm）随主文件：先分拣主件，再按日期映射附属件
    wal_shm: list[str] = []
    for f in files:
        base = os.path.basename(f or "")
        d = parse_prewindow_name(f)
        if d is None:
            others.add(f)
            continue
        if base.endswith(".bundle"):
            bundles.append((d, f))
        elif base.endswith(".db"):
            dbs.append((d, f))
        else:  # .db-wal / .db-shm
            wal_shm.append((d, f))

    keep: set[str] = set(others)
    for group, n in ((bundles, BUNDLE_KEEP), (dbs, DB_KEEP)):
        if group:
            group.sort(key=lambda x: x[0], reverse=True)
            keep.update(f for _, f in group[:n])
    # 附属件随主：主在保留集（同日期）则留，主不在则随删
    kept_dates = {parse_prewindow_name(f) for f in keep if parse_prewindow_name(f)}
    for d, f in wal_shm:
        if d in kept_dates:
            keep.add(f)
        else:
            keep.discard(f)  # 显式从 keep 排除（ prune 时按 keep 差集删，此行防呆）
    # 判定附属件去留：不在保留日期 → 不进 keep（即被删）
    for d, f in wal_shm:
        if d not in kept_dates:
            keep.discard(f)
    return keep


def prune_window_backups(directory: str, yes: bool = False) -> dict:
    """对目录执行 prewindow 轮转。默认 dry-run（yes=False）只报不删。

    返回结构化清单：{dry_run, would_delete, deleted, kept, protected}。
    三钉#2：受管文件全被判定为「保留」时零删除（宁可不删不可误删）。
    """
    files = sorted(
        os.path.join(directory, n) for n in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, n))
    )
    keep = keep_set(files)
    managed = [f for f in files if parse_prewindow_name(f) is not None]
    # 受管但语义非法日期（parse 返回 None 但 PREWINDOW_RE 能匹配）——上面 parse 已
    # 把非法日期归 None=白名单外，天然进 keep，无歧义。
    would_delete = [f for f in managed if f not in keep]
    protected = [f for f in files if f not in managed]

    result = {
        "dry_run": not yes,
        "would_delete": would_delete,
        "deleted": [],
        "kept": [f for f in managed if f in keep],
        "protected": protected,
    }
    if not yes:
        logger.info(f"window_retention dry-run: would_delete={len(would_delete)} "
                    f"kept={len(result['kept'])} protected={len(protected)}")
        return result

    for f in would_delete:
        try:
            os.remove(f)
            result["deleted"].append(f)
            logger.info(f"window_retention: removed {os.path.basename(f)}")
        except OSError as e:
            logger.warning(f"window_retention: 删除失败 {os.path.basename(f)}: {e}")
    return result
