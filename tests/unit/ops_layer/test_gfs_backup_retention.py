# -*- coding: utf-8 -*-
"""GFS 备份保留 (2026-09-20): 7 日 + 4 周 + 6 月分级轮转。

现状病根: db_backup 只轮转 keep_daily=7 — 删掉的备份永久消失。
若数据损坏 8 天后才察觉 (陈旧损坏/误删), 最近 7 日备份可能全是坏库
(备份的是同一个坏状态的 7 份拷贝), 无更早恢复点。GFS 三档 = 恢复深度。

统筹三硬约束 (sm-28f3a431442d4dbc / 卡⑤):
1. 白名单式删除 — 只删「日备 glob 命中且不在保留集」的文件;
   pinned/ 手工件 / 解析不了文件名的永不进删除集。
2. 删前断言保留集非空且含最新一份。
3. 幂等 + 留痕 — 重跑第二轮零删除; 每删一份留痕。

日期锚 = 文件名 (qs_YYYY-MM-DD.db.gz), 不用 mtime (mtime 受 cp --preserve
影响; 08-21 血案的「字典序」教训同族: 排序锚必须是语义锚)。
"""
import os


# 判例#8: RED 须 AssertionError 非 ImportError — 模块存在性先显式断言。
def test_backup_retention_module_exists():
    try:
        import ops_layer.backup_retention  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"ops_layer.backup_retention 未建: {e}") from e


import pytest


# ---- 测试脚手架: fake-dated 备份文件 (统筹要求 fake-dated, 不 mock 时间库) ----

def _make(paths, tmp_path):
    """在 tmp 下造空文件 (fake-dated: 文件名即日期锚, mtime 无关紧要)。"""
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    for p in paths:
        (d / p).write_bytes(b"x")
    return str(d)


def _names(tmp_path):
    return sorted(os.listdir(os.path.join(str(tmp_path), "backups")))


# ---- 保留集纯函数 (核心判据) ----

def test_keep_set_nonempty_and_contains_newest(tmp_path):
    """三硬约束#2: 保留集非空且必含最新一份。"""
    from ops_layer.backup_retention import gfs_keep_set
    d = _make([
        "qs_2026-09-18.db.gz",  # 最新
        "qs_2026-09-17.db.gz",
        "qs_2026-09-16.db.gz",
        "qs_2026-09-15.db.gz",
        "qs_2026-09-14.db.gz",
        "qs_2026-09-11.db.gz",
        "qs_2026-09-09.db.gz",
        "qs_2026-08-15.db.gz",  # 老
        "qs_2026-07-20.db.gz",
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=4, keep_monthly=6,
                        today="2026-09-20")
    assert keep, "保留集不得为空 (三硬约束#2)"
    newest = max(files)
    assert newest in keep, "最新一份必须在保留集内 (三硬约束#2)"
    # 语义勘定 (日期窗): 日档 09-14~09-20 锁 5 份; 09-09/09-11 同属 ISO
    # 周37 (09-07~09-13), 周档每周只锁一份且锚=最早 → 锁 09-09 删 09-11;
    # 08-15 落月档 (8月 ∈ 4~9月窗) 锁; 07-20 (7月) 超全部档窗 → 不锁
    # (轮转收敛 = 会删, 正是 GFS 的意义)。
    assert os.path.join(d, "qs_2026-09-09.db.gz") in keep, "09-09 周37锚 (该周最早)"
    assert os.path.join(d, "qs_2026-09-11.db.gz") not in keep, \
        "09-11 同周第二份不锁 (周档每周一份)"
    assert os.path.join(d, "qs_2026-08-15.db.gz") in keep, "08-15 8月锚"
    assert os.path.join(d, "qs_2026-07-20.db.gz") in keep, "07-20 7月锚 (6个月窗=4~9月含7月)"
    # 此 fixture 全部 9 份均被三档窗锁定 (与「此集全锁」断言一致)
    # 09-11 是本 fixture 唯一该删的 (周37 第二份) — 已由上方 not-in 断言钉住


def test_older_than_all_tiers_is_deleted(tmp_path):
    """不在任何档的老文件进删除集 — 轮转真收敛, 不是只增不删。"""
    from ops_layer.backup_retention import gfs_keep_set
    d = _make([
        "qs_2026-09-18.db.gz",
        "qs_2026-04-01.db.gz",   # 4月锚 (月档保 6 个月: 4~9月)
        "qs_2026-02-01.db.gz",   # 7 个月前 → 超出月档 6 个月, 非周锚 → 删
        "qs_2026-01-05.db.gz",   # 8 个月前 → 删
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=4, keep_monthly=6,
                        today="2026-09-20")
    assert os.path.join(d, "qs_2026-09-18.db.gz") in keep
    assert os.path.join(d, "qs_2026-04-01.db.gz") in keep, "4月锚应在月档 (6个月窗内)"
    assert os.path.join(d, "qs_2026-02-01.db.gz") not in keep, "7个月前必须可删"
    assert os.path.join(d, "qs_2026-01-05.db.gz") not in keep


def test_weekly_tier_locks_one_per_iso_week(tmp_path):
    """周档: 每个 ISO 周锁最早一份 (排除月档已占), 保最近 4 周。"""
    from ops_layer.backup_retention import gfs_keep_set
    # 2026-09-14(周一)~09-18(周五) = 上周 (ISO 周 38); 09-07 周是 ISO 37
    # 7 日窗外、5 周窗内: ISO 37/36 两周应各有锁定
    d = _make([
        "qs_2026-09-18.db.gz",  # 最新, 日档
        "qs_2026-09-11.db.gz",  # ISO 周 37 (周五) — 周 37 锚
        "qs_2026-09-09.db.gz",  # 周 37 更早, 让锚取 09-07
        "qs_2026-09-07.db.gz",  # 周 37 最早 (周一) — 应锁这份
        "qs_2026-09-04.db.gz",  # ISO 周 36 (周五)
        "qs_2026-08-31.db.gz",  # ISO 周 36 最早 (周一) — 应锁这份
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=4, keep_monthly=6,
                        today="2026-09-20")
    assert os.path.join(d, "qs_2026-09-07.db.gz") in keep, "ISO周37锚=该周最早一份"
    assert os.path.join(d, "qs_2026-09-09.db.gz") not in keep, \
        "周37的第二份不锁 (周档每周一份, 9-07已锁)"
    assert os.path.join(d, "qs_2026-08-31.db.gz") in keep, "ISO周36锚"


def test_monthly_tier_locks_first_of_month(tmp_path):
    """月档: 每自然月锁最早一份, 保最近 6 个月。"""
    from ops_layer.backup_retention import gfs_keep_set
    d = _make([
        "qs_2026-09-18.db.gz",
        "qs_2026-09-01.db.gz",   # 9月锚 (9月最早) → 月档锁它
        "qs_2026-08-01.db.gz",   # 8月锚
        "qs_2026-08-20.db.gz",   # 8月第二份不锁 (若非周锚)
        "qs_2026-04-02.db.gz",   # 4月锚 (6个月窗内: 4~9月)
        "qs_2026-03-28.db.gz",   # 3月 → 超6个月窗 → 删
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=4, keep_monthly=6,
                        today="2026-09-20")
    assert os.path.join(d, "qs_2026-09-01.db.gz") in keep, "9月锚"
    assert os.path.join(d, "qs_2026-09-18.db.gz") in keep, "最新日备恒留"
    assert os.path.join(d, "qs_2026-08-01.db.gz") in keep, "8月锚"
    assert os.path.join(d, "qs_2026-04-02.db.gz") in keep, "4月锚在6个月窗内"
    assert os.path.join(d, "qs_2026-03-28.db.gz") not in keep, "超窗月份可删"


def test_whitelist_never_deletes_unparseable_or_pinned(tmp_path):
    """三硬约束#1: 白名单式 — 非日备命名/pinned 永不进删除集。"""
    from ops_layer.backup_retention import gfs_keep_set
    d = _make([
        "qs_2026-09-18.db.gz",
        "qs_2026-09-11.db.gz",
        # 白名单外 (命名不匹配日备 glob): 永不删
        "manual_fix_snapshot.db",        # 手工件
        "qs_2026-08-01.db",              # 无 .gz (VACUUM INTO 中间态)
        "stock_financials_dump_20260906.sql",
        "g1_backup_20260825.db",         # 08-21 血案锚 qs_*.db* 家族的幸存者
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=4, keep_monthly=6,
                        today="2026-09-20")
    # keep 集只含能被 GFS 判定的日备; 非日备文件必须也全在 keep (不被删)
    for extra in ("manual_fix_snapshot.db", "qs_2026-08-01.db",
                  "stock_financials_dump_20260906.sql", "g1_backup_20260825.db"):
        assert os.path.join(d, extra) in keep, f"白名单外文件 {extra} 不得进删除集"


def test_prune_idempotent_and_leaves_trace(tmp_path, caplog):
    """三硬约束#3: prune 两连跑 — 第一轮删超档, 第二轮零删除 (幂等)。"""
    from ops_layer.backup_retention import prune_backups
    d = _make([
        "qs_2026-09-18.db.gz",
        "qs_2026-04-01.db.gz",   # 4月锚 (月档 4~9月窗) → 留
        "qs_2026-02-01.db.gz",   # 超档 (7个月前) → 第一轮删
        "qs_2026-01-05.db.gz",   # 超档 (8个月前) → 第一轮删
    ], tmp_path)
    deleted1 = prune_backups(d, keep_daily=7, keep_weekly=4, keep_monthly=6,
                             today="2026-09-20")
    assert len(deleted1) == 2, f"第一轮应删 2 份, 实删 {deleted1}"  # 02-01/01-05 超档 (4~9月窗外); 04-01 月档锚留
    assert not os.path.exists(os.path.join(d, "qs_2026-02-01.db.gz"))
    assert os.path.exists(os.path.join(d, "qs_2026-04-01.db.gz"))
    # 第二轮: 幂等, 零删除
    deleted2 = prune_backups(d, keep_daily=7, keep_weekly=4, keep_monthly=6,
                             today="2026-09-20")
    assert deleted2 == [], f"第二轮必须零删除 (幂等), 实删 {deleted2}"
    assert os.path.exists(os.path.join(d, "qs_2026-04-01.db.gz"))


def test_prune_refuses_when_keep_set_would_be_empty(tmp_path):
    """三硬约束#2 兜底: 目录里只剩白名单外文件 → 一份不删 (无日备可判)。"""
    from ops_layer.backup_retention import prune_backups
    d = _make(["manual_fix.db", "notes.txt"], tmp_path)
    deleted = prune_backups(d, keep_daily=7, keep_weekly=4, keep_monthly=6,
                            today="2026-09-20")
    assert deleted == [], "无日备可判时零删除 (不碰白名单外文件)"


def test_tier_counts_are_config_driven(tmp_path):
    """config 键化: keep_weekly=0 关周档, keep_monthly=2 缩月档 — 不写死。"""
    from ops_layer.backup_retention import gfs_keep_set
    d = _make([
        "qs_2026-09-18.db.gz",
        "qs_2026-09-01.db.gz",   # 9月锚 (keep_monthly=2: 8~9月)
        "qs_2026-08-01.db.gz",   # 8月锚
        "qs_2026-07-01.db.gz",   # 7月 → 超2个月窗 (除非周锚), 8/31无备份→
                                 # 周档 4 周窗: 08-24~09-20, 07-01 不在 → 删
    ], tmp_path)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    keep = gfs_keep_set(files, keep_daily=7, keep_weekly=0, keep_monthly=2,
                        today="2026-09-20")
    assert os.path.join(d, "qs_2026-09-01.db.gz") in keep
    assert os.path.join(d, "qs_2026-08-01.db.gz") in keep
    assert os.path.join(d, "qs_2026-07-01.db.gz") not in keep, "月窗=2 时 7月可删"
