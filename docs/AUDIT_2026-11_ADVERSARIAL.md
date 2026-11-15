# 敌对审计报告 (Adversarial Audit) — 2026-11

> 审计口径:**假设系统会用它自己的话术骗过审计**。凡"文档声称有闸门"处,一律回到代码里找出闸门的实际判据,再问一句"这个判据在什么输入下恒真"。
>
> 审计方式:纯静态阅读(逐行追踪调用链 + 交叉核对同族模块的注释与实现)。**本次审计未运行测试套件**,所有结论均为代码可判定项(给出 file:line 与反例),不含需要跑数才能证实的推测——需要跑数才能定案的,统一列在第 6 节。

---

## 1. 结论摘要

| 编号 | 级别 | 一句话 | 位置 |
|---|---|---|---|
| AUD-01 | P0 | 回撤熔断:分子含现金、分母不含现金,−8% 熔断在非满仓结构下永不触发 | `protection_gate.py` |
| AUD-02 | P0 | 入场约束被喂 100 股订单,"余额不足"与"单只仓位上限 20%"两道闸生产链路恒不触发 | `entry_engine.py` / `constraint_checker.py` |
| AUD-03 | P1 | 追高防御在异常时静默放行,零痕迹 | `entry_engine.py:82` |
| AUD-04 | P1 | 当日连损熔断的判据是"人话子串 + 单一写入者的 action_type" | `protection_gate.py` |
| AUD-05 | P1 | 冷却期按工作日计,节假日窗口内**提前**解除;注释把方向写反 | `protection_gate.py:136` |
| AUD-06 | P1 | 超时出场依赖 `holding_days`,而同仓注释自称该列"死列恒 0" | `exit_engine.py` |
| AUD-07 | P2 | layer4 的 "MACD 死叉" 不是 MACD,是 SMA 差离 | `exit_engine.py:232` |
| AUD-08 | P2 | 指标口径 off-by-one 与 `omega=9999` 魔法数 | `metrics.py` |
| AUD-09 | P3 | PIT `staleness_days` 在 lookback 分支恒 0(假值) | `pit.py:115` |
| AUD-10 | P3 | 费用口径文档写千分之一、代码是万分之五 | `docs/ARCHITECTURE.md:231` |

---

## 2. P0 — 会让风控"看起来有、实际没有"的两条

### AUD-01 组合回撤熔断的分子分母不同源 → 熔断永不触发

**证据链**

- 分子 `_current_portfolio_nav()` (`src/execution_layer/core/protection_gate.py:170-189`):
  `nav = get_available_cash() + Σ(last_price × position_qty)`,**含现金**。
- 分母 `_peak_nav()` (`protection_gate.py:191-207`):
  `peak = MAX over days of Σ(mv)`,数据源 `paper_positions_snapshot`,而该表的 `mv` 是**持仓市值、现金不进表**——见 `src/backtest_layer/paper_trading/settlement.py:31` 原文:
  > `weight = 该仓 mv / Σ(active mv) 仓内占比 (现金不进表)`
- 判定 `dd = nav / peak - 1` (`protection_gate.py:241`),且 `peak = max(peak, nav)` 只会抬高分母。

**为什么这不只是"偏一点"**

只要**峰值那一天不是满仓**,分母就系统性小于当日真实净值。用你自己在 `protection_gate.py:172-177` 里记录的真值代入:

- 记录的真值:`nav = 6,848 + 279,315 = 286,163`,真实回撤 `−4.61%` → 隐含当时分母 ≈ 300,000(全组合口径)。
- 换成现在的 `_peak_nav`:分母变成 `MAX Σmv`。若峰值日持仓市值就是 279,315,则
  `dd = 286,163 / 279,315 − 1 = +2.45%`。

**同一天、同一份数据,旧实现说 −4.61%,新实现说 +2.45%。** 符号都反了。而熔断条件是 `dd <= -8%`,所以在现金占比显著时(描述里 6,848 现金 / 286,163 净值 ≈ 2.4% 现金都已能让符号翻转),这个闸门不是"偏严/偏松",而是**恒不成立**。

这与 10-07/10-08 那次假熔断是同族病灶的反面:那次是分母口径与分子不同源导致**假触发**,这次是同一处不同源导致**永不触发**。注释里那句"绝不拿 initial_capital 当分母"解决了假触发,但没有回答"那分母到底该是什么"。

**为什么现有测试抓不到**

`tests/unit/execution_layer/test_fuse_fix.py:256-259` 直接把 `mv = 290000 / 310000 / 295000` 喂进快照表——即把**组合净值**当成**持仓市值**写。夹具与实现在同一个错误假设上互相印证,所以绿灯。

**修复方向(需要你定口径,这里只给选项)**

1. 快照落库时同时落当日 `cash`(或新建一张 `nav_daily(date, cash, mv, nav)`),`peak = MAX(cash + mv)` —— 与分子完全同源,推荐;
2. 或者把分子降级为"持仓市值",即 `dd` 定义为**在投仓位**的回撤(与分母同源),同时把配置项与文档改名,不再宣称"全组合回撤";
3. 在方案落地前,至少让 `_peak_nav` 在"该 peak 与 nav 口径不可比"时**显式不可用**(返回 None + 留痕),而不是当分母用。现在的行为是拿一个不可比的数算出正回撤,然后当成"没问题"。

---

### AUD-02 入场约束收到的是 100 股,不是本笔计划规模

**证据链**

- 唯一生产调用点 `src/execution_layer/core/entry_engine.py:129`:
  `constraint_result = constraint_checker.check_entry(code, current_price)` —— 只传两个参数。
- 被调函数 `src/execution_layer/core/constraint_checker.py:60`:
  `def check_entry(self, code, current_price, position_qty: int = 100)` —— 落到默认值 100 股。

**后果**

- 第 3 道"余额不足"(`constraint_checker.py:76-79`)比的是 **100 股的钱**:
  `cost = current_price * 100`。30 万本金、10~50 元的票 → 1,000~5,000 元,永远小于可用现金。
- 第 5 道"单只仓位上限"(`constraint_checker.py:96-104`)算的是
  `100 × price / total_equity`。同样价位下是 **0.3%~1.7%**,而阈值是 20% —— 需要股价 **> 600 元**才可能越界。
- 第 2 道"最小 100 股"因此恒通过(它检查的正是那个默认值)。

也就是说:`execution.max_single_stock_pct = 20` 这个参数在实盘链上是**装饰**,唯一的实际约束只剩"持仓数量上限"与"T+1"。

更根本的问题在签名:`EntryEngine.check_entry(code, current_price, current_time)` 里**根本没有"本笔计划股数"这个概念**,说明 sizing 环节缺失或发生在别处。若 sizing 在别处(如 `paper_broker` 按现金自行定量),那么这道闸门校验的对象与真正下单的对象**不是同一个东西**——这类"校验对象错配"比参数错更危险,因为它不会报错。

**修复方向**

- `position_qty` 改为必填无默认值,缺省即 `TypeError`(宁红不默);
- `EntryEngine` 先算出 `plan_qty`(单只上限 / 现金 / 100 股整手三者取小)再传入,并把这个 `plan_qty` 作为 `EntryDecision` 的一部分返回给 broker,保证"校验的规模 = 下单的规模";
- 测试同步改用真实规模。现有 `tests/unit/execution_layer/test_constraint_checker.py` 全部用 100 股,是恒绿断言。

---

## 3. P1 — 判据在特定路径下失效

### AUD-03 追高防御:异常即静默放行

`src/execution_layer/core/entry_engine.py:42-84`,`_check_spike_defense` 整个函数体包在

```python
except Exception:
    pass
```

里。取历史价(`data_layer.get_history`)、DB 读、RSI 计算任一环节抛异常,结果是**追高防御整体消失,无日志、无降级标记、无留痕**,调用方拿到 `None` 并理解为"检查通过"。

这正面违反 `docs/ARCHITECTURE.md` 1.1 宪法#1「诚实降级,绝不静默」与 `CONTRIBUTING.md` 的三态律。同族对照:`protection_gate.py:108` 的守卫异常走了 fail-closed 并留痕,是正确写法;这里没有。

**建议**:至少 `logger.warning` + 把 `spike_defense=unavailable` 落到 `entry_blocks`;要么干脆 fail-closed。风控面 fail-open 必须可见。

### AUD-04 当日连损熔断:判据是人类文案子串

`src/execution_layer/core/protection_gate.py:155-166`:

```sql
WHERE action_type='exit' AND reason LIKE '%止损%' AND created_at LIKE '<today>%'
```

两个脆弱点:

1. `action_type='exit'` 全 `src/` 只有一个写入点:`src/execution_layer/core/paper_broker.py:227`。而 `exit_engine` 触发时写的是 `status_change`(`stock_pool.py:201`),清理/换仓路径写 `pool_exit` / `swap_exit`。**只要当日止损不是经 `paper_broker` 成交的**(真实券商手工回填、cleanup、swap),计数恒 0,当日 3 次熔断不生效。
2. 判据是 `reason` 文本里的"止损"二字。文案一改(如"硬止损"→"止损位击穿"仍命中,但"破位离场"就不命中了),闸门静默失效。

这与本文件顶部对 `_DD_PAUSE_PREFIX` 的告诫("措辞即协议,不可随意改")是同一个风险,但只有回撤那条被当作协议对待了。

**建议**:改用结构化字段计数(`stock_pool.exit_layer` / `exit_reason`,或 `action_records.action_detail` 里的 layer 编号),不要用自由文本子串做风控判据。

### AUD-05 冷却期按工作日计,方向与注释相反

`src/execution_layer/core/protection_gate.py:136-151`:

```python
"""start 之后到 end 的交易日数 (工作日口径, 节假日保守多算=保护方向安全)。"""
```

按 `d.weekday() < 5` 计数。**工作日数 ≥ 交易日数**,所以在含节假日的窗口里,5 个工作日可能只对应 3~4 个交易日 → `trading_days >= cooldown_days` 提前成立 → **冷却期提前 1~2 天解除**。

注释说的"保守多算=保护方向安全"方向是反的:多算等于**更快放行**,属于欠保护。同理 `return 999` 在解析失败时是"不拦",注释也写成了"保守放行"。

同仓库已经有正确实现:`common/calendar.TradingCalendar.trading_days_between`(`exit_engine.py:179` 在用)。

**建议**:换用交易日历,或至少把注释改成真实方向——注释写反比没写更糟,因为下一个人会照着注释做决策。

### AUD-06 超时出场依赖一个被同仓宣判"死列"的字段

`src/execution_layer/core/exit_engine.py:44` 注释:

> `holding_days 列是死列恒 0 不可用`

而 `exit_engine.py:258-261` 的 layer 5(最大持仓天数超时出场)判据正是:

```python
holding_days = pool.get("holding_days") or 0
if holding_days >= self.max_holding_days and entry_price > 0:
```

`src/execution_layer/tracking/pnl_tracker.py:57-65` 确实会 `holding_days = prev + 1`(仅 `track_pnl` 被调且当日为交易日时)。所以二者必有一错:**要么 layer 5 不生效(注释对),要么注释是陈的(代码对)**。

这条本身不是"一定坏了",而是**同一仓库对同一字段给出互斥结论**——按判例文化,这属于"判据没有唯一源"。README 支柱三 #13 把"最大持仓天数时间衰减退出"列为已交付能力,所以这条会直接影响对外陈述。

**建议**:先跑一条 SQL 定性(`SELECT status, MIN(holding_days), MAX(holding_days), COUNT(*) FROM stock_pool WHERE entry_date IS NOT NULL GROUP BY status`),再决定是修 layer 5(改用 `entry_date` + 交易日历现算,与 layer 4 同源)还是删掉这条能力陈述。

---

## 4. P2/P3 — 口径与失真

### AUD-07 layer4 的 "MACD 死叉" 不是 MACD

`src/execution_layer/core/exit_engine.py:232-253`:

```python
ema12 = sum(chunk[-12:]) / 12
ema26 = sum(chunk[-26:]) / 26
```

这是**简单移动平均**,不是指数移动平均(无递推、无种子、无平滑系数 2/(n+1))。函数名与 reason 文案("MACD死叉")会误导所有后续维护者。另外 `for i in range(25, len(macro_closes))` 在 35 根窗口下只产生 10 个点,DEA 用 9 点均线,首个信号点的统计意义极弱。

**建议**:要么实现真 EMA(12/26/9),要么把函数改名 `sma_cross` 并同步改文案。名字撒谎的指标会在复盘归因里持续制造错误解释。

### AUD-08 指标口径 off-by-one 与魔法数

- `src/backtest_layer/core/metrics.py:63`:年化用 `trading_days / len(equity_curve)`。N 个净值点只有 **N−1** 个收益,应为 `len(returns)`。252 点时把 251 个交易日当 1 年,年化收益率高估约 0.4%(复合口径下随倍数放大)。
- `metrics.py:108` / `:164`:`omega` 在无亏损时 `inf`,落库时被替换为 `9999.0`。一个不存在的数字被写成"极好的 omega",与"宁 None 不假值"的纪律冲突。建议 `None`。
- `metrics.py:213-225`:`deflated_sharpe` 只减去了期望最大 SR,未做方差项修正,是近似而非 DSR 原式。注释应写明是近似,否则会被当作严格多重检验校正引用。

### AUD-09 PIT 的 `staleness_days` 在 lookback 分支恒 0

`src/data_layer/pit.py:115-121`:`lookback_days > 0` 分支硬编码 `staleness_days=0`,而同文件 `:130-137` 的另一分支是真算的。下游无法区分"数据新鲜"与"没算"。按三态律应为 `None` 或真算。

### AUD-10 费用口径:文档与代码差一倍

- `docs/ARCHITECTURE.md:231` 与 README:「印花税(千分之一单向)」。
- `src/execution_layer/core/constraint_checker.py:14` 与 `src/backtest_layer/core/models.py:10`:`stamp_tax = 0.0005`(万分之五)。

**代码是对的**(2023-08-28 起 0.05%),文档是错的 → 按文档估交易成本会高估一倍,进而高估策略的最小可行收益。

附带两处小口径问题:

- 过户费 `0.00002`(`constraint_checker.py:15`、`models.py:13`)是实际费率(0.001%)的**两倍**;
- `calculate_fees` 的 `net_amount = amount - total`(`constraint_checker.py:22`)对**买入**符号是反的(买入应付出 `amount + total`)。若该字段已被使用,账面会少算 2 倍费用。

---

## 5. 已核实为稳健的部分

敌对审计不应只交坏消息。以下是我逐行看过、**未找到可攻击面**的部分:

- **看板鉴权代理** `scripts/qs_dash_auth_proxy.py`:scrypt(n=16384) + 随机盐、`hmac.compare_digest` 双比较、`SameSite=Strict; HttpOnly; Secure`、会话内存态 + 24h TTL、失败计数在**登录成功时清零**(`:292-293`)、仅当对端是本机才信任 `CF-Connecting-IP`(`:226-235`)、`POST` 非 `/login` 一律要求有效会话(`:265-267`)、请求体 1 MiB 上限。未发现鉴权绕过。
- **CI**:actions 全部按 SHA 钉死、gitleaks 先校验 sha256 再执行、`pip-audit` 扫描依赖、`compileall` + import 冒烟 + `ruff`、解释器钉 3.11。这套门禁比多数开源量化项目严格。
- **PIT 财务可得日** `src/data_layer/pit.py:68-79`:公告日优先、为空回落法定披露截止日(4/30、8/31、10/31、次年 4/30),没有"报告期直接放行"的前视口子。
- **Token 预算** `src/agent_layer/llm/token_budget.py`:惰性跨日自愈(不依赖调用点记得 reset)、`real/estimated` 口径可辨、估算兜底不覆盖真实值。设计是自洽的。
- **`get_available_cash`** `stock_pool.py:450-484`:资金守恒式正确,负现金 clamp + 告警落库,没有静默造数。

---

## 6. 未能验证 / 需要生产数据才能定案

本次审计只有代码,没有库、没有日志、没有测试运行结果。以下需要你这边跑数:

1. **AUD-06 定性**:`holding_days` 在生产 `active` 行的实际分布(是否恒 0);
2. **AUD-04 覆盖度**:生产 `action_records` 里 `action_type` 的实际分布,以及当日止损记录分别由哪些路径写入;
3. **AUD-02 事实确认**:`paper_broker` 下单时的实际股数是否由本闸门之外的地方计算(若是,需要对齐"校验规模=下单规模");
4. **AUD-01 修复前的风险敞口**:用生产 `paper_positions_snapshot` 逐日算一遍 `MAX(Σmv)` 与当日 `cash+Σmv`,确认当前 `dd` 的符号是否长期为正;
5. **测试有效性**:本次未运行 `pytest`,也未做变异验证。README 承诺的"6000+ 测试、反向变异"能否抓住 AUD-01/02,取决于夹具是否使用真实规模的数据(见 AUD-01/02 里点名的两个夹具)。

---

## 7. 建议修复顺序

1. **AUD-02**(半天):签名改必填 + 计算 `plan_qty`,影响面小、收益确定;
2. **AUD-01**(1~2 天):先定口径(全组合 or 在投仓位),再动快照表与 `_peak_nav`;这条修复前建议加一条"口径不可比即显式不可用"的临时护栏;
3. **AUD-03 / AUD-05**(各几行):异常留痕 + 换交易日历,顺手清掉两处反向注释;
4. **AUD-04**(半天):风控判据从文本子串换成结构化字段;
5. **AUD-06 / AUD-07 / AUD-08 / AUD-09 / AUD-10**:口径更正与文档同步,可合并成一次 `chore`/`docs` 提交。

---

*本报告由外部 AI Agent 静态审计产出,不含运行期证据;凡标注"建议先跑数"的条目,请勿直接据此改动生产判定。*
