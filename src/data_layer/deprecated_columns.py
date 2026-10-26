"""data_layer/deprecated_columns.py — DL-39-exec (D-66): factor_daily 31 物理列正式弃用

用户 2026-08-23 拍板 b) 正式弃用。依据: 逐层审计/DL-39-断供列等价性分析-2026-08-22.md
—— factor_scores JSON 无这 31 列任何原始值等价 (0 完全/10 近似/21 无等价);
物理列自 2026-07-17 集体断供; strategy_layer/cache.py 把它们当 Layer-2 DB 缓存,
断供后永远 miss → 落 Layer-3 现算 (系统没坏, 只是每天白查一次空缓存)。

本模块是弃用名单的**单一事实源**:
- strategy_layer/cache.py 读 DEPRECATED_FACTOR_COLUMNS, Layer-2 跳过这些列
  (省掉每因子一次必 miss 的 DB 查询)
- 新鲜度审计豁免名单 = 同一集合 (不造第二套机制; 挂 DL-29/DL-38 的
  data_quality_flags 豁免思路: 声明式名单 + 消费方按需读)

**不动表结构、不删列 (除 xueqiu_heat_score 空列, 单独 GO)、600~690 万行/列历史数据全保留。**
消费方若依赖这些列, 需改用真源表 (见 跨层契约变更.md DL-39 条目):
估值类→stock_valuation, 行情衍生→daily_klines 现算, 龙虎榜→lhb_daily,
分析师→analyst_forecasts, 财报类→财务报表 (DL-31)。
"""

from __future__ import annotations

# 31 个 2026-07-17 断供的物理列 (D-66 弃用范围)。fwd_return_1d/5d/20d 不在其中
# —— 那是 IC 环唯一输入, 必须活着 (DL-24/DL-34 负责)。
DEPRECATED_FACTOR_COLUMNS: frozenset[str] = frozenset({
    # 估值 (真源: stock_valuation)
    "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv",
    "pe_rank", "pb_rank",
    # 财报 (真源: 财务报表, DL-31)
    "roe", "gross_margin", "net_margin", "rev_growth", "profit_growth",
    "roe_rank",
    # 行情衍生 (真源: daily_klines 现算)
    "momentum_5d", "momentum_20d", "volatility_20d", "volume_ratio",
    "macd_hist", "rsi_14", "rsi6", "rsi12", "turnover_rate", "change_pct",
    "momentum_rank", "macd_rank", "momentum_score",
    # DL-66 普查补录 (2026-08-23): ma_status 全 NULL、无消费方、不在原 31 列名单,
    # 同属 2026-07-17 断供族 → 第 32 列 (统筹 sm-b3254cb457254ab1 批准小改)。
    "ma_status",
    # 情绪/结构 (真源: lhb_daily / analyst_forecasts / concept_daily)
    "analyst_consensus_score", "lhb_drama_score", "lhb_institution_score",
    "xueqiu_heat_score", "concept_count",
})

# 弃用生效日 (最后写入日), 供文档/审计引用
DEPRECATED_SINCE = "2026-07-17"

# ---------------------------------------------------------------------------
# DL-66 / 统筹 sm-a5a07066c2944051 裁定: lhb_daily 两列弃用 (⚫ 裁定不做)
# ---------------------------------------------------------------------------
# 根因形状 (单独记账, 统筹将提炼进框架):
#   "schema 里有列、写入方从不写、而且上游源根本没有这个字段。"
# 这不是"忘了写", 是建表时凭想象加了一列, 而数据源 (zzshare lhb) 里压根没有
# 对应的东西 —— 与 add_adjust_type_column.py 的 DEFAULT 'qfq' 同类病:
# schema 的设想跑在数据现实前面, 然后没人回头核对。
# 实测 (DL-66 普查 + zzshare lhb_list 字段探针): zzshare lhb 返回
# amplitude/buy_in/capitalization/turnover/turnover_ratio/up_desc/join_num 等,
# 无任何 drama 字段 → drama_score 无源可采。
# 裁定: 弃用 lhb_drama_score 因子 (不走派生路线, 派生分无 ground truth 且
# IC 现在不可测); amplitude/turnover_ratio/join_num/up_desc 派生异动因子入
# Q-50 排队, 出队条件 = GO-F 落地 IC 可测后用 IC 验证。
DEPRECATED_LHB_COLUMNS: frozenset[str] = frozenset({
    "drama_score",
    "drama_score_change",
})

def is_deprecated_lhb_column(name: str) -> bool:
    """列名是否为已弃用的 lhb_daily 列。"""
    return name in DEPRECATED_LHB_COLUMNS

# 新鲜度审计豁免名单 (D-66 要求)。复用弃用名单本身 —— 声明式单一事实源,
# 与 DL-29/DL-38 的 data_quality_flags 豁免同一思路 (名单声明 + 消费方按需读),
# 不造第二套机制。
#
# 实测澄清 (2026-08-23): 当前 freshness_audit 是**表级**审计
# (REQUIRED_TABLES 4 张 + COLLECT_TABLES 8 张), factor_daily 不在其清单,
# 也没有任何列级新鲜度检查会报这 31 列 —— 所以今天并无"狼来了"在响。
# 此名单是给**未来**任何列级新鲜度/停写检查的豁免依据: 引入此类检查时
# 必须先读本名单跳过 deprecated 列, 否则才会真的天天误报。
FRESHNESS_EXEMPT_FACTOR_COLUMNS: frozenset[str] = DEPRECATED_FACTOR_COLUMNS


def is_deprecated_factor_column(name: str) -> bool:
    """factor_name 是否为已弃用的 factor_daily 物理列。"""
    return name in DEPRECATED_FACTOR_COLUMNS
