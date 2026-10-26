# 附录·判例精选与术语表

> 前文见《QuantStar 设计与实现》主文件与十层详解章节。

## A. 判例精选（新人护身符）

影响面最大的 12 条，按"你会先撞上"的顺序排：

1. **想当然表名**——管道叫 stock_list 实际落 stock_info 表。任何表操作前 PRAGMA 亲验真名。
2. **mock 落后于调用形态**——管道加了 kwargs，旧 mock lambda 没加 → 测试假红。改调用面必同步 mock 签名。
3. **列名错=静默假绿**——veto 否决源查 decision_date 列，真表叫 created_at，查询空结果被当"无否决"。凡查询必对齐真表 schema。
4. **夹具默认值与行内值不一致**——夹具 default code='000001' 但行内写 '600519'，断言炸在错的地方。夹具与断言同源。
5. **断言落后于现实**——存量测试按 10 源写死，新源注册后断言炸。断言要写成"至少含/形态"而非"穷举等于"。
6. **合并到 main ≠ 生产生效**——benchmark 修复合并后 13 天从未真正跑过（生产竞态环境吞了）。验收锚必须是 runs 表实跑行。
7. **sys.path 陷阱**——软链 venv 的 editable .pth 指生产树，worktree 测试偷偷加载生产代码。强制 importlib 按路径装载+assert 路径含 worktree 名。
8. **ISO 时间戳显示层截断**——H:M:S 含冒号在显示层被劫持/截断，判读用文件 md5 落盘比对，不靠终端回显。
9. **dotenv 引号**——.env 里 KEY="value" 的双引号会被当值的一部分（key 剥引号后才是真值）。
10. **f-string 嵌套同引号**——3.12 合法 3.11 炸（生产 venv=3.11）。ops 脚本必须用生产 venv 解释器验证。
11. **时敏测试缺省 now**——收盘段逻辑缺省 datetime.now()，白天跑测试恒在收盘前 → 全静默假绿。时敏测试显式传时间参数。
12. **整库备份禁令**——单表写作业期间禁整库备份（锁竞争+满盘风险）。S0 单表 dump 先行。

## B. 术语表

| 术语 | 释义 |
|---|---|
| 三态 | buy_now / wait_watch / pass——推荐建议的三个态 |
| 条件单四件套 | 为什么选/在哪买/什么情况买/何时卖 |
| 三池一门 | 推荐池→跟踪池→持仓→退出的全生命周期通道 |
| PIT | Point-in-Time 双时态——只用当时能见到的数据 |
| S0 | 写库前的单表 dump 备份纪律 |
| 建了没通电 | 代码/表/管道存在但无排程无消费——等于不存在 |
| 五态律 | 🟡部分完成不是完成态 |
| FAN_OUT | 多通道同发不降级的投递类型（盘中警示噪音源） |
| 哨兵 | 每日对拍固定判据的观测作业（行数/新鲜度/漂移） |
| 判例 | 病根→修法→为什么 的可复用坑记录 |
| 窗 | 22:05 唯一变更窗（现双窗制+15:35） |
| 断言锚 | 验收用的可计算判据（非形容词） |
| 基座续链 | 新分支建在自己旧分支尖端（同文件族免冲突） |
| worktree | git 并行施工目录——六席并行开发的基础设施 |
| 错峰重投 | 会话限流死后的延迟重发策略（避免立即再撞同闸） |

## C. 数据库主要域表索引

stock_info（全市场底册）/ daily_klines（1,283 万行）/ stock_financials / factor_daily（712 万行）/ factor_weights / factor_ic / model_predictions / agent_predictions / agent_checkpoints / memory_store / stock_news（1.3 万行）/ news_stock_links（多对多关联，09-11 建）/ lhb_daily（source 列）/ paper_trading / paper_positions_snapshot / recommendation_retrospect（906 行——昨日推荐点评的数据面）/ action_records / delivery_log / pipeline_runs / alerts / concept_membership / hot_concept_daily / popularity_daily / adjust_factors / dividends / splits——全 84 表见 schema_version 迁移史。

## 夜班补遗（2026-09-11 凌晨，kimi 亲执施工实录）

> 来源：22:05 主窗补执 + 夜班自主施工。全部为实证判例（含锚点见 CHANGELOG）。

- **判例 13：hasattr 探测式接线必须逐类断言效果**——记忆层六专家仅 1 类实现 `set_memory_db`，入口 `hasattr` 探测静默跳过其余五类，「六专家注入」实为 1。修法：`_MemoryWire` 基类收口 + 注册表驱动验收（`ALL_EXPERTS` 逐类断言真消费，新专家入册自动覆盖）。**机制对 ≠ 效果通。**
- **判例 14：worktree 隔离机具不得随合并进 main**——测试在收集期清洗 `sys.modules` + 硬绑 worktree 路径，单跑全绿、进套件毒化后续测试导入。合并前必拆。
- **判例 15：回归调用面须与基线同胚**——给回归误加 `CONFIG_ENV=prod` 触发 CF-01 七键校验炸出 178E，代码无辜。复刻流水线时环境键/工作目录/解释器与代码变更同等重要。
- **判例 16：时敏测试的三种形态**——①夹具 `date` 列从 aged 时间戳派生，跨午夜行不可见（canrun）；②排程紧贴 00:01，子夜宽限期内「未到死线」断言漂（heartbeat CLI）；③硬编码日期日历腐烂（当日告警两周后腐成「旧账」误入选）。修法统一：夹具用代码钟 `bj_date()`，CLI 留 `--now` 可测性口，禁硬编码日期。
- **判例 17：脚本不得假设 .venv 与自身同树**——`PY=$ROOT/.venv/bin/python` 在无 venv 的 worktree 里炸出 17 红（watchdog-shell 族）。加 `command -v python3` 回落即愈。
- **判例 18：退市检测管线建了没通电**——`stock_list` 管线上线数月零调度（无 crontab 腿），且其安全闸口径会被 250 只北交所码顶翻。结果：196 只死股（含退市 20 年的）挂 `is_active=1` 数月，还被填充器写入平价零量假 K 线。修法：翻牌+管道口径修 0/3/6 头+挂 15:22 每日腿。**观测件的价值=病藏不住。**
- **判例 19：校验面须跟上格式演进**——晚报宪法六段改出飞书卡片后，旧校验器每日误报「报告段落为空」。契约面落后于格式演进是静默家族的远亲。
- **判例 20：`git add -A` 在嵌套 worktree 仓库里会吞 gitlink**——永远显式路径；已用 `.gitignore /qs-*/` 结构性防护。
- **判例 21：预并体检（merge-tree 零冲突 + 分支×最新 main 预跑测试）能在窗前抓病**——news_intel 第七专家缺记忆注入面，被注册表驱动验收在窗前十小时抓捕。
