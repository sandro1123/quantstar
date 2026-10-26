# -*- coding: utf-8 -*-
"""ops_layer/backup_retention.py — GFS 分级备份保留 (W2 卡⑤, 2026-09-20)。

病根: db_backup 只轮转 keep_daily=7 — 删掉的备份永久消失, 恢复深度 7 天。
若数据损坏 8 天后才察觉, 最近 7 份日备可能全是同一坏状态的拷贝, 无更早
恢复点。GFS 三档 (7 日 + 4 周 + 6 月) = 恢复深度。

统筹三硬约束 (REC 卡同期派单 GFS, sm-28f3a431442d4dbc):
1. 白名单式删除 — 只删「日备命名 (qs_YYYY-MM-DD.db.gz) 且不在保留集」;
   pinned/ 手工件 / 解析不了文件名的永不进删除集 (08-21 血案:
   glob("qs_*.db*") 字典序连带删手工备份 → 判例: 排序/匹配锚必须是语义锚)。
2. 删前断言保留集非空且含最新一份。
3. 幂等 + 留痕 — 重跑零删除; 每删一份 logger.info 留痕。

日期锚 = 文件名 (非 mtime — mtime 受 cp --preserve 影响, 文件名是
备份本体自带的语义锚)。

磁盘数学 (2026-09-20 生产实测): 7 份日备 ~3.7GB/份 = 25.9GB 恒定水位;
gz 增速 +0.0078GB/日 (非 3.5G/日 — 3.5G 是单份大小非日增量, 已勘误)。
GFS 稳态 ≈ 7日+4周+6月 ≈ 17 份 ≈ 63GB (102G free 内, 净增 +38GB;
周/月档与日档重叠的份不重复占空间 — 同一文件多档锁定只保留一份)。
"""
from __future__ import annotations

import glob
import logging
import os
import re
from datetime import date, timedelta

logger = logging.getLogger("ops.backup_retention")

# 日备文件名白名单: qs_YYYY-MM-DD.db.gz (db_backup handler 的产出锚)
_DAILY_BACKUP_RE = re.compile(r"^qs_(\d{4})-(\d{2})-(\d{2})\.db\.gz$")


def _parse_daily_name(path: str) -> date | None:
    """从文件名解析日备日期; 非日备命名返回 None (白名单外)。"""
    m = _DAILY_BACKUP_RE.match(os.path.basename(path))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None  # 语义非法日期 (2026-13-40) = 白名单外, 不判不删


def _week_anchor(d: date) -> date:
    """ISO 周锚 = 该周一 (周档每 ISO 周一份)。"""
    return d - timedelta(days=d.weekday())


def gfs_keep_set(
    files: list[str],
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 6,
    today: str | date | None = None,
) -> set[str]:
    """GFS 保留集 (纯函数): 从日备文件列表选出该保留的文件路径集合。

    三档:
    - 日档: 最近 keep_daily 份 (按日期倒序)
    - 周档: 最近 keep_weekly 个 ISO 周, 每周锚定该周最早一份
    - 月档: 最近 keep_monthly 个自然月, 每月锚定该月最早一份
    白名单外文件 (非日备命名) 无条件保留 — 它们不归 GFS 管。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    if today is None:
        today = date.today()

    dailies: list[tuple[date, str]] = []
    others: set[str] = set()
    for f in files:
        d = _parse_daily_name(f)
        if d is None:
            others.add(f)  # 白名单外: 永不删 (硬约束#1)
        else:
            dailies.append((d, f))
    dailies.sort(key=lambda x: x[0])

    keep: set[str] = set(others)

    # 日档: 日期在最近 keep_daily 天内 (锚定 today 倒推, 不是「最近 N 份」
    # — 后者在断供时放大窗口: 断 10 天后「最近 7 份」= 17 天深度, 语义漂移;
    # 日期窗语义: 断供期间就是没有, 恢复点深度诚实)。
    daily_floor = today - timedelta(days=max(0, keep_daily - 1))
    for d, f in dailies:
        if keep_daily > 0 and d >= daily_floor:
            keep.add(f)

    # 日档窗外的候选, 归周档/月档锁定
    older = [x for x in dailies if x[1] not in keep]

    # 周档: 今天所在 ISO 周往回数 keep_weekly 个周, 每周锚 = 该周最早一份。
    # 窗必须锚定 today 倒推 (2026-09-20), 不能只取「文件里最近的 N 个周」——
    # 后者让够老的周也永远进集 (RED 实证: 7 个月前的份被锁 = 轮转不收敛)。
    this_week = _week_anchor(today)
    week_floor = this_week - timedelta(weeks=max(0, keep_weekly - 1))
    weeks: dict[date, tuple[date, str]] = {}
    for d, f in older:
        w = _week_anchor(d)
        if w < week_floor:
            continue  # 出周窗
        if w not in weeks or d < weeks[w][0]:
            weeks[w] = (d, f)
    if keep_weekly > 0:
        for w in weeks:
            keep.add(weeks[w][1])

    # 月档: 最近 keep_monthly 个自然月 (窗=今天所在月份往回数 N 个月,
    # 每月锚 = 该月最早一份; 同样按 today 锚定, 不按文件集合锚定)。
    def _month_index(d: date) -> int:
        return d.year * 12 + (d.month - 1)

    cur_idx = _month_index(today)
    month_floor = cur_idx - (max(0, keep_monthly - 1))
    months: dict[int, tuple[date, str]] = {}
    for d, f in older:
        mi = _month_index(d)
        if mi < month_floor:
            continue  # 出月窗
        if mi not in months or d < months[mi][0]:
            months[mi] = (d, f)
    if keep_monthly > 0:
        for mi in months:
            keep.add(months[mi][1])

    # 硬约束#2: 保留集非空且含最新一份 (有日备可判时)
    if dailies:
        newest = max(dailies, key=lambda x: x[0])[1]
        assert keep, "GFS 保留集为空 (硬约束#2)"
        assert newest in keep, f"最新一份 {newest} 必须在保留集内 (硬约束#2)"
    return keep


def prune_backups(
    backup_dir: str,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 6,
    today: str | date | None = None,
) -> list[str]:
    """对备份目录执行 GFS 轮转, 返回被删文件路径列表 (留痕由 logger 落)。

    白名单式: 只有 qs_YYYY-MM-DD.db.gz 命名且不在 gfs_keep_set 的文件会删;
    其余 (手工件/pinned/解析不了) 永不触及。幂等: 第二轮零删除。
    无日备可判时零删除 (census_guard 判例: 空集显式不动作, 不哑删)。
    """
    pattern = os.path.join(backup_dir, "qs_????-??-??.db.gz")
    files = sorted(glob.glob(pattern))
    keep = gfs_keep_set(files, keep_daily=keep_daily, keep_weekly=keep_weekly,
                        keep_monthly=keep_monthly, today=today)
    # 硬约束#2 兜底: 判定面 (files) 为空 → 零删除
    if not files:
        return []
    deleted: list[str] = []
    for f in files:
        if f in keep:
            continue
        try:
            os.remove(f)
        except OSError as e:
            logger.warning(f"GFS prune: 删除失败 {f}: {e}")
            continue
        deleted.append(f)
        logger.info(f"GFS prune: removed {f} (daily={keep_daily}, "
                    f"weekly={keep_weekly}, monthly={keep_monthly})")
    return deleted
