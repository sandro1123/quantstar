# 数据流审计报告 — 2026-11(第二批)

> 第一批见 `docs/AUDIT_2026-11_ADVERSARIAL.md`(风控四闸:入场/闸门/约束/出场)。
> 本批换一个视角:**跟着数据走一遍**,从源到落库、从落库到打分、从打分到人看到的字。
>
> 方式仍是纯静态阅读。**本环境无 Python 执行能力,未运行测试套件**;凡需要跑数才能定案的,统一列在第 6 节。

---

## 0. 追踪路径

```
源适配器 → pipeline → 清洗/落库 → 读回(PIT) → 因子 → 聚合 → 选股 → 执行 → 报告段落 → 人
           ①            ②          ③         ④       ⑤      ⑥        ⑦        ⑧
```

本批重点在 ③(读回)、④⑤⑥(打分与选择)、⑧(人看到的字)。⑦(执行)由第一批覆盖。

---

## 1. 发现清单

| 编号 | 级别 | 一句话 | 位置 |
|---|---|---|---|
| AUD-11 | P1 | 报告里的「综合分」不是排序用的那个分数(三分数并存,只露一个) | `report_layer/sections/tomorrow_picks.py:40,162` |
| AUD-12 | P1 | 渲染异常被伪装成「暂无推荐」—— ERROR 冒充 EMPTY | `tomorrow_picks.py:176-179`、`:66` |
| AUD-13 | P1 | 漏斗最宽的一道闸建在 3.3% 覆盖的字段上,且报告不解释 | `strategy_layer/selection/selector.py:126-137` |
| AUD-14 | P1 | Top-N 截断发生在排序**之前**,依赖调用方预排序 | `selector.py:206` vs `:259` |
| AUD-15 | P2 | L3.3 近期表现调整用 `score *= (1+boost)`,对负分反向 | `selector.py:247-252` |
| AUD-16 | P0-CI | 配置 schema 落后于 yaml → 配置门禁长期红 | `config/schemas/backtest.schema.json:62` |
| AUD-18 | **P0** | 回撤闸门「显式放行」的返回值是 truthy 字符串,`check()` 把它当拦截 —— 日志说放行,实际拦单 | `protection_gate.py:247-280` vs `:114-116` |
| AUD-19 | P1-CI | 两份测试互相矛盾,不可能同时通过 —— 开源仓库的 CI 因此永远红 | `test_llm_10router_hy3.py` vs `test_llm_deprivatization.py` |

---

## 2. 人类产出面(AUD-11 ~ AUD-12)

### AUD-11 「综合分」是入池时的 initial_score,不是当前排序依据

`tomorrow_picks.py:40-45` 的 SQL 只取 `initial_score`,第 162 行把它标成「综合分」展示:

```python
score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "评分生成中"
...
lines.append(f"  信心与折扣：综合分 {score_s} ｜ 审批 {review}{health}")
```

但系统里同时存在**三个分数**:

| 字段 | 谁在写 / 谁在读 | 语义 |
|---|---|---|
| `initial_score` | 入池时写死;`tomorrow_picks` 展示 | 入池那一刻的分数 |
| `current_score` | 每日更新;`exit_engine._layer_6_score_decay` 用来判衰减 | **当前**分数 |
| `composite_score` | `selector.py:259` 排序依据(含 L3.3 调整) | **本次选股真正用的**分数 |

后果:人看到的是 A,系统排序用的是 C,出场判定用的是 B。三者一旦分叉(入池后评分下降是常态,衰减出场正是为此而设),报告就在**用一个已经不成立的数字给人信心**。这是「报告与执行两套判据」在内容层的复现(第一批 AUD-04 是同族)。

**修法**(需你定口径,我没改):展示层要么显示 `current_score` 并同时给出「入池 X → 当前 Y」的变化,要么保留 `initial_score` 但把标签改成「入池分」。**最不该保留的是现在这个措辞**:「综合分」三个字听起来是"综合起来的当前评分",而它实际是历史快照。

### AUD-12 渲染失败被写成「暂无推荐」——三态律的最后一道口子

`tomorrow_picks.py:176-179`:

```python
except Exception as e:
    logger.warning(f"TomorrowPicksSection build failed: {e}")
    return SectionResult(title="📌 明日推荐", body="暂无推荐（数据积累中）", ...)
```

同一文件 `:66` 的 `pick_codes` 也是 `except Exception: return set()`。

正文里两种完全不同的世界被压成同一句话:

- **EMPTY**:今天市场确实没有符合条件标的 → 「暂无推荐」正确;
- **ERROR**:SQL 挂了 / 表缺列 / advisor 链抛异常 → 用户看到的还是「暂无推荐（数据积累中）」,只是"数据还在攒",**永远不会去查**。

`docs/ARCHITECTURE.md` 1.1 宪法#1 与 `CONTRIBUTING.md` 的三态律管的正是这件事。数据层已经做得很好(`source_health` 的 -1/0/>0 三态),**唯独最后一公里——给人看的那句话——没有三态**。

**修法**:catch 分支返回不同的正文,例如「推荐生成失败(已告警),见运维页」,并把 `blocked/reason` 带进 `SectionResult`。代价是测试里断言 `暂无推荐（数据积累中）` 的用例要跟着改(见 `tests/unit/report_layer/test_structure_top3.py`),所以我没有单方面改。

---

## 3. 选择面(AUD-13 ~ AUD-15)

### AUD-13 最宽的闸门建在一个覆盖率 3.3% 的字段上

`selector.py:126-137`:

```python
ld = list_dates.get(c.code, "")
if not ld:
    n_unknown += 1
    continue        # 上市日期未知 → 直接拒绝
```

同文件 `:59` 自己写着:

> 当前 `stock_info.list_date` 覆盖仅 3.3%,待 DL-21 回填后此过滤才真正可用。

也就是说:**约 96.7% 的候选在这道闸上被拒**,而拒绝的理由是"我们没有这个字段",不是"这只票不合格"。fail-closed 的哲学没错(未知即不买),但链路上下**没有任何一级把这个事实当成故障**:

- `filters_applied` 里有一行 `L1 上市日期未知(拒绝, 待DL-21回填): N 只` —— 但它只是被 append 进一个列表;
- 最终到人眼里的是 `明日推荐: 暂无推荐（候选池积累中）`。

**同一句话同时描述了"市场没机会"和"我们缺一个字段"**。这是本批我认为最该先修的一条:**它会让系统在数据不健康时表现得像市场冷清**,而这类静默降级正是你们判例文化里最忌的。

**修法(建议)**:把 `n_unknown / total` 作为一条硬判据 —— 超过阈值(如 50%)时 `SelectionResult.blocked=True`,理由写「L1 数据依赖缺失: list_date 覆盖 X%」,并让报告层如实转述。这样"选不出来"会被归因到数据,而不是市场。

### AUD-14 Top-N 在排序之前截断

```python
# L2: Top-N
top_n = top_n_map.get(market_regime, 15)
candidates = candidates[:top_n]          # :206  ← 按"传入顺序"切片
...
candidates.sort(key=lambda x: x.composite_score, reverse=True)   # :259 ← 最后才排序
```

`selector.run()` 从头到尾没有对输入排序。它取的是 `factor_daily` 的**原始顺序**。当前能工作,是因为上游 aggregator 恰好按分降序产出——这是一个**隐式契约,没有断言、没有注释、没有测试守着**。

一旦上游改成按 code 排序、或并发聚合导致顺序不定,`Top 15` 就会变成"编号靠前的 15 只",而且**不会报错**:后面 `:259` 的 sort 还会把结果整理得漂漂亮亮,看起来完全正常。

**修法**:在 `candidates = list(factor_daily)` 之后立刻 `candidates.sort(key=lambda x: x.composite_score, reverse=True)`,并加一条变异钉(把 sort 注掉必须红)。L3.3 之后的 `:259` 保留(调整后重排是必要的)。

### AUD-15 `score *= (1 + boost)` 对负分反向

```python
boost = max(-0.2, min(0.2, val * 2))
c.composite_score *= (1 + boost)
```

乘法对**负分**是反的:某只票 `composite_score = -1.5`、近期收益 +10%(`boost = +0.2`),调整后变成 `-1.8` —— **表现越好,排得越后**。

z-score 体系里负数占一半,所以这不是边角。若本意是"线性加一个动量项",应该是 `score += |score|` 归一化后的量,或直接 `score += boost * scale`。

**修法**:先确认 `composite_score` 的值域与符号含义(是不是已经做过截面标准化);若可负,改用加法形式,并在测试里钉一条"负分股 + 正收益不得被降权"。

### AUD-17(附)`today_rows[0]` 取的是任意一行

`selector.py:99-101`:`today_rows = [k for k in ks if k.date == date]`,然后取 `today_rows[0].close`。若同一 `(code, date)` 因多源合并写入两行(库内 `daily_klines` 是否保证唯一未在本次审计范围内确认),取哪一行取决于遍历顺序。同类取首行的写法在 `_l1_hard_filters` 里只用了一处,影响面小,记录备查。

---

## 4. CI 红 —— 根因定谳与已修的一条(AUD-16)

### AUD-16 配置 schema 落后于 yaml,配置门禁长期红(本 PR 已修)

`config/schemas/backtest.schema.json:62-69` 里 `walk_forward` 被锁成标量:

```json
"walk_forward": { "type": ["null","string","number","boolean"] }
```

而 `config/defaults/backtest.yaml:18-24` 早已是 WFO 参数块(6 个键的映射)。于是
`tests/unit/config_layer/test_config_gate.py::TestStage2Schema::test_config_matches_schema[backtest.yaml]`
**必然失败**,失败信息就是 CI 里那条:

> `{'n_windows': 5, ...} is not of type 'null','string','number','boolean'`

**根因不是生成器坏了,是 schema 没重新生成。** `scripts/gen_config_schemas.py` 的行为是正确的:
`walk_forward` 当时是 `null` 时它生成标量联合;后来有人把 WFO 参数填进去,却没重跑生成器。

**本 PR 的修法**:把该节点按生成器的实际输出语义重写为 `type: object` + 6 个属性 + required(= 键名排序)+ `additionalProperties: true`。这与 `python scripts/gen_config_schemas.py` 会产出的内容一致,不是手写艺术。

**遗留风险**:其它 12 份 schema 也可能同样落后。我无法在本环境跑生成器确认,所以只改了 CI 报错点名的这一份。请你本地执行一次:

```bash
python scripts/gen_config_schemas.py && git diff --stat config/schemas/
```

若 diff 为空,说明其余 schema 都是新的;若有差异,那就是一批还没被发现的门禁盲区。

### 其余 9 条红(未修,附根因)

| 失败用例 | 我读到的根因 | 建议 |
|---|---|---|
| `test_no_hardcoded_dates`(2 条) | `.hardcoded_date_baseline.txt` 未收录新增文件;存量 4588 处 | 跑门禁自带的基线刷新流程,或按报错清单补录 |
| `test_llm_10router_hy3`(3 条) | **与 `test_llm_deprivatization` 互相矛盾,见 AUD-19** —— 不是配置分叉,是两份测试在断言相反的事实 | 归位或删除旧的那份 |
| `test_paper_broker` | `PaperTradeResult(reason='invalid_status')` | 夹具状态与 broker 的状态机前置不匹配 |
| `test_paper_broker_entry_idempotency::test_missing_action_records_fails_closed` | 断言 fail-closed,`protection_gate.py:85-103` 对"表不存在"走 fail-open | **这是安全语义分歧,不是笔误**——见下 |

最后一条需要单独说:`protection_gate` 里那段注释(EX-04b)解释得分很清楚——空库意味着"无历史=无可评估风险",fail-open 是为了不误杀测试。但测试断言的是 fail-closed。**两边的哲学都对,冲突在"表不存在"到底算不算风险未知**。这条只能由你裁定,我不改。

---

## 4b. 新增两条(AUD-18 / AUD-19)

### AUD-18 回撤闸门「显式放行」在 `check()` 里是拦截(P0)

**证据链**(三处代码互相对照,不是推测):

1. `protection_gate.py:246-280` —— `_check_drawdown` 在 `dd is None or peak_unavailable` 时进入「无法判定」分支。该分支在两种情形下给出**非空字符串**:
   - NAV 末端陈旧 > 5 自然日 → `守卫读数陈旧 (...) → 显式放行`;
   - 无活跃持仓但有待卖单 → `无 活 跃 持 仓 显式放行 (判 例 包E)`。
   然后落一条 `guard_stale` 留痕,并 `return reason`。
2. `protection_gate.py:114-116` —— `check()` 对每个守卫的返回值只做 `if reason:`,**任何非空字符串都 `return False, reason`**,即拦截入场。
3. `protection_gate.py:247` 的注释原文:`显式放行 (truthy reason = block)` —— 作者**知道** truthy 即 block,但标签仍叫"放行"。

**结论**:这两个分支的实际效果是**拦截入场**,而留痕(`guard_stale`)、日志、告警文案里写的都是「显式放行」。运维翻账本看到的是"系统已放行",实际单子被拦着。

**这不是理论路径,有一个确定会触发的场景**:`_stale_nav_reason` 的阈值是 5 个**自然日**(`:365`),而 A 股长假远超 5 天。以仓库自己的日历(`common/calendar.py`)为例:

- 国庆 2026:10-01 ~ 10-08 休市,最后交易日 09-30,首个交易日 10-09 —— 间隔 **9 天**;
- 春节 2027:02-11 ~ 02-17 休市,最后交易日 02-10,首个交易日 02-18 —— 间隔 **8 天**。

长假后首个交易日的盘中,`last_price_date` 还停在假前最后一天(日终盯市要到收盘后才更新),于是 `_curve_stale_days() > 5` → 触发「陈旧」分支 → 整个交易日**入场被拦**,而日志说"显式放行"。*(推断:我没有生产日志佐证它是否真的发生过;但代码路径与日历数字都是可核对的。)*

**为什么现有测试抓不到**:`test_pack_b_stale_nav.py:67` 与 `test_pack_b_production_link.py:52` 断言的是 `_check_drawdown` / `_stale_nav_reason` 的**返回字符串里含"显式放行"** —— 它们钉住的恰好是那句有歧义的文案。我读到的测试里没有一条经过 `check()` 去断言 `can_trade` 的实际取值。又是"断言只看文案、不看效果"。

**修法(需你定语义,我没改)**:二选一,现在是两边都不是:

- 想要**放行**:这两个分支 `return ""`,把"显式放行"的可见性交给 `guard_stale` 留痕(已经在写)—— 这与注释里"放行 (reason="" 走 paused 逻辑)"的意图一致;
- 想要**拦截**:文案改成「读数陈旧,拦截入场」,并把阈值从"自然日"改成"交易日"(否则每个长假都会误拦一整天)。

无论选哪个,都应补一条经过 `check()` 的断言:`can_trade` 的真假必须与留痕文案一致(并按 AGENTS.md 加变异验证)。

### AUD-19 两份测试互相矛盾,开源 CI 不可能转绿(P1)

`tests/unit/config_layer/test_llm_deprivatization.py`(Issue #15)断言:

- `config/defaults/*.yaml` **不得**出现 `\.10router`、`Hermes-Agent`(`:24-28`);
- `llm.yaml` 的 provider **不得**有 `key_source`(`:57`)。

`tests/unit/agent_layer/test_llm_10router_hy3.py`(2026-10-21 作者私有网关切换时写)断言的恰好相反:

- 每条腿必须 `key_source`,路径含 `.10router/db/data.sqlite`(`:47-52`);
- query 必须含 `Hermes-Agent`;主链必须是 `cbcn/hy3`。

`config/defaults/llm.yaml` 现在是 Issue #15 之后的脱敏版(`api.openai.com` / `gpt-4o-mini` / `api_key_env`),所以**后者必红、前者必绿**。两份测试断言的是互斥事实,无论 `llm.yaml` 怎么改,总有一份红 —— **CI 的 `test` job 因此在开源仓库里结构性不可能全绿**。

后果比"红灯"更严重:门禁长期红,人就学会忽略它;而 PR #20(纯文档)也红,正是这个原因 —— 没有信号能力的门禁,放过了 AUD-01。

**修法**:`test_llm_10router_hy3.py` 属于私有部署校验,应搬到 `config/local.yaml` 的校验(或在缺少私有网关时 `skip`),不应在开源默认配置上断言。

---

## 5. 我做不到的事:代为申请账号/Key

需要说清楚:我**无法代你注册任何账号或申请 API Key** —— 那需要你的邮箱、手机号、实名与支付信息,而且多数平台的服务条款也要求注册行为由账号持有人本人完成。

我能做的是**把"要申请什么"变成一张可执行的清单**:哪个 Key 解锁哪条数据链、填哪个环境变量、缺了会降级成什么、免费额度够不够跑日频。见 `docs/DATA_SOURCE_KEYS.md`(本 PR 同批提交)。

---

## 6. 需要你跑数才能定案

1. **AUD-11**:`SELECT code, initial_score, current_score FROM stock_pool WHERE status IN ('watch','pending_entry','active')` —— 看两者分叉有多大。分叉越大,现在这句「综合分」越误导。
2. **AUD-13**:`SELECT COUNT(*) FROM stock_info WHERE list_date IS NOT NULL AND list_date != ''` —— 确认 3.3% 是否仍然成立;若已回填,请更新 `selector.py:59` 的注释,否则它会一直误导下一个读者。
3. **AUD-14**:看 `Aggregator` 产出是否**始终**按 `composite_score` 降序。若是,补一条断言把契约钉住即可;若不是,`Top 15` 现在就是错的。
4. **AUD-15**:`SELECT MIN(composite_score), MAX(composite_score) FROM factor_daily` —— 确认能否为负。
5. **AUD-16**:`python scripts/gen_config_schemas.py && git diff --stat config/schemas/`

---

## 7. 结论

第一批的病灶是「闸门看起来有、实际不拦」。这一批是**同一个病在内容层的镜像**:报告看起来在解释、实际在复述一个过期的数字(AUD-11);链路看起来"暂无推荐"、实际是缺字段(AUD-13)或渲染挂了(AUD-12)。

数据层的三态律做得比多数开源项目认真得多(`source_health` 的 -1/0/>0、`freshness_audit`、`readiness`)。**缺的从来不是意识,是把同一条纪律走完最后 100 米**——走到人看到的那句话为止。

---

*本报告为静态审计产出,不含运行期证据。标注「需跑数」的条目请勿直接据此改生产判定。*
