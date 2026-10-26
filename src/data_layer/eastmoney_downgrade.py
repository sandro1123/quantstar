"""data_layer/eastmoney_downgrade.py — DL-28 (D-43): 东财系 akshare 端点应用层反爬记档 + 降级标记

决策 D-43 (2026-08-22, 逐层审计/数据源能力全盘点-2026-08-22.md §四):
东财系端点被**应用层反爬**拦截 —— TCP 通、TLS 握手完整、证书验证 OK、ALPN 协商
完成, 请求发出后服务端主动关闭; 关键旁证是服务器拒绝 HTTP/2 只收 http/1.1
(TLS ClientHello 指纹识别的典型表现)。IP 封禁/IPv6/缺 Header/TLS 版本四个假设
逐一排除均不吻合。处置 = **不绕过**, 理由四条: ①东财数据在本系统有替代
②对抗反爬是持续维护成本, 与"连续 3 日无人干预自运行"目标冲突 ③合规风险
(2026-03 东财已推官方授权数据服务) ④datacenter-web.eastmoney.com 子域可达,
真需要时从该子域找。

本模块是东财降级名单的**单一事实源** (复用 DL-39 声明式模式, 不造第二套机制):
- akshare 适配器读 EASTMONEY_ENDPOINTS, 东财端点失败时经 _em_call 落
  em_downgrades 降级标记 (可观测), 并按"已知降级"记日志而非新告警;
- 任何未来的源健康/告警逻辑若要区分"东财已知降级"与"新故障", 先读本名单。

**关键: 降级是间歇性的, 不是硬封死 —— 所以这是软标记, 不屏蔽调用。**
实测 (2026-08-21): stock_sector_fund_flow_rank (东财 datacenter, 无 _em 后缀)
仍返回 1000 行; datacenter-web.eastmoney.com 返回 200。因此**不在调用前拦截**,
只在失败时打标; 哪天通了自动恢复, 无需改代码 (D-43 原文)。

推论 (盘点 §四): efinance / qstock 底层同为东财, 装了也用不了 —— 这很可能是
历史上"akshare 一直配不好"的真正原因 (社区推荐的多是东财系接口)。
"""

from __future__ import annotations

# 东财系 akshare 端点名单 (D-43 降级范围) = 盘点 §四 明确判定的东财端点。
# 判据: `_em` 后缀 (东财 push2 系) 无歧义; stock_sector_fund_flow_rank 虽无
# _em 后缀但盘点明确归为东财 datacenter 系 (间歇可用, 2026-08-21 实测仍出数)。
# 注意: stock_zh_a_hist / stock_financial_analysis_indicator 不带 _em 且盘点未
# 判为东财反爬受影响, 故**不在**本名单 (避免误标可用的 kline/财务主链)。
EASTMONEY_ENDPOINTS: frozenset[str] = frozenset({
    # 估值 (真源替代: stock_valuation 已由其他源覆盖)
    "stock_value_em",
    # 全市场快照名录 (替代: akshare_sina stock_list 主链)
    "stock_zh_a_spot_em",
    # 分钟K线
    "stock_zh_a_hist_min_em",
    # 涨停股池 (替代: fuyao / zzshare limit_up)
    "stock_zt_pool_em",
    # 跌停股池 (替代: fuyao / zzshare limit_down)
    "stock_zt_pool_dtgc_em",
    # 分析师研报评级
    "stock_research_report_em",
    # 板块资金流 — 东财 datacenter, 间歇可用 (2026-08-21 实测 1000 行)
    "stock_sector_fund_flow_rank",
})

# 降级决策编号, 供日志/审计引用
DOWNGRADE_DECISION = "D-43"

# 降级性质: 应用层反爬 (WAF/TLS 指纹), 间歇性, 软标记不屏蔽
DOWNGRADE_REASON = "应用层反爬 (WAF/TLS 指纹), 间歇性, 不绕过"


def is_eastmoney_endpoint(name: str) -> bool:
    """akshare 端点名是否为已记档的东财系 (D-43 降级范围)。"""
    return name in EASTMONEY_ENDPOINTS
