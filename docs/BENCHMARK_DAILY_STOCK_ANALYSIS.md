# 对标笔记: ZhuLinsen/daily_stock_analysis

> **对标依据**:只读了对方 README 与它索引的 `docs/market-support.md` / `docs/full-guide.md` 的能力描述,**没有读它的源码,也没有做代码级审计**。因此本文比较的是「能力面」,不是「实现质量」。
>
> **用途**:回答一个问题——它有哪些东西值得搬进 QuantStar,搬进来落在哪个文件,代价多大。

---

## 1. 先分清定位,不然会对标错方向

| | daily_stock_analysis (DSA) | QuantStar |
|---|---|---|
| 一句话 | 自选股**日报生成器**:每天把持仓的几只票分析成一张决策仪表盘推到 IM | **生产级量化系统**:选股 → 因子 → 多智能体质检 → 状态机执行 → 结算对账 |
| 核心风险 | 报告质量 | **下错单、算错账** |
| 判据文化 | 输出好看、结论可信 | 判例、变异验证、三态律、fail-closed |
| 交付形态 | GitHub Actions 零服务器 / Docker / 桌面端 | crontab + systemd + SQLite + 飞书 HITL |
| 广度 | A/HK/US/JP/KR/TW + ETF,7 类搜索源,6 类推送渠道 | A 股单市场,15 个行情源,飞书为主 |

**结论**:DSA 强在**广度与可部署性**,QuantStar 强在**可审计性与执行纪律**。要补的是广度里真正影响生产稳定的那部分,而不是把 QuantStar 改造成日报工具。

---

## 2. 能力面对比

| 维度 | DSA | QuantStar 现状 |
|---|---|---|
| 市场 | A/HK/US/JP/KR/TW + ETF | A 股(含北交所链条) |
| 行情源 | AkShare / Tushare / Pytdx / Baostock / YFinance / Longbridge / TickPlus | akshare / baostock / tushare / mootdx / easyquotation / gtimg / zzshare / fuyao / adata / tonghuashun(**15 个适配器**) |
| 源能力边界 | **文档化表**(market-support.md) | 分散在 `capabilities` 集合里,**无汇总视图** → 本 PR 补上 |
| 新闻/搜索 | **7 家可轮换**(SerpAPI/Tavily/Bocha/Brave/MiniMax/SearXNG/Anspire),配额感知 | `news_collector` + `news_tiering`,单一链路为主 |
| 推送 | 企微/飞书/TG/Discord/Slack/邮件,含签名校验 | 飞书(DM/群/多维表/HITL)+ apprise 外挂 |
| 部署 | GitHub Actions 零服务器;Docker;桌面端 | cron + systemd + Docker;无零服务器模式 |
| Web | FastAPI 工作台(任务进度/历史报告/主题) | `web/api.py`(232 行)+ marimo 看板 + 鉴权代理 |
| Agent | 策略问股(15 种内置策略,多轮) | agent_layer(辩论/委员会/专家),**无对话入口** |
| 回测 | 有(AlphaEvo 另仓) | WFO、Purged CV、全费率、Paper→Real 毕业门禁 |
| 风控 | 报告层建议 | 三闸 + 五层出场 + 回撤熔断 + HITL 审批(**本系统核心资产**) |
| 时点(PIT) | 未见声明 | `data_layer/pit.py` + 公告日对齐 |
| 断点续传 | 明确支持 | `replay_dlq` + partial 管道,**无 date_range checkpoint** |
| 在线异常检测 | 无 | `source_health` 交叉验证(DL-26)、`source_switch` 降级留痕 |

---

## 3. 值得借鉴的(按落地价值排序)

### P1 — 直接影响数据可信度

**① 源能力矩阵 + 缺口即红**(本 PR 已落地)

DSA 用一张 md 表讲清「哪个市场、哪个源、能拿什么」。文档会过期,改成代码判据就不会:
`data_layer/sources/coverage.py` 从 `DATA_TYPES` × `capabilities` × `is_available()` 自动派生,
`qs data sources` 一条命令出四态(ok / 单点 / 降级 / 缺口)。
这把 `ARCHITECTURE.md` 宪法#5「单点必有对冲」从口号变成了可执行断言。

**② 主备分层:token 型商业源优先,免费源只兜底**

DSA 明写「免费源受限流与接口变动影响,长期定时建议配置 token 源」。QuantStar 的
`SourceRegistry` 已有 `type_priorities` 机制,缺的是**默认策略**:现在靠各管道显式 register 的
priority 决定,没人系统地回答「哪类数据该由谁主供」。
落地:`sources/registry.py` 增加一份按 `data_type` 的默认优先级基线 + 一条
「商业源未配置时降级留痕」的判据(与 `source_switch` 同源)。

**③ 新闻源多 key 轮换 + 配额感知降级**

DSA 的 7 家搜索源是有配额概念的(每家有 QPS/日限)。QuantStar 的 `news_collector` 一旦
主源 429 基本只能等下一轮。落地:`news_collector.py` 引入 key 池 + 429/配额错误分类
(区分「限流,换 key 重试」与「无结果,正常空」—— 正是三态律的 NONE/EMPTY/ERROR)。

### P2 — 影响运维韧性

**④ 断点续传(按 date_range 存 checkpoint)**

DSA 明确支持。QuantStar 有 `replay_dlq`(失败重放)和 partial 管道修复,但一段 250 日的
回填跑到 180 天被 `flock` 或超时打断后,只能整段重跑。落地:
`ops_layer/pipeline_logger.py` 增加 `(pipeline, date_start, date_end, cursor)` 记录,
`pipelines/base.py` 支持从 cursor 续跑。**注意**:必须与 `flock_guard` 的超时预算一起设计,
否则续跑会变成新的并发源。

**⑤ 推送通道签名校验的显式化**

DSA 逐通道讲清签名/校验方式。QuantStar 走飞书 IM API 不涉及 webhook 签名,但若接
企微/钉钉 webhook,签名缺失就是静默 401。落地:`delivery_layer/channels/` 增加通道能力声明
(是否需要签名/是否需要 token),启动时校验配置完整性。

### P3 — 体验层,优先级低

**⑥ Web 工作台任务进度** —— `web/api.py` 补任务进度/历史报告,不必照抄全套 UI。
**⑦ Agent 策略问股(多轮)** —— agent_layer 已有能力,缺 `/chat` 入口;`web/api.py` 加一条路由即可。
**⑧ 智能导入(图片/CSV/拼音补全)** —— 对生产系统价值低,跳过。

---

## 4. QuantStar 更强、不该为对标稀释的

1. **状态机 + 三闸 + 五层出场**:DSA 输出的是「建议」,QuantStar 管的是「单据」。把两者混在一起会让报告和执行两套判据并存——这正是审计 AUD-04 暴露的问题类型。
2. **PIT 语义**:`pit.py` 的公告日对齐是 DSA 能力面里没有的。多市场扩张时这个必须逐市场重新推导(港股/美股财年是不同的),不能简单套用。
3. **判例 + 变异验证**:`tests/unit/_mutation.py` 这套纪律是真正的护城河,对标时不能因为「对方没有」而放松。

---

## 5. 不建议照搬的

- **单文件 `main.py` 编排**:QuantStar 的分层契约正是为了防这个。
- **免费源零配置默认**:DSA 这么做是为了 fork 后 5 分钟能跑;对生产系统等于把稳定性押在上游限流上。
- **「评分 + 买卖点」直接进执行**:QuantStar 有 RR 闸门与状态机,报告层的评分只能是输入,不能是决策。
- **多市场一夜铺开**:港股/美股会带来财年、交易时段、涨跌停(无)、复权(不同)、结算制度全套差异。真要扩,应按「一个市场一轮」推进,先把 `TradingCalendar` 参数化(现在 `common/calendar.py` 硬编码 A 股节假日)。

---

## 6. 建议路线图

| 批次 | 内容 | 落点 | 规模 |
|---|---|---|---|
| 已做 | 源能力矩阵 + `qs data sources` | `sources/coverage.py` | 小 |
| 下一批 | 源优先级基线 + 商业源缺配留痕 | `sources/registry.py` | 小 |
| 下一批 | 新闻源 key 轮换 + 配额三态 | `news_collector.py` | 中 |
| 后续 | date_range checkpoint(与 flock 预算联合设计) | `pipeline_logger.py` / `pipelines/base.py` | 中 |
| 后续 | 市场参数化日历(扩 HK/US 的前置) | `common/calendar.py` | 中 |
| 可选 | Agent 问股路由 | `web/api.py` | 小 |

---

## 7. 本 PR 落地了什么

- `src/data_layer/sources/coverage.py` — 能力矩阵 + `gaps()` / `assert_coverage()`,判据全部复用现成注册表,**不造第二份清单**;
- `DataLayer.get_source_coverage()` — 层内接线;
- `qs data sources [--json]` — 命令行出口(人读 + 机器读);
- `tests/unit/data_layer/test_source_coverage.py` — 四态各一钉 + 探测异常不中断 + 汇总守恒 + 未知类型名报缺口。

**没有做的**(避免只交文档):优先级基线、key 轮换、checkpoint 三项都需要改动在跑的生产管道,单独一轮做更稳。

---

*本文只做能力面对比,不是对 DSA 的质量评价;它对「5 分钟跑起来」的优化是它那个定位下的正确选择。*
